"""Developer plans are data; only Host can authorize exact configured verification."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from github_work import parse_developer, developer_prompt
from verification_routing import (admit_plan, capabilities, parse_plan, review_reason,
                                  verification_record, wants_cloud, wants_local)
from work_feedback import result_record
import test_action_bridge as fixtures
import test_action_dispatch as dispatch
from test_local_execution import SHA, REPO, WORKSPACE, STAMP, observed

CAPS = capabilities(cloud_enabled=True, local_preset="unittest",
                    workspace_enabled=True, diagnostics_enabled=True)


def plan(route, preset=None):
    value = {"route": route, "reason": "Bounded verification required"}
    if preset is not None:
        value["preset"] = preset
    return value


class RoutingTests(unittest.TestCase):
    def test_strict_worker_contract_and_legacy_compatibility(self):
        base = dict(status="ready", repository=REPO, branch="task", commit_sha=SHA, summary="report")
        self.assertIsNone(parse_developer(json.dumps(base), expected_repo=REPO).verification)
        for route, preset in (("cloud", None), ("local", "unittest"), ("hybrid", "pytest"),
                              ("none", None), ("unavailable", None)):
            obj = plan(route, preset)
            self.assertEqual(parse_developer(json.dumps(dict(base, verification=obj)),
                                             expected_repo=REPO).verification, obj)
        for bad in (None, [], {}, plan("shell"), plan("cloud", "unittest"),
                    plan("local"), plan("local", "python -c evil"),
                    dict(plan("cloud"), command="run"), dict(plan("local", "unittest"), path="/tmp"),
                    dict(plan("none"), reason=""), dict(plan("cloud"), reason="x"*601)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_developer(json.dumps(dict(base, verification=bad)), expected_repo=REPO)
        with self.assertRaises(ValueError):
            parse_developer(json.dumps(dict(base, status="blocked", branch="", commit_sha="",
                                            verification=plan("none"))), expected_repo=REPO)

    def test_host_admission_is_independent_and_no_fallback(self):
        for route in ("cloud", "none", "unavailable"):
            record = admit_plan(plan(route), SHA, CAPS)
            self.assertFalse(wants_local(record, {"status": "not_configured"}))
        old = admit_plan(None, SHA, CAPS)
        self.assertTrue(wants_cloud(old, enabled=True))
        self.assertFalse(wants_local(old, {"status": "unavailable"}))
        self.assertEqual(review_reason(old, {"status": "not_configured"}, None), "cloud_not_passed")
        denied = admit_plan(plan("local", "pytest"), SHA, CAPS)
        self.assertEqual(denied["admission_reason"], "preset_not_authorized")
        self.assertFalse(wants_local(denied, None))
        empty = capabilities(cloud_enabled=True, local_preset="unittest",
                             workspace_enabled=False, diagnostics_enabled=True)
        self.assertEqual(empty["local_presets"], [])
        self.assertEqual(admit_plan(plan("local", "unittest"), SHA, empty)["admission_reason"],
                         "local_not_authorized")
        self.assertEqual(admit_plan(plan("cloud"), SHA, dict(CAPS, cloud_ci=False))["admission_reason"],
                         "cloud_disabled")
        hybrid = admit_plan(plan("hybrid", "unittest"), SHA, CAPS)
        self.assertFalse(wants_local(hybrid, {"status": "pending"}))
        self.assertTrue(wants_local(hybrid, {"status": "passed"}))
        self.assertEqual(review_reason(hybrid, {"status": "passed"}, None), "local_not_passed")
        self.assertEqual(review_reason(hybrid, {"status": "passed"}, observed()), "")

    def test_capabilities_are_scoped_and_plan_evidence_bound_to_sha(self):
        prompt = developer_prompt("Add tests", repo=REPO, work_id=1, verification_capabilities=CAPS)
        self.assertIn('"local_presets": ["unittest"]', prompt)
        self.assertIn("Absence of workflow", prompt)
        self.assertNotIn("WORK_WORKSPACE_ROOT", prompt)
        record = admit_plan(plan("local", "unittest"), SHA, CAPS)
        self.assertEqual(verification_record(record, SHA), record)
        self.assertIsNone(verification_record(record, "b"*40))
        data = dict(developer_status="ready", repository=REPO, branch="task", commit_sha=SHA,
                    remote_verified=True, verification=record)
        self.assertEqual(result_record(data, 0)["verification"], record)
        self.assertNotIn("verification", result_record(dict(data, remote_verified=False), 0))

    def test_denied_or_unsupported_plan_never_creates_worktree(self):
        for preset in ("pytest", "gpu_smoke_test"):
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as d:
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
                    "commit_sha": SHA, "summary": "Developer report",
                    "verification": plan("local", preset)}), "dev")
                with patch("agent_relay.verify_remote_branch", return_value=True), \
                     patch("agent_relay.observe_ci") as cloud, \
                     patch("agent_relay.prepare_workspace") as prepare, \
                     patch("agent_relay.run_local_tests") as local, \
                     patch("agent_relay.diagnose_local_failure") as diagnose:
                    worker()
                cloud.assert_not_called()
                prepare.assert_not_called()
                local.assert_not_called()
                diagnose.assert_not_called()
                dev.send.assert_called_once()
                snap = app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"], "needs_review")
                self.assertTrue(snap["result"]["remote_verified"])
                self.assertEqual(snap["result"]["verification"]["admission_reason"], "preset_not_authorized")
                self.assertEqual(app.progress.record["result"]["verification"],
                                 snap["result"]["verification"])


if __name__ == "__main__":
    unittest.main()
