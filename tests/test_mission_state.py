"""NA-07.3 offline mission persistence, boundaries and owner-only controls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from dataclasses import replace

from agent_events import EventJournal
from agent_control import ControlState
from mission_state import MissionStore, MissionPolicy, MissionRejected, parse_request
from agent_relay import AgentRelay


class MissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = EventJournal(self.root / "events.sqlite3", "bot-5")
        self.now = 10000
        self.store = MissionStore(self.journal, clock=lambda: self.now)
        self.policy = MissionPolicy(frozenset({10}), {"demo": "owner/repo"}, -123, 0)
        self.request = parse_request("demo 60 10 3 12 | Implement CLI | works; tests pass")

    def create(self, **kw):
        args = dict(owner_user_id=10, message_id=1, policy=self.policy)
        args.update(kw)
        return self.store.create(self.request, **args)

    def test_persistence_and_no_execution(self):
        created = self.create()
        restarted = MissionStore(EventJournal(self.journal.path, "bot-5"), clock=lambda: self.now)
        self.assertEqual(restarted.latest(self.policy), created)
        self.assertEqual(created["criteria"], ["works", "tests pass"])
        self.assertEqual(created["expires_at"], 13600)
        self.assertFalse(created["execution_enabled"])
        self.assertFalse(created["merge_allowed"])
        self.assertFalse(created["peer_delegation_allowed"])
        self.assertEqual(created["used_decisions"], 0)
        self.assertEqual(self.journal.read(), [])

    def test_one_active_and_replay_after_revoke(self):
        mission = self.create()
        with self.assertRaisesRegex(MissionRejected, "mission_already_active"):
            self.create(message_id=2)
        self.assertTrue(self.store.revoke(mission["mission_id"], owner_user_id=10, policy=self.policy))
        with self.assertRaisesRegex(MissionRejected, "source_already_used"):
            self.create()
        self.assertNotEqual(self.create(message_id=2)["mission_id"], mission["mission_id"])
        self.assertFalse(self.store.revoke(mission["mission_id"], owner_user_id=10, policy=self.policy))

    def test_expiry_and_new_mission(self):
        old = self.create()
        self.now = old["expires_at"]
        self.assertEqual(self.store.latest(self.policy)["status"], "expired")
        self.assertEqual(self.create(message_id=2)["status"], "active")

    def test_generation_owner_scope_and_clock_invalidation(self):
        policies = [
            replace(self.policy, generation=2),
            replace(self.policy, owners=frozenset({11})),
            replace(self.policy, projects={"demo": "other/repo"}),
            replace(self.policy, chat_id=-456),
        ]
        for i, policy in enumerate(policies):
            self.create(message_id=i+1)
            self.assertEqual(self.store.latest(policy)["status"], "invalidated")
            self.assertEqual(self.store.latest(self.policy)["status"], "invalidated")
        self.create(message_id=5)
        self.now -= 1
        self.assertEqual(self.store.latest(self.policy)["status"], "invalidated")

    def test_reject_wrong_owner_and_project(self):
        with self.assertRaises(MissionRejected):
            self.create(owner_user_id=11)
        with self.assertRaises(MissionRejected):
            self.create(policy=replace(self.policy, projects={}))
        self.assertIsNone(self.store.latest(self.policy))

    def test_invalid_request(self):
        for text in ("demo 0 1 1 1 | g | c", "demo 60 101 1 1 | g | c",
                     "demo 60 1 31 1 | g | c", "demo 60 1 1 201 | g | c",
                     "demo 60 1 1 1 | g |", "demo 60 1 1 1 | | c",
                     "demo -1 1 1 1 | g | c"):
            with self.subTest(text=text), self.assertRaises(MissionRejected):
                parse_request(text)

    def test_isolation(self):
        self.create()
        other = MissionStore(EventJournal(self.root / "other.sqlite3", "bot-6"), clock=lambda: self.now)
        self.assertIsNone(other.latest(self.policy))

    def host(self):
        host = Mock()
        host.bot_id = 5
        host.bot_username = "RagnaBot"
        host.config.chat_id = -123
        host.config.allowed_user_ids = frozenset({10, 11})
        host.config.mission_allowed_user_ids = frozenset({10})
        host.config.peers = frozenset({"yukibot"})
        host.config.peer_bot_ids = frozenset({6})
        host.control = ControlState(str(self.root / "control.json"))
        host.missions = self.store
        host._mission_policy.side_effect = lambda generation=None: replace(
            self.policy, generation=host.control.generation() if generation is None else generation)
        return host

    def message(self, text=None, **kw):
        message = {"chat": {"id": -123, "type": "supergroup"}, "message_id": 1,
                   "from": {"id": 10, "is_bot": False},
                   "text": text or "/mission@RagnaBot start demo 60 10 3 12 | CLI | tests pass"}
        message.update(kw)
        return message

    def test_host_commands_do_not_call_model_or_worker(self):
        host = self.host()
        self.assertTrue(AgentRelay._handle_mission_command(host, self.message()))
        mission = self.store.latest(self.policy)
        self.assertIsNotNone(mission)
        self.assertTrue(AgentRelay._handle_mission_command(host, self.message("/mission@RagnaBot status")))
        self.assertTrue(AgentRelay._handle_mission_command(host, self.message(
            "/mission@RagnaBot revoke " + mission["mission_id"], message_id=3)))
        self.assertEqual(self.store.latest(self.policy)["status"], "revoked")
        host.client.send.assert_not_called()
        host.work.start.assert_not_called()

    def test_peer_forwarded_other_human_and_wrong_chat_rejected(self):
        host = self.host()
        for message in (self.message(**{"from": {"id": 6, "username": "yukibot", "is_bot": True}}),
                        self.message(**{"from": {"id": 11, "is_bot": False}}),
                        self.message(forward_origin={}), self.message(quote={}),
                        self.message(chat={"id": -456, "type": "supergroup"})):
            self.assertTrue(AgentRelay._handle_mission_command(host, message))
        self.assertIsNone(self.store.latest(self.policy))
        host.api.call.assert_not_called()

    def test_stop_resume_does_not_restore_grant_and_paused_start_rejected(self):
        host = self.host()
        host.control.stop()
        AgentRelay._handle_mission_command(host, self.message())
        self.assertIsNone(self.store.latest(self.policy))
        host.control.resume()
        AgentRelay._handle_mission_command(host, self.message())
        self.assertEqual(self.store.latest(host._mission_policy())["status"], "active")
        host.control.stop()
        host.control.resume()
        self.assertEqual(self.store.latest(host._mission_policy())["status"], "invalidated")
        host.client.send.assert_not_called()
