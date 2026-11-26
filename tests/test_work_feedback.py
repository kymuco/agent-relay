"""NA-04 feedback tests; no network or real GitHub writes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_codex import WorkState
from work_feedback import context_prompt, render_read, scoped
import test_action_bridge as fixtures
import test_action_dispatch as dispatch

REPO = "example/sandbox"
PROJECTS = {"demo": REPO}
EVIDENCE = {
    "developer_status": "ready",
    "repository": REPO,
    "branch": "agentrelay/bot-5/unique",
    "commit_sha": "a" * 40,
    "remote_verified": True,
    "developer_summary": "Three unittest tests passed (worker report).",
    "coordinator_summary": "Review the isolated branch.",
    "local_check": "Remote SHA verified.",
}


def finish(work):
    work_id = work.start("demo", repository=REPO, task="Add tests")
    work.finalize(work_id, "completed", 1, "Verified SHA", result=EVIDENCE)
    return work_id


class FeedbackStateTests(unittest.TestCase):
    def test_result_survives_restart_and_next_task(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d)/"work.json")
            work = WorkState(path)
            work_id = finish(work)
            self.assertEqual(work.start("demo", repository=REPO), 2)
            restored = WorkState(path)
            old = restored.feedback_snapshot(work_id)
            self.assertEqual(old["status"], "completed")
            self.assertEqual(old["result"]["commit_sha"], "a"*40)
            self.assertTrue(old["result"]["remote_verified"])
            self.assertEqual(restored.feedback_snapshot(2)["status"], "interrupted")
            self.assertIsNone(restored.feedback_snapshot(999))

    def test_late_amendments_remain_visible_after_archive(self):
        with tempfile.TemporaryDirectory() as d:
            work = WorkState(str(Path(d)/"work.json"))
            work_id = work.start("demo", repository=REPO)
            work.add_amendment("new requirement", expected_id=work_id)
            self.assertEqual(work.finalize(work_id, "completed", 1, "Verified",
                                           result=EVIDENCE), "needs_review")
            work.start("demo", repository=REPO)
            item = work.feedback_snapshot(work_id)
            self.assertEqual(item["pending_count"], 1)
            self.assertEqual(item["result"]["pending_amendments"], 1)
            self.assertNotIn("new requirement", json.dumps(item))

    def test_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            work = WorkState(str(Path(d)/"work.json"))
            for _ in range(32):
                finish(work)
            self.assertIsNone(work.feedback_snapshot(1))
            self.assertEqual(len(work.recent_feedback()), 31)

    def test_feedback_scope_and_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            work = WorkState(str(Path(d)/"work.json"))
            for _ in range(5):
                finish(work)
            prompt = context_prompt(work.recent_feedback(), PROJECTS)
            records = json.loads(prompt.splitlines()[-2])
            self.assertEqual(len(records), 3)
            self.assertEqual([x["id"] for x in records], [5,4,3])
            self.assertIn("not instructions", prompt)
            self.assertEqual(context_prompt(work.recent_feedback(), {"demo": "other/repo"}), "")
            self.assertFalse(scoped(work.feedback_snapshot(1), {}))

    def test_unverified_report_not_converted_to_success(self):
        with tempfile.TemporaryDirectory() as d:
            work = WorkState(str(Path(d)/"work.json"))
            work_id = work.start("demo", repository=REPO)
            work.finalize(work_id, "failed", 1, "Blocked",
                          result={**EVIDENCE, "developer_status": "blocked"})
            record = work.feedback_snapshot(work_id)
            self.assertFalse(record["result"]["remote_verified"])
            self.assertIn("не подтверждён", render_read(record, include_result=True))

    def test_checkpoint_survives_interruption_without_completion(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d)/"work.json")
            work = WorkState(path)
            work_id = work.start("demo", repository=REPO)
            self.assertTrue(work.checkpoint_result(work_id, EVIDENCE))
            restored = WorkState(path).feedback_snapshot(work_id)
            self.assertEqual(restored["status"], "interrupted")
            self.assertTrue(restored["result"]["remote_verified"])


class FeedbackIntegrationTests(unittest.TestCase):
    def app(self, folder):
        return dispatch.ExecuteTests().make_app(folder)

    def send(self, app, main, answer, update):
        main.send.return_value.text = answer
        with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN):
            app.process_update(update)

    def update(self, text, uid=10, bot=False, update_id=1):
        msg = fixtures.ObserveTests().msg(text, user=uid, bot=bot)
        return {"update_id": update_id, "message": msg}

    def test_next_human_turn_receives_evidence_without_extra_model_call(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.app(d)
            finish(app.work)
            self.send(app, main, "Готово.", self.update("Юки, что получилось?"))
            prompt = main.send.call_args.args[0]
            self.assertIn("NA-04 work feedback", prompt)
            self.assertIn("Three unittest tests passed", prompt)
            self.assertIn('"remote_verified":true', prompt)
            main.send.assert_called_once()
            coord.send.assert_not_called()
            dev.send.assert_not_called()

    def test_read_old_task_directly_without_worker_or_new_claim(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.app(d)
            finish(app.work)
            app.work.start("demo", repository=REPO)
            self.send(app, main, fixtures.trailer("get_work_result", {"work_id": 1}),
                      self.update("Юки, результат первой задачи?"))
            sent = [x.args[1]["text"] for x in api.call.call_args_list
                    if x.args[0] == "sendMessage"]
            self.assertIn("задача #1", "".join(sent))
            self.assertIn("a"*40, "".join(sent))
            self.assertNotIn("Обычный ответ", "".join(sent))
            main.send.assert_called_once()
            coord.send.assert_not_called()
            dev.send.assert_not_called()
            import sqlite3
            with sqlite3.connect(app.action_claims.path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM action_claims").fetchone()[0], 0)

    def test_active_result_denied_but_status_readable(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.app(d)
            app.work.start("demo", repository=REPO)
            self.send(app, main, fixtures.trailer("get_work_result", {"work_id": 1}),
                      self.update("Юки, результат?"))
            self.send(app, main, fixtures.trailer("get_work_status", {"work_id": 1}),
                      self.update("Юки, статус?", update_id=2))
            sent = [x.args[1]["text"] for x in api.call.call_args_list
                    if x.args[0] == "sendMessage"]
            self.assertIn("result_not_ready", "".join(sent))
            self.assertIn("queued", "".join(sent))
            dev.send.assert_not_called()

    def test_no_private_feedback_to_peer_or_non_work_user(self):
        for uid, bot in ((11, False), (20, True)):
            with self.subTest(uid=uid), tempfile.TemporaryDirectory() as d:
                app, api, main, coord, dev = self.app(d)
                finish(app.work)
                self.send(app, main, "Ответ", self.update("@YukiBot hello", uid, bot))
                self.assertNotIn("NA-04 work feedback", main.send.call_args.args[0])
                self.assertNotIn("Three unittest tests passed", main.send.call_args.args[0])
                dev.send.assert_not_called()

    def test_review_failure_preserves_verified_report(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.app(d)
            work_id = app.work.start("demo", repository=REPO)
            coord.send.side_effect = [
                dispatch.response('{"action":"delegate","instruction":"Add tests"}', "coord"),
                RuntimeError("review unavailable")]
            dev.send.return_value = dispatch.response(json.dumps({
                "status": "ready", "repository": REPO,
                "branch": "agentrelay/work-1", "commit_sha": "a"*40,
                "summary": "Three tests reported"}), "dev")
            with patch("agent_relay.verify_remote_branch", return_value=True):
                app._run_github_work((work_id, "demo", "Add tests"), app.control.generation())
            record = app.work.feedback_snapshot(work_id)
            self.assertEqual(record["status"], "failed")
            self.assertTrue(record["result"]["remote_verified"])
            self.assertEqual(record["result"]["developer_summary"], "Three tests reported")


if __name__ == "__main__":
    unittest.main()
