"""NA-07.4 offline admission, response identity and no execution."""
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock

from agent_events import EventJournal
from mission_state import MissionStore, MissionPolicy, parse_request
from mission_cognition import MissionCognition, parse_decision
from github_work import ChatRegistry
from agent_control import ControlState
from agent_relay import AgentRelay


class CognitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = EventJournal(self.root / "events.sqlite3", "bot-5")
        self.now = 10000
        self.store = MissionStore(self.journal, clock=lambda: self.now)
        self.policy = MissionPolicy(frozenset({10}), {"demo": "owner/repo"}, -123, 0)
        self.request = parse_request("demo 60 10 3 12 | CLI | tests pass")
        self.decision = {"action": "propose_task", "reason": "First", "task": "Implement parser"}

    def create(self, *, arm=True, message=1):
        return self.store.create(self.request, owner_user_id=10, message_id=message,
                                 policy=self.policy, arm_cognition=arm)

    def test_initial_claim_and_budget_once(self):
        mission = self.create()
        self.assertEqual(mission["cognition"]["state"], "ready")
        claimed = self.store.claim_cognition(self.policy)
        self.assertEqual(claimed["used_decisions"], 1)
        self.assertIsNone(self.store.claim_cognition(self.policy))
        self.assertTrue(self.store.finish_cognition(mission["mission_id"], self.policy,
                                                    decision=self.decision))
        final = self.store.latest(self.policy)
        self.assertEqual(final["cognition"]["decision"], self.decision)
        self.assertEqual(final["used_tasks"], 0)
        self.assertFalse(final["execution_enabled"])
        self.assertIsNone(self.store.claim_cognition(self.policy))

    def test_old_mission_not_armed(self):
        self.create(arm=False)
        self.assertIsNone(self.store.claim_cognition(self.policy))

    def test_restart_does_not_replay_claimed(self):
        self.create()
        self.store.claim_cognition(self.policy)
        restarted = MissionStore(self.journal, clock=lambda: self.now)
        restarted.recover_cognition(enabled=True)
        self.assertEqual(restarted.latest(self.policy)["cognition"]["state"], "interrupted")
        self.assertEqual(restarted.latest(self.policy)["used_decisions"], 1)
        self.assertIsNone(restarted.claim_cognition(self.policy))

    def test_disabled_discards_ready(self):
        self.create()
        self.store.recover_cognition(enabled=False)
        self.assertIsNone(self.store.claim_cognition(self.policy))
        self.assertEqual(self.store.latest(self.policy)["used_decisions"], 0)

    def test_expired_or_revoked_result_not_accepted(self):
        mission = self.create()
        self.store.claim_cognition(self.policy)
        self.now = mission["expires_at"]
        self.store.finish_cognition(mission["mission_id"], self.policy, decision=self.decision)
        self.assertEqual(self.store.latest(self.policy)["cognition"]["state"], "discarded")
        mission = self.create(message=2)
        self.store.claim_cognition(self.policy)
        self.store.revoke(mission["mission_id"], owner_user_id=10, policy=self.policy)
        self.store.finish_cognition(mission["mission_id"], self.policy, decision=self.decision)
        self.assertIsNone(self.store.latest(self.policy)["cognition"]["decision"])

    def test_control_generation_fences_result(self):
        mission = self.create()
        self.store.claim_cognition(self.policy)
        changed = replace(self.policy, generation=2)
        self.store.finish_cognition(mission["mission_id"], changed, decision=self.decision)
        self.assertEqual(self.store.latest(changed)["cognition"]["state"], "discarded")

    def test_parser_strict(self):
        self.assertEqual(parse_decision('{"action":"wait","reason":"No evidence","task":""}')["action"], "wait")
        for invalid in ('{"action":"start_work","reason":"x","task":"y"}',
                        '{"action":"wait","reason":"x","task":"y"}',
                        '{"action":"wait","reason":"x","task":"","extra":true}',
                        '{"action":"propose_task","reason":"x","task":""}'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_decision(invalid)

    def test_cwa_separate_lane_verified_and_no_dispatch(self):
        mission = self.create()
        registry = ChatRegistry(str(self.root / "chats.json"), main_id="main")
        client, send = Mock(), Mock()
        send.return_value = Mock(text='{"action":"wait","reason":"Need evidence","task":""}',
                                 conversation=Mock(conversation_id="mission-chat"))
        cognition = MissionCognition(client, registry, send, agent_name="Ragna")
        self.assertEqual(cognition.turn(mission)["action"], "wait")
        self.assertEqual(registry.get("mission"), "mission-chat")
        self.assertIs(send.call_args.kwargs["client"], client)
        self.assertTrue(send.call_args.kwargs["verify_final_text"])
        cognition.turn(mission)
        self.assertEqual(send.call_args.kwargs["conversation"], "mission-chat")
        send.return_value.conversation.conversation_id = "main"
        with self.assertRaises(ValueError):
            cognition.turn(mission)

    def test_host_turn_records_proposal_only(self):
        mission = self.create()
        claimed = self.store.claim_cognition(self.policy)
        host = Mock()
        host.control = ControlState(str(self.root / "control.json"))
        host._shutdown.is_set.return_value = False
        host._run_permitted.return_value = True
        host._mission_policy.return_value = self.policy
        host.missions = self.store
        host.mission_cognition.turn.return_value = self.decision
        AgentRelay._run_mission_cognition(host, claimed, 0)
        self.assertEqual(self.store.latest(self.policy)["cognition"]["decision"], self.decision)
        host.work.start.assert_not_called()
        host.api.call.assert_not_called()
        self.assertEqual(host.mission_cognition.turn.call_count, 1)
