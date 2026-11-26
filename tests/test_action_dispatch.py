"""NA-03: live dispatch contract, using mocked connectors only."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from action_bridge import parse_action_response
from action_dispatch import DispatchClaims
from action_policy import Source, request_digest
from agent_relay import AgentRelay
from github_work import BackgroundDeveloper, ChatRegistry
import test_action_bridge as fixtures


def response(text, cid):
    return Mock(text=text, conversation=Mock(conversation_id=cid))


class ClaimsTests(unittest.TestCase):
    def test_claim_survives_restart_and_blocks_changed_update_or_payload(self):
        with tempfile.TemporaryDirectory() as d:
            filename = str(Path(d) / "claims.sqlite3")
            source = Source(5, -123, 1, 41, 10, 0, request_digest("do it"))
            self.assertTrue(DispatchClaims(filename).claim(source, "first"))
            store = DispatchClaims(filename)
            self.assertFalse(store.claim(source, "different"))
            self.assertFalse(store.claim(replace(source, update_id=2), "different"))
            self.assertFalse(store.claim(replace(source, message_id=42), "different"))
            self.assertTrue(store.claim(replace(source, update_id=2, message_id=42), "next"))

    def test_mixed_handoff_and_quoted_action_rejected(self):
        for prefix in ("[[handoff:OtherBot]]\n", "Example:\n" + chr(96)*3 + "\n",
                       "Reply\n/relay@OtherBot hello\n"):
            with self.subTest(prefix=prefix):
                value = parse_action_response(prefix + fixtures.trailer(),
                    token=fixtures.TOKEN, projects={"demo": "example/sandbox"})
                self.assertEqual(value.status, "rejected")


class ExecuteTests(unittest.TestCase):
    def make_app(self, folder, **changes):
        fixture = fixtures.ObserveTests()
        old, api, main, coord, dev = fixture.make_app(folder)
        settings = dict(action_mode="execute", action_execute_ack=True,
                        action_auto_projects=frozenset({"demo"}),
                        action_claim_file=str(Path(folder)/"claims.sqlite3"))
        settings.update(changes)
        app = AgentRelay(replace(old.config, **settings), api, main, coord, dev)
        return app, api, main, coord, dev

    def update(self, text="Юки, создай модуль в demo", **extra):
        msg = fixtures.ObserveTests().msg(text)
        msg.update(extra)
        return {"update_id": 1, "message": msg}

    def test_execute_requires_explicit_operator_activation(self):
        for changes in ({"action_execute_ack": False},
                        {"action_observe_read_only_ack": False},
                        {"action_auto_projects": frozenset()},
                        {"work_local_verify": True}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):
                    self.make_app(d, **changes)

    def test_allowed_action_reserves_once_and_runs_existing_worker(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            main.send.return_value.text = fixtures.trailer()
            event = self.update()
            with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
                 patch("agent_relay.threading.Thread") as thread:
                app.process_update(event)
                app.process_update(event)
                thread.return_value.start.assert_called_once()
                run = thread.call_args.kwargs["target"]
            self.assertEqual(app.work.record["status"], "queued")
            branch = app.work.record["branch"]
            self.assertRegex(branch, r"^agentrelay/bot-5/[0-9a-f]{32}$")
            self.assertTrue(main.send.call_args.kwargs["verify_final_text"])
            coord.send.assert_not_called()
            dev.send.assert_not_called()
            coord.send.side_effect = [
                response('{"action":"delegate","instruction":"Add tests"}', "coord"),
                response('{"action":"finish","message":"Committed"}', "coord")]
            import json
            dev.send.return_value = response(json.dumps({
                "status": "ready", "repository": "example/sandbox", "branch": branch,
                "commit_sha": "a"*40, "summary": "Committed"}), "dev")
            with patch("agent_relay.verify_remote_branch", return_value=True) as verify:
                run()
            self.assertEqual(app.work.record["status"], "completed")
            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[2], branch)
            self.assertIn(branch, dev.send.call_args.args[0])
            self.assertTrue(dev.send.call_args.kwargs["verify_final_text"])
            self.assertFalse(app._native_worker_lock.locked())

    def test_direct_route_skips_both_coordinator_turns_and_verifies(self):
        import json
        for outcome in ("ready", "blocked", "wrong_sha", "stopped"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as d:
                app, api, main, coord, dev = self.make_app(d)
                main.send.return_value.text = fixtures.trailer(args={
                    "project": "demo", "task": "Add tests", "delegation": "direct"})
                event = self.update()
                with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
                     patch("agent_relay.threading.Thread") as thread:
                    app.process_update(event)
                    app.process_update(event)
                    thread.return_value.start.assert_called_once()
                    run = thread.call_args.kwargs["target"]
                branch = app.work.record["branch"]
                self.assertEqual(app.work.record["delegation"], "direct")
                self.assertEqual(app.work.record["status"], "queued")
                def answer(*args, **kwargs):
                    if outcome == "stopped":
                        app.control.stop()
                        app.work.stop(expected_id=1)
                    return response(json.dumps({
                        "status": "blocked" if outcome == "blocked" else "ready",
                        "repository": "example/sandbox",
                        "branch": "" if outcome == "blocked" else branch,
                        "commit_sha": "" if outcome == "blocked" else "a"*40,
                        "summary": "Worker report"}), "dev")
                dev.send.side_effect = answer
                with patch("agent_relay.verify_remote_branch",
                           return_value=outcome != "wrong_sha") as verify:
                    run()
                coord.send.assert_not_called()
                dev.send.assert_called_once()
                self.assertIn("Add tests", dev.send.call_args.args[0])
                self.assertIn(event["message"]["text"], dev.send.call_args.args[0])
                self.assertTrue(dev.send.call_args.kwargs["verify_final_text"])
                self.assertEqual(verify.call_count, int(outcome in ("ready", "wrong_sha")))
                expected = {"ready": "completed", "blocked": "failed",
                            "wrong_sha": "failed", "stopped": "stopped"}[outcome]
                self.assertEqual(app.work.record["status"], expected)
                self.assertFalse(app._native_worker_lock.locked())
                if outcome == "ready":
                    evidence = app.work.feedback_snapshot(1)["result"]
                    self.assertTrue(evidence["remote_verified"])
                    self.assertEqual(evidence["delegation"], "direct")
                    self.assertEqual(evidence["coordinator_summary"], "")

    def test_stop_after_reservation_prevents_worker_calls(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            main.send.return_value.text = fixtures.trailer()
            with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
                 patch("agent_relay.threading.Thread") as thread:
                app.process_update(self.update())
                run = thread.call_args.kwargs["target"]
            app.control.stop()
            app.control.resume()
            run()
            coord.send.assert_not_called()
            dev.send.assert_not_called()
            self.assertEqual(app.work.record["status"], "stopped")
            self.assertFalse(app._native_worker_lock.locked())

    def test_stop_during_model_turn_prevents_claim(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            def answer(*a, **kw):
                app.control.stop()
                app.control.resume()
                return Mock(text=fixtures.trailer())
            main.send.side_effect = answer
            with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN):
                app.process_update(self.update())
            self.assertEqual(app.work.record["status"], "idle")
            dev.send.assert_not_called()

    def test_forwarded_source_and_manual_start_cannot_bypass_policy(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            main.send.return_value.text = fixtures.trailer()
            with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN):
                app.process_update(self.update(forward_origin={"type": "hidden_user"}))
            self.assertTrue(app._handle_work_command(
                fixtures.ObserveTests().msg("/work@YukiBot start demo Test"), Queue()))
            self.assertEqual(app.work.record["status"], "idle")
            coord.send.assert_not_called()
            dev.send.assert_not_called()

    def test_thread_launch_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            main.send.return_value.text = fixtures.trailer()
            with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
                 patch("agent_relay.threading.Thread") as thread:
                thread.return_value.start.side_effect = RuntimeError("thread failed")
                app.process_update(self.update())
                thread.return_value.start.assert_called_once()
            self.assertEqual(app.work.record["status"], "failed")
            self.assertFalse(app._native_worker_lock.locked())
            dev.send.assert_not_called()

    def test_amendment_requires_active_exact_card(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            app.work.start("demo")
            app.progress.begin(1, "demo", "Test", reply_message_id=41)
            main.send.return_value.text = fixtures.trailer(
                "amend_work", {"work_id": 1, "task": "Add edge case"})
            event = self.update("Добавь тест", reply_to_message={
                "message_id": 301, "from": {"id": 5, "is_bot": True}})
            with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
                 patch("agent_relay.threading.Thread"):
                app.process_update(event)
            self.assertEqual(app.work.pending_snapshot(), ["Add edge case"])
            dev.send.assert_not_called()


class WorkerScopeTests(unittest.TestCase):
    def test_stop_during_phase_update_prevents_developer_send(self):
        with tempfile.TemporaryDirectory() as d:
            c, dev = Mock(), Mock()
            c.send.return_value = response('{"action":"delegate","instruction":"fix"}', "c")
            runner = BackgroundDeveloper(c, dev, ChatRegistry(str(Path(d)/"chats")),
                lambda prompt, *, client, **kw: client.send(prompt, **kw))
            allowed = [True]
            self.assertIsNone(runner.run(work_id=1, project="demo", repo="example/sandbox",
                task="fix", permitted=lambda: allowed[0],
                on_phase=lambda _: allowed.__setitem__(0, False)))
            dev.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
