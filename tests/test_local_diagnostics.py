"""NA-06.5: diagnostic scope, evidence and Host routing without a real model call."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from local_codex import CodexResult
from local_diagnostics import (parse_diagnostic_projects, should_diagnose,
                               diagnose_local_failure, diagnostic_record)
from work_feedback import result_record, render_read
from work_card_views import render_work_card, work_keyboard
import test_action_bridge as fixtures
import test_action_dispatch as dispatch
from test_local_execution import SHA, REPO, IDENTITY, STAMP, WORKSPACE, observed

REPORT = {"category": "code", "summary": "Assertion differs from implementation.",
          "recommendation": "Review conversion formula."}
FAILURE = observed("failed")


class DiagnosticsTests(unittest.TestCase):
    def call(self, runner, *, workspace_ok=True, permitted=lambda: True, output="AssertionError: 1 != 2"):
        directory = Path(".").resolve()
        with patch("local_diagnostics.verify_prepared_workspace",
                   return_value=directory if workspace_ok else None):
            return diagnose_local_failure(directory, directory, REPO, SHA, WORKSPACE,
                                          FAILURE, output, runner=runner, timeout=30,
                                          permitted=permitted)

    def test_explicit_scope_and_routing(self):
        self.assertEqual(parse_diagnostic_projects("demo", {"demo": "unittest"}), {"demo"})
        self.assertEqual(parse_diagnostic_projects("", {}), set())
        for value in ("other", "demo,demo", "demo,"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_diagnostic_projects(value, {"demo": "unittest"})
        for state in ("failed", "no_tests", "timeout", "output_limit", "unavailable"):
            self.assertTrue(should_diagnose({"status": state}))
        self.assertFalse(should_diagnose(observed()))
        self.assertFalse(should_diagnose(None))

    def test_read_only_report_and_secret_redaction(self):
        runner = Mock()
        runner.run.return_value = CodexResult(True, json.dumps(REPORT), 0)
        with patch.dict(os.environ, {"WORK_CI_TOKEN": "sensitive-example-value"}):
            result = self.call(runner, output="AssertionError sensitive-example-value")
        self.assertEqual(result["status"], "reported")
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(result["summary"], REPORT["summary"])
        runner.run.assert_called_once()
        self.assertEqual(runner.run.call_args.kwargs, {"timeout": 30, "sandbox": "read-only"})
        prompt = runner.run.call_args.args[1]
        self.assertIn(SHA, prompt)
        self.assertIn("AssertionError [redacted]", prompt)
        self.assertNotIn("sensitive-example-value", prompt)
        self.assertIn("Do not change any files", prompt)
        self.assertIn("Do not claim any fix", prompt)
        self.assertEqual(diagnostic_record(result, FAILURE, WORKSPACE, SHA), result)

    def test_bad_reports_and_unavailable_codex_preserve_failure(self):
        runner = Mock()
        for reply, reason in (
            (CodexResult(False, "not authenticated", 1), "codex_failed"),
            (CodexResult(True, "not JSON", 0), "invalid_report"),
            (CodexResult(True, json.dumps(dict(REPORT, action="repair")), 0), "invalid_report"),
            (CodexResult(True, '{"category":"code","category":"test","summary":"x","recommendation":"y"}', 0), "invalid_report"),
        ):
            runner.run.return_value = reply
            result = self.call(runner)
            self.assertEqual((result["status"], result["reason"]), ("unavailable", reason))
        runner.run.side_effect = RuntimeError("Codex not installed")
        result = self.call(runner)
        self.assertEqual(result["reason"], "codex_unavailable")
        self.assertNotIn("Codex not installed", json.dumps(result))

    def test_stop_and_changed_workspace_prevent_diagnosis(self):
        runner = Mock()
        self.assertIsNone(self.call(runner, permitted=lambda: False))
        self.assertEqual(self.call(runner, workspace_ok=False)["reason"], "workspace_changed")
        runner.run.assert_not_called()
        runner.run.return_value = CodexResult(True, json.dumps(REPORT), 0)
        directory = Path(".").resolve()
        with patch("local_diagnostics.verify_prepared_workspace", side_effect=[directory, None]):
            result = diagnose_local_failure(directory, directory, REPO, SHA, WORKSPACE,
                                           FAILURE, "", runner=runner, timeout=30,
                                           permitted=lambda: True)
        self.assertEqual(result["reason"], "workspace_changed")
        active = [True]
        def stop(*args, **kwargs):
            active[0] = False
            return CodexResult(True, json.dumps(REPORT), 0)
        runner.run.side_effect = stop
        self.assertIsNone(self.call(runner, permitted=lambda: active[0]))

    def test_report_is_bound_to_failed_execution_and_rendered_as_advice(self):
        runner = Mock()
        runner.run.return_value = CodexResult(True, json.dumps(REPORT), 0)
        diagnostic = self.call(runner)
        self.assertIsNone(diagnostic_record(diagnostic, observed(), WORKSPACE, SHA))
        self.assertIsNone(diagnostic_record(dict(diagnostic, commit_sha="c"*40), FAILURE, WORKSPACE, SHA))
        data = dict(developer_status="ready", repository=REPO, branch="test", commit_sha=SHA,
                    remote_verified=True, workspace=WORKSPACE, execution=FAILURE, diagnostic=diagnostic)
        saved = result_record(data, 0)
        self.assertEqual(saved["diagnostic"], diagnostic)
        self.assertEqual(saved["execution"]["status"], "failed")
        self.assertNotIn("diagnostic", result_record(dict(data, remote_verified=False), 0))
        card = dict(work_id=1, phase="needs_review", project="demo", result=saved)
        self.assertIn("Codex Diagnostics", render_work_card(card))
        self.assertIn(REPORT["summary"], render_work_card(card, detail=True))
        self.assertTrue(any(b.get("callback_data") == "wp:s:1"
                            for row in work_keyboard(1, False, {}, phase="diagnostics")["inline_keyboard"]
                            for b in row))


class DiagnosticsIntegrationTests(unittest.TestCase):
    def test_native_diagnostics_only_for_opted_in_failure_never_repairs(self):
        for enabled, outcome, expected in ((True, "failed", True), (True, "passed", False),
                                            (False, "failed", False)):
            with self.subTest(enabled=enabled, outcome=outcome), tempfile.TemporaryDirectory() as d:
                base = Path(d).resolve()
                source, root = base/"source", base/"root"
                source.mkdir()
                root.mkdir()
                fixture = dispatch.ExecuteTests()
                app, api, main, coord, dev = fixture.make_app(
                    str(source), work_workspace_root=str(root), work_local_tests="demo=unittest",
                    work_codex_diagnostics="demo" if enabled else "",
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
                    "commit_sha": SHA, "summary": "Developer report"}), "dev")
                ci = dict(status="not_configured", commit_sha=SHA, run_count=0, run_ids=[],
                          observed_at=STAMP)
                def execute(*args, **kwargs):
                    if "failure_output" in kwargs and outcome == "failed":
                        kwargs["failure_output"].append("AssertionError: test fixture")
                    return observed(outcome)
                with patch("agent_relay.verify_remote_branch", return_value=True), \
                     patch("agent_relay.observe_ci", return_value=ci), \
                     patch("agent_relay.prepare_workspace", return_value=WORKSPACE), \
                     patch("agent_relay.run_local_tests", side_effect=execute), \
                     patch("local_diagnostics.verify_prepared_workspace", return_value=root), \
                     patch("local_codex.CodexRunner.run",
                           return_value=CodexResult(True, json.dumps(REPORT), 0)) as codex:
                    worker()
                self.assertEqual(codex.call_count, int(expected))
                dev.send.assert_called_once()
                coord.send.assert_not_called()
                snap = app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"], "completed" if outcome == "passed" else "needs_review")
                self.assertEqual(snap["result"]["execution"], observed(outcome))
                self.assertEqual(snap["result"]["ci"], ci)
                if expected:
                    self.assertIn("AssertionError: test fixture", codex.call_args.args[1])
                    self.assertEqual(snap["result"]["diagnostic"]["status"], "reported")
                    self.assertEqual(app.progress.record["result"]["diagnostic"],
                                     snap["result"]["diagnostic"])
                    self.assertNotIn("AssertionError: test fixture", json.dumps(snap))
                    self.assertIn("Codex Diagnostics", render_read(snap, include_result=True))
                else:
                    self.assertNotIn("diagnostic", snap["result"])
                self.assertFalse(app._native_worker_lock.locked())


if __name__ == "__main__":
    unittest.main()
