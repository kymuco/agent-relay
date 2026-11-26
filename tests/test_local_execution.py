"""NA-06.4: genuine local unittest processes and native Host routing."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_execution import parse_local_tests, should_run_local, run_local_tests, execution_record
from work_feedback import result_record
from work_card_views import render_work_card
import test_action_bridge as fixtures
import test_action_dispatch as dispatch

SHA = "a"*40
REPO = "example/sandbox"
IDENTITY = "bot-5-work-1-" + "b"*32
STAMP = "2026-11-26T12:00:00+00:00"
WORKSPACE = dict(status="prepared", commit_sha=SHA, workspace_id=IDENTITY,
                 reason="", observed_at=STAMP)


def observed(status="passed"):
    return dict(status=status, commit_sha=SHA, workspace_id=IDENTITY,
                preset="unittest", exit_code=0 if status=="passed" else 1,
                tests_run=3, elapsed_ms=12, observed_at=STAMP)


class LocalRunnerTests(unittest.TestCase):
    def test_operator_presets_and_ci_routing(self):
        projects = {"demo": Path("."), "other": Path(".")}
        self.assertEqual(parse_local_tests("demo=unittest;other=pytest", projects),
                         {"demo": "unittest", "other": "pytest"})
        for raw in ("demo=cmd", "unknown=unittest", "demo=unittest;demo=pytest",
                    "demo=unittest;"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_local_tests(raw, projects)
        for state in ("not_configured", "not_run", "unavailable"):
            self.assertTrue(should_run_local({"status": state}))
        for state in ("passed", "failed", "pending", "incomplete", "unknown"):
            self.assertFalse(should_run_local({"status": state}))
        self.assertTrue(should_run_local(None))

    def run_fixture(self, source_text=None, *, timeout=10):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d).resolve()
            if source_text is not None:
                (directory/"test_example.py").write_text(source_text, encoding="utf-8")
            with patch("local_execution.verify_prepared_workspace", return_value=directory), \
                 patch.dict(os.environ, {"WORK_PYTHON": "", "WORK_CI_TOKEN": "not-for-child",
                                         "TELEGRAM_BOT_TOKEN": "not-for-child"}):
                return run_local_tests(directory, directory, REPO, SHA, WORKSPACE,
                                       "unittest", timeout=timeout, permitted=lambda: True)

    def test_real_nonzero_unittest_with_restricted_environment(self):
        result = self.run_fixture(
            "import unittest, os\n"
            "class Tests(unittest.TestCase):\n"
            "    def test_one(self): self.assertEqual(1+1, 2)\n"
            "    def test_two(self): self.assertNotIn('TELEGRAM_BOT_TOKEN', os.environ)\n"
            "    def test_three(self): self.assertNotIn('WORK_CI_TOKEN', os.environ)\n")
        self.assertEqual((result["status"], result["exit_code"], result["tests_run"]),
                         ("passed", 0, 3), result)
        self.assertEqual(execution_record(result, WORKSPACE, SHA), result)

    def test_real_failed_empty_and_premature_exit_not_success(self):
        failed = self.run_fixture("import unittest\nclass Tests(unittest.TestCase):\n"
                                  "    def test_fail(self): self.assertTrue(False)\n")
        self.assertEqual((failed["status"], failed["exit_code"], failed["tests_run"]),
                         ("failed", 1, 1), failed)
        empty = self.run_fixture()
        self.assertEqual((empty["status"], empty["exit_code"], empty["tests_run"]),
                         ("no_tests", 5, 0), empty)
        premature = self.run_fixture("raise SystemExit(0)\n")
        self.assertNotEqual(premature["status"], "passed")

    def test_timeout_and_output_limit(self):
        timed = self.run_fixture("import time\ntime.sleep(10)\n", timeout=0)
        self.assertEqual(timed["status"], "timeout")
        excessive = self.run_fixture("print('x' * (2 * 1024 * 1024))\n")
        self.assertEqual(excessive["status"], "output_limit")

    def test_stopped_or_unverified_worktree_does_not_launch(self):
        source = Path(".").resolve()
        with patch("local_execution.verify_prepared_workspace") as verify, \
             patch("local_execution.subprocess.Popen") as proc:
            self.assertIsNone(run_local_tests(source, source, REPO, SHA, WORKSPACE,
                              "unittest", timeout=1, permitted=lambda: False))
            verify.assert_not_called()
            verify.return_value = None
            result = run_local_tests(source, source, REPO, SHA, WORKSPACE,
                                     "unittest", timeout=1, permitted=lambda: True)
            self.assertEqual(result["status"], "unavailable")
            proc.assert_not_called()

    def test_evidence_rejects_mismatch_and_zero_test_success(self):
        data = observed()
        self.assertIsNone(execution_record(dict(data, tests_run=0), WORKSPACE, SHA))
        self.assertIsNone(execution_record(dict(data, commit_sha="c"*40), WORKSPACE, SHA))
        self.assertIsNone(execution_record(dict(data, workspace_id="bogus"), WORKSPACE, SHA))
        result = dict(developer_status="ready", repository=REPO, branch="test",
                      commit_sha=SHA, remote_verified=True, workspace=WORKSPACE, execution=data)
        self.assertEqual(result_record(result, 0)["execution"], data)
        self.assertNotIn("execution", result_record(dict(result, remote_verified=False), 0))
        self.assertIn("Local Runner (unittest)", render_work_card(
            dict(work_id=1, phase="completed", project="demo", result=result)))


class LocalIntegrationTests(unittest.TestCase):
    def test_ci_fallback_and_separate_evidence(self):
        scenarios = [
            ("not_configured", "passed", True, "completed"),
            ("unavailable", "passed", True, "completed"),
            ("not_configured", "failed", True, "needs_review"),
            ("passed", "passed", False, "completed"),
            ("failed", "passed", False, "needs_review"),
            ("pending", "passed", False, "needs_review"),
        ]
        for state, outcome, runs, expected in scenarios:
            with self.subTest(state=state, outcome=outcome), tempfile.TemporaryDirectory() as d:
                base = Path(d).resolve()
                source, root = base/"source", base/"root"
                source.mkdir()
                root.mkdir()
                fixture = dispatch.ExecuteTests()
                app, api, main, coord, dev = fixture.make_app(
                    str(source), work_workspace_root=str(root), work_local_tests="demo=unittest",
                    work_ci_verify=True, work_ci_wait_seconds=0)
                main.send.return_value.text = fixtures.trailer(args={
                    "project": "demo", "task": "Add tests", "delegation": "direct"})
                with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
                     patch("agent_relay.threading.Thread") as thread:
                    app.process_update(fixture.update())
                    worker = thread.call_args.kwargs["target"]
                branch = app.work.record["branch"]
                dev.send.return_value = dispatch.response(json.dumps({
                    "status": "ready", "repository": REPO, "branch": branch,
                    "commit_sha": SHA, "summary": "3 tests passed"}), "dev")
                ids = [1] if state in ("passed", "failed", "pending") else []
                ci = dict(status=state, commit_sha=SHA, run_count=len(ids), run_ids=ids,
                          observed_at=STAMP)
                with patch("agent_relay.verify_remote_branch", return_value=True), \
                     patch("agent_relay.observe_ci", return_value=ci), \
                     patch("agent_relay.prepare_workspace", return_value=WORKSPACE), \
                     patch("agent_relay.run_local_tests", return_value=observed(outcome)) as execute, \
                     patch("local_codex.CodexRunner.run") as codex:
                    worker()
                self.assertEqual(execute.call_count, int(runs))
                codex.assert_not_called()
                coord.send.assert_not_called()
                snap = app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"], expected)
                self.assertEqual(snap["result"]["ci"], ci)
                self.assertTrue(snap["result"]["remote_verified"])
                if runs:
                    self.assertEqual(snap["result"]["execution"], observed(outcome))
                    self.assertEqual(app.progress.record["result"]["execution"], observed(outcome))
                else:
                    self.assertNotIn("execution", snap["result"])
                self.assertFalse(app._native_worker_lock.locked())


if __name__ == "__main__":
    unittest.main()
