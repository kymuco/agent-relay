"""Separate GPT-6 High developer chat and Ragna-work coordinator tests."""
from __future__ import annotations
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from github_work import (
    BackgroundDeveloper, ChatRegistry, parse_developer, parse_repositories,
    parse_coordinator, verify_remote_branch, checkout_verified_commit,
    _github_origin_matches,
)
from local_codex import CodexResult


def response(text, cid):
    return Mock(text=text, conversation=Mock(conversation_id=cid))


class GitHubWorkTests(unittest.TestCase):
    def config(self, directory, *, verify=False):
        return Config("secret", -123, "main-ragna-chat", "auth.json",
                      frozenset(), allowed_user_ids=frozenset({10}),
                      state_file=str(Path(directory) / "cursor.json"),
                      control_file=str(Path(directory) / "control.json"),
                      media_enabled=False, min_interval_seconds=0,
                      work_enabled=True, work_allowed_user_ids=frozenset({10}),
                      work_projects="demo=" + str(Path(directory).resolve()),
                      work_repos="demo=owner/demo", work_executor="github",
                      work_chat_state_file=str(Path(directory) / "chats.json"),
                      work_state_file=str(Path(directory) / "work.json"),
                      work_local_verify=verify)

    def app(self, folder, *, verify=False):
        api, main, coordinator, developer = Mock(), Mock(), Mock(), Mock()
        api.call.return_value = {"id": 5, "username": "MyBot"}
        app = AgentRelay(self.config(folder, verify=verify),
                         api, main, coordinator, developer)
        return app, api, main, coordinator, developer

    def message(self, body):
        return {"chat": {"id": -123, "type": "supergroup"},
                "message_id": 19, "from": {"id": 10, "is_bot": False},
                "text": body}

    def test_explicit_gpt6_high_even_if_sender_omits_model(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coordinator, developer = self.app(folder)
            main.send.return_value.text = "hello"
            app._cwa_send("secret prompt", purpose="chat", conversation="main-ragna-chat")
            self.assertEqual(main.send.call_args.kwargs["model"], "gpt-6")
            self.assertEqual(main.send.call_args.kwargs["reasoning_effort"], "high")
            app._cwa_send("other", purpose="catchup", model="bad", reasoning_effort="low")
            self.assertEqual(main.send.call_args.kwargs["model"], "gpt-6")
            self.assertEqual(main.send.call_args.kwargs["reasoning_effort"], "high")

    def test_config_defaults_and_wrong_model_rejected(self):
        self.assertEqual(Config("s", -123, "c", "a", frozenset()).model, "gpt-6")
        self.assertEqual(Config("s", -123, "c", "a", frozenset()).effort, "high")
        import os
        config = {
            "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "-123",
            "ALLOWED_USER_IDS": "10", "CWA_CONVERSATION_ID": "c",
        }
        with patch("agent_relay.load_dotenv"), patch.dict(os.environ, config, clear=True):
            cfg = Config.from_env()
            self.assertEqual((cfg.model, cfg.effort), ("gpt-6", "high"))
            os.environ["CWA_MODEL"] = "gpt-6-instant"
            with self.assertRaisesRegex(ValueError, "pinned"):
                Config.from_env()
            os.environ["CWA_MODEL"] = "gpt-6"
            os.environ["CWA_REASONING_EFFORT"] = "medium"
            with self.assertRaisesRegex(ValueError, "pinned"):
                Config.from_env()

    def test_chat_registry_persists_two_separate_conversations(self):
        with tempfile.TemporaryDirectory() as folder:
            file = str(Path(folder) / "work-chats.json")
            store = ChatRegistry(file)
            self.assertIsNone(store.get("developer"))
            store.remember("developer", "dev-abc")
            store.remember("coordinator", "ragna-work-abc")
            self.assertEqual(ChatRegistry(file).get("developer"), "dev-abc")
            self.assertEqual(ChatRegistry(file).get("coordinator"), "ragna-work-abc")
            with self.assertRaisesRegex(ValueError, "changed"):
                store.remember("developer", "unexpected-new-chat")

    def test_both_new_chats_create_on_gpt6_high_then_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            c, d = Mock(), Mock()
            c.send.side_effect = [
                response('{"action":"delegate","instruction":"make branch"}', "coord-1"),
                response('{"action":"finish","message":"Review the diff."}', "coord-1"),
                response('{"action":"delegate","instruction":"next task"}', "coord-1"),
            ]
            d.send.side_effect = [
                response('{"status":"ready","repository":"owner/demo",'
                         '"branch":"agentrelay/work-1","commit_sha":"' + "a"*40 +
                         '","summary":"Committed."}', "dev-1"),
                response('{"status":"blocked","repository":"owner/demo",'
                         '"branch":"","commit_sha":"","summary":"No auth."}', "dev-1"),
            ]
            registry = ChatRegistry(str(Path(folder)/"chats.json"))
            def send(prompt, *, client, **kwargs):
                self.assertEqual(kwargs["model"], "gpt-6")
                self.assertEqual(kwargs["reasoning_effort"], "high")
                return client.send(prompt, **kwargs)
            worker = BackgroundDeveloper(c, d, registry, send)
            report = worker.run(work_id=1, project="demo", repo="owner/demo",
                                task="test", permitted=lambda: True)
            self.assertEqual(report.status, "ready")
            self.assertNotIn("conversation", c.send.call_args.kwargs)
            self.assertNotIn("conversation", d.send.call_args.kwargs)
            review = worker.review(work_id=1, project="demo", repo="owner/demo",
                                   task="test", report="confirmed", permitted=lambda: True)
            self.assertEqual(review, "Review the diff.")
            worker.run(work_id=2, project="demo", repo="owner/demo",
                       task="new", permitted=lambda: True)
            self.assertEqual(c.send.call_args.kwargs["conversation"], "coord-1")
            self.assertEqual(d.send.call_args.kwargs["conversation"], "dev-1")
            self.assertEqual(ChatRegistry(str(Path(folder)/"chats.json")).ids,
                             {"coordinator": "coord-1", "developer": "dev-1"})

    def test_task_branch_identity_is_exact_not_existing_branch(self):
        with tempfile.TemporaryDirectory() as folder:
            coordinator, developer = Mock(), Mock()
            coordinator.send.return_value = response(
                '{"action":"delegate","instruction":"Fix code"}', 'coord-1')
            developer.send.return_value = response(
                '{"status":"ready","repository":"owner/demo",'
                '"branch":"main","commit_sha":"' + 'a'*40 + '"'
                ',"summary":"Existing branch is not a new commit"}', 'dev-1')
            store = ChatRegistry(str(Path(folder) / 'chats.json'))
            runner = BackgroundDeveloper(
                coordinator, developer, store,
                lambda prompt, *, client, **kwargs: client.send(prompt, **kwargs))
            with self.assertRaisesRegex(ValueError, 'different branch'):
                runner.run(work_id=3, project='demo', repo='owner/demo',
                           task='Fix', permitted=lambda: True)
            self.assertIn('agentrelay/work-3', developer.send.call_args.args[0])

    def test_cwa_changed_conversation_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            coordinator, developer = Mock(), Mock()
            registry = ChatRegistry(str(Path(folder) / 'chats.json'))
            registry.remember('coordinator', 'coord-original')
            coordinator.send.return_value = response(
                '{"action":"delegate","instruction":"do work"}', 'unexpected-conversation')
            worker = BackgroundDeveloper(
                coordinator, developer, registry,
                lambda prompt, *, client, **kwargs: client.send(prompt, **kwargs))
            with self.assertRaisesRegex(ValueError, 'different conversation ID'):
                worker.run(work_id=1, project='demo', repo='owner/demo',
                           task='fix', permitted=lambda: True)
            self.assertEqual(registry.get('coordinator'), 'coord-original')
            developer.send.assert_not_called()

    def test_strict_worker_evidence_prevents_untrusted_repo_or_fake_sha(self):
        good = json.dumps({"status":"ready","repository":"owner/demo",
                           "branch":"feat/fix", "commit_sha":"a"*40,
                           "summary":"One commit"})
        self.assertEqual(parse_developer(good, expected_repo="owner/demo").status, "ready")
        for raw in (
            good.replace("owner/demo", "evil/repo"),
            good.replace("a"*40, "bad"),
            good.replace("feat/fix", "--upload-pack=oops"),
            good[:-1] + ', "command":"rm"}',
            '{"status":"blocked","repository":"owner/demo","branch":"feat/x",'
            '"commit_sha":"","summary":"blocked"}',
        ):
            with self.assertRaises(ValueError):
                parse_developer(raw, expected_repo="owner/demo")
        with self.assertRaises(ValueError):
            parse_repositories("demo=untrusted")
        self.assertEqual(parse_repositories("demo=owner/demo"), {"demo":"owner/demo"})

    def test_remote_origin_is_checked_before_branch_verification(self):
        self.assertTrue(_github_origin_matches("https://github.com/owner/demo.git", "owner/demo"))
        self.assertTrue(_github_origin_matches("git@github.com:owner/demo.git", "owner/demo"))
        self.assertFalse(_github_origin_matches("https://github.com/evil/demo.git", "owner/demo"))
        with tempfile.TemporaryDirectory() as folder:
            with patch("github_work.subprocess.run") as run:
                run.side_effect = [Mock(returncode=0, stdout="https://github.com/owner/demo.git\n"),
                                   Mock(returncode=0, stdout="a"*40 + "\trefs/heads/feat/x\n")]
                self.assertTrue(verify_remote_branch(Path(folder), "owner/demo", "feat/x", "a"*40))
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[0][-2:],
                                 ["origin", "refs/heads/feat/x"])

    def test_background_work_does_not_block_main_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coordinator, developer = self.app(folder)
            entered, release = threading.Event(), threading.Event()
            coordinator.send.side_effect = [
                response('{"action":"delegate","instruction":"create branch"}', "coord-1"),
                response('{"action":"finish","message":"Verified commit; inspect it."}', "coord-1"),
            ]
            def work_send(*args, **kwargs):
                entered.set()
                release.wait(3)
                return response('{"status":"ready","repository":"owner/demo",'
                                '"branch":"agentrelay/work-1","commit_sha":"' + "a"*40 +
                                '","summary":"Committed."}', "dev-1")
            developer.send.side_effect = work_send
            with patch("agent_relay.verify_remote_branch", return_value=True) as verified:
                q = Queue()
                self.assertTrue(app._handle_work_command(self.message(
                    "/work@MyBot start demo Create a branch"), q))
                self.assertTrue(entered.wait(2))
                self.assertTrue(q.empty())  # did not occupy normal CWA queue
                main.send.return_value = response("I can still chat.", "main-ragna-chat")
                answer = app._cwa_send("ordinary chat", purpose="chat",
                                       conversation="main-ragna-chat")
                self.assertEqual(answer.text, "I can still chat.")
                self.assertFalse(release.is_set())
                release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and app.work.active(1):
                    time.sleep(0.02)
                self.assertEqual(app.work.record["status"], "completed")
                verified.assert_called_once()
            self.assertEqual(main.send.call_args.kwargs["model"], "gpt-6")
            self.assertEqual(coordinator.send.call_args.kwargs["reasoning_effort"], "high")
            self.assertEqual(developer.send.call_args.kwargs["model"], "gpt-6")

    def test_stop_during_worker_prevents_check_or_response(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coordinator, developer = self.app(folder)
            entered, release = threading.Event(), threading.Event()
            coordinator.send.return_value = response(
                '{"action":"delegate","instruction":"do work"}', "coord-1")
            def pending(*args, **kwargs):
                entered.set()
                release.wait(3)
                return response('{"status":"ready","repository":"owner/demo",'
                                '"branch":"agentrelay/work-1","commit_sha":"' + "a"*40 +
                                '","summary":"Done"}', "dev-1")
            developer.send.side_effect = pending
            q = Queue()
            with patch("agent_relay.verify_remote_branch") as verify:
                app._handle_work_command(self.message("/work@MyBot start demo Work"), q)
                self.assertTrue(entered.wait(2))
                app._handle_work_command(self.message("/work@MyBot stop"), q)
                release.set()
                time.sleep(0.15)
                verify.assert_not_called()
                self.assertEqual(app.work.record["status"], "stopped")

    def test_optional_local_verification_is_codex_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coordinator, developer = self.app(folder, verify=True)
            coordinator.send.side_effect = [
                response('{"action":"delegate","instruction":"code"}', "c"),
                response('{"action":"finish","message":"Reviewed."}', "c"),
            ]
            developer.send.return_value = response(
                '{"status":"ready","repository":"owner/demo",'
                '"branch":"agentrelay/work-1","commit_sha":"' + "a"*40 +
                '","summary":"One commit"}', "d")
            q = Queue()
            with patch("agent_relay.verify_remote_branch", return_value=True), \
                 patch("agent_relay.checkout_verified_commit") as checkout, \
                 patch("local_codex.CodexRunner.run", return_value=CodexResult(
                     True, "3 tests passed", 0)) as codex:
                app._handle_work_command(self.message("/work@MyBot start demo Test"), q)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and app.work.active(1):
                    time.sleep(0.02)
                self.assertEqual(app.work.record["status"], "completed")
                checkout.assert_called_once()
                self.assertEqual(codex.call_args.kwargs["sandbox"], "read-only")


if __name__ == "__main__":
    unittest.main()
