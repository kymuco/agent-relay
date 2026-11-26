"""NA-05: two independent agent profiles in one checkout; mocked transports."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from agent_profile import ProfileOwnership, profile_directory, namespace_config
from agent_relay import AgentRelay, Config
from github_work import ChatRegistry
from natural_work import route_natural_work
from task_conversation import normalize_task_reply


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch_root = patch("agent_profile.PROFILE_ROOT", self.root / "profiles")
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def config(self, profile="ragna", name="Ragna", main="main-r", **overrides):
        cfg = Config("token", -123, main, "auth_data.json", frozenset(),
                     agent_profile=profile, agent_name=name,
                     allowed_user_ids=frozenset({10}), work_allowed_user_ids=frozenset({10}),
                     work_enabled=True, work_executor="github", work_live_progress=True,
                     work_projects="demo=" + str(self.root),
                     work_repos="demo=owner/sandbox", natural_work_enabled=True,
                     min_interval_seconds=0, media_enabled=False)
        return replace(cfg, **overrides)

    def app(self, cfg, bot_id=5, username="RagnaBot"):
        api, main, coord, dev = Mock(), Mock(), Mock(), Mock()
        api.call.return_value = {"id": bot_id, "username": username, "message_id": 301}
        main.send.return_value = Mock(text="hello",
                                       conversation=Mock(conversation_id=cfg.conversation_id))
        app = AgentRelay(cfg, api, main, coord, dev)
        if app.profile_lease:
            self.addCleanup(app.profile_lease.close)
        return app, api, main, coord, dev

    def test_state_paths_are_profile_owned_and_idempotent(self):
        r = namespace_config(self.config())
        y = namespace_config(self.config("yuki", "Yuki", "main-y"))
        from agent_profile import STATE_FILES
        for field in STATE_FILES:
            self.assertNotEqual(getattr(r, field), getattr(y, field))
            self.assertEqual(Path(getattr(r, field)).parent, profile_directory("ragna"))
        self.assertEqual(namespace_config(r), r)
        self.assertNotEqual(r.auth_file, y.auth_file)
        with self.assertRaises(ValueError):
            namespace_config(replace(r, state_file=y.state_file))

    def test_invalid_profile_identifiers(self):
        for name in ("../x", "Ragna", "a/b", "con", "com1", "a.b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                profile_directory(name)

    def test_two_agents_same_checkout_independent_work_stop_and_cursor(self):
        r, _, rm, _, _ = self.app(self.config())
        y, _, ym, _, _ = self.app(self.config("yuki", "Yuki", "main-y"), 6, "YukiBot")
        r.work.start("demo", repository="owner/sandbox", task="Ragna task")
        y.work.start("demo", repository="owner/sandbox", task="Yuki task")
        r.cursor.advance(88)
        r.control.stop()
        r.work.stop()
        self.assertTrue(r.control.paused)
        self.assertFalse(y.control.paused)
        self.assertEqual(y.work.record["status"], "queued")
        self.assertEqual(y.work.record["task"], "Yuki task")
        self.assertNotEqual(r.cursor.offset, y.cursor.offset)
        self.assertNotEqual(r.config.work_progress_file, y.config.work_progress_file)
        self.assertEqual(y.config.bot_aliases, ("Yuki",))
        self.assertEqual(y._background.agent_name, "Yuki")
        y.process_update({"update_id": 1, "message": {
            "chat": {"id": -123, "type": "supergroup"},
            "message_id": 42, "from": {"id": 10, "is_bot": False},
            "text": "Yuki, привет"}})
        self.assertIn('"agent_name": "Yuki"', ym.send.call_args.args[0])
        self.assertEqual(ym.send.call_args.kwargs["conversation"], "main-y")
        rm.send.assert_not_called()

    def test_same_profile_cannot_run_twice(self):
        self.app(self.config())
        with self.assertRaisesRegex(ValueError, "already running"):
            self.app(self.config())

    def test_owner_binding_survives_process_exit(self):
        app, *_ = self.app(self.config())
        app.profile_lease.close()
        with self.assertRaises(ValueError):
            self.app(self.config(main="changed"))
        with self.assertRaises(ValueError):
            self.app(self.config(), 9, "OtherBot")
        restored, *_ = self.app(self.config())
        self.assertEqual(restored.bot_id, 5)

    def test_distinct_profiles_cannot_share_bot_or_main_chat(self):
        self.app(self.config())
        with self.assertRaises(ValueError):
            self.app(self.config("yuki", "Yuki", "main-y"), 5, "YukiBot")
        with self.assertRaises(ValueError):
            self.app(self.config("other", "Other", "main-r"), 6, "OtherBot")

    def test_chat_lanes_exclusive_across_profiles_and_restart(self):
        r, *_ = self.app(self.config())
        y, *_ = self.app(self.config("yuki", "Yuki", "main-y"), 6, "YukiBot")
        r._background.registry.remember("coordinator", "coord-r")
        with self.assertRaises(ValueError):
            y._background.registry.remember("developer", "coord-r")
        with self.assertRaises(ValueError):
            y._background.registry.remember("developer", "main-r")
        with self.assertRaises(ValueError):
            r._background.registry.remember("developer", "coord-r")
        r.profile_lease.close()
        Path(r.config.work_chat_state_file).unlink()
        restored, *_ = self.app(self.config())
        self.assertEqual(restored._background.registry.get("coordinator"), "coord-r")

    def test_cwa_wrong_main_identity_is_rejected(self):
        app, _, main, _, _ = self.app(self.config())
        main.send.return_value.conversation.conversation_id = "some-other-chat"
        with self.assertRaisesRegex(ValueError, "different or unknown"):
            app._cwa_send("hello", purpose="chat", conversation="main-r")
        main.send.assert_called_once()

    def test_name_routing_does_not_assume_every_bot_is_ragna(self):
        _, _, _, _, _ = self.app(self.config())
        kwargs = dict(bot_username="YukiBot", aliases=("Юки",),
                      project_repos={"demo": "owner/sandbox"}, active_project=None)
        self.assertIsNone(route_natural_work("Рагна, создай тесты в demo", **kwargs))
        self.assertEqual(route_natural_work("Юки, создай тесты в demo", **kwargs).project, "demo")
        self.assertEqual(normalize_task_reply("Рагна, добавь тест", bot_username="YukiBot",
                                               aliases=("Юки",)), "Рагна, добавь тест")

    def test_named_legacy_branches_unique_even_with_same_local_task_id(self):
        r, _, _, _, _ = self.app(self.config())
        y, _, _, _, _ = self.app(self.config("yuki", "Yuki", "main-y"), 6, "YukiBot")
        with patch("agent_relay.threading.Thread"):
            for app in (r, y):
                app._handle_work_command({
                    "chat": {"id": -123, "type": "supergroup"},
                    "message_id": 7, "from": {"id": 10, "is_bot": False},
                    "text": "/work@" + app.bot_username + " start demo Add tests"}, Queue())
        self.assertEqual(r.work.record["id"], y.work.record["id"])
        self.assertRegex(r.work.record["branch"], r"^agentrelay/bot-5/[0-9a-f]{32}$")
        self.assertRegex(y.work.record["branch"], r"^agentrelay/bot-6/[0-9a-f]{32}$")

    def test_selected_env_file(self):
        env = self.root / ".env.yuki"
        env.write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=-123\n"
                       "CWA_CONVERSATION_ID=main-y\nALLOWED_USER_IDS=10\n"
                       "AGENT_PROFILE=yuki\nAGENT_NAME=Yuki\n", encoding="utf-8")
        with patch.dict(os.environ, {"AGENTRELAY_ENV_FILE": str(env)}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.agent_profile, "yuki")
        self.assertEqual(Path(cfg.state_file).parent, profile_directory("yuki"))

    def test_legacy_registry_cannot_reuse_main_or_other_lane(self):
        p = self.root / "chats.json"
        r = ChatRegistry(str(p), main_id="main")
        with self.assertRaises(ValueError):
            r.remember("coordinator", "main")
        r.remember("coordinator", "c")
        with self.assertRaises(ValueError):
            r.remember("developer", "c")


if __name__ == "__main__":
    unittest.main()
