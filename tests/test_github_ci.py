"""Read-only CI observation: exact identity, bounded results, native feedback."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from github_ci import observe_ci, ci_record, _get
from work_feedback import result_record
from work_card_views import render_work_card, work_keyboard
import test_action_bridge as fixtures
import test_action_dispatch as dispatch

SHA = "a"*40
BRANCH = "agentrelay/work-1"
REPO = "example/sandbox"


def run(id=1, status="completed", conclusion="success", sha=SHA, branch=BRANCH):
    return dict(id=id, status=status, conclusion=conclusion, head_sha=sha, head_branch=branch)


def payload(*runs):
    return dict(total_count=len(runs), workflow_runs=list(runs))


class CIObservationTests(unittest.TestCase):
    def observe(self, *responses):
        with patch("github_ci._get", side_effect=responses) as get:
            result = observe_ci(REPO, BRANCH, SHA, wait_seconds=0, permitted=lambda: True)
            self.assertEqual(get.call_args_list[0].args[2]["head_sha"], SHA)
            self.assertEqual(get.call_args_list[0].args[2]["branch"], BRANCH)
            return result

    def test_success_failure_pending_and_incomplete(self):
        for data, status in (
            (payload(run(), run(2)), "passed"),
            (payload(run(), run(2, conclusion="failure")), "failed"),
            (payload(run(status="in_progress", conclusion=None)), "pending"),
            (payload(run(conclusion="cancelled")), "incomplete"),
            (payload(run(conclusion="skipped")), "incomplete"),
        ):
            with self.subTest(status=status):
                result = self.observe(data)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["run_count"], data["total_count"])
                self.assertEqual(ci_record(result, SHA), result)

    def test_no_runs_distinguished_from_no_registered_workflows(self):
        self.assertEqual(self.observe(payload(), {"total_count": 0})["status"], "not_configured")
        self.assertEqual(self.observe(payload(), {"total_count": 1})["status"], "not_run")

    def test_wrong_identity_truncation_and_http_failure_never_pass(self):
        for data in (payload(run(sha="b"*40)), payload(run(branch="main")),
                     {"total_count": 2, "workflow_runs": [run()]},
                     payload(run(), run()), payload(run(status="unknown")),
                     HTTPError("https://api.github.com", 403, "denied", None, None)):
            with self.subTest(data=type(data)):
                self.assertEqual(self.observe(data)["status"], "unavailable")

    def test_stop_prevents_additional_requests(self):
        permitted = [True]
        def get(*args, **kwargs):
            permitted[0] = False
            return payload()
        with patch("github_ci._get", side_effect=get) as call:
            self.assertIsNone(observe_ci(REPO, BRANCH, SHA, wait_seconds=60,
                                         permitted=lambda: permitted[0]))
            call.assert_called_once()

    def test_fixed_get_endpoint_and_token_not_in_url(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"total_count":0,"workflow_runs":[]}'
        with patch("github_ci.build_opener") as opener, \
             patch.dict("os.environ", {"WORK_CI_TOKEN": "private-test-token"}):
            opener.return_value.open.return_value = response
            _get(REPO, "runs", {"head_sha": SHA, "branch": BRANCH}, timeout=8)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.method, "GET")
            self.assertTrue(request.full_url.startswith("https://api.github.com/repos/example/sandbox/actions/runs?"))
            self.assertNotIn("private-test-token", request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer private-test-token")
            with self.assertRaises(ValueError):
                _get("../evil", "runs", {}, timeout=8)

    def test_evidence_not_attached_to_unverified_or_other_commit(self):
        ci = self.observe(payload(run()))
        data = dict(developer_status="ready", repository=REPO, branch=BRANCH,
                    commit_sha=SHA, remote_verified=True, ci=ci)
        self.assertEqual(result_record(data, 0)["ci"], ci)
        self.assertNotIn("ci", result_record(dict(data, commit_sha="b"*40), 0))
        self.assertNotIn("ci", result_record(dict(data, remote_verified=False), 0))
        self.assertIn("GitHub CI", render_work_card(dict(work_id=1, phase="completed",
                                                        project="demo", result=data)))
        self.assertTrue(any("wp:s:1" == button.get("callback_data")
                            for row in work_keyboard(1, False, {}, phase="ci")["inline_keyboard"]
                            for button in row))


class CIIntegrationTests(unittest.TestCase):
    def test_direct_work_persists_ci_and_never_invokes_coordinator(self):
        for conclusion, expected in (("success", "completed"), ("failure", "needs_review")):
            with self.subTest(conclusion=conclusion), tempfile.TemporaryDirectory() as d:
                fixture = dispatch.ExecuteTests()
                app, api, main, coord, dev = fixture.make_app(d, work_ci_verify=True, work_ci_wait_seconds=0)
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
                with patch("agent_relay.verify_remote_branch", return_value=True) as verify, \
                     patch("github_ci._get", return_value=payload(run(branch=branch, conclusion=conclusion))) as get:
                    worker()
                verify.assert_called_once()
                get.assert_called_once()
                coord.send.assert_not_called()
                dev.send.assert_called_once()
                snapshot = app.work.feedback_snapshot(1)
                self.assertEqual(snapshot["status"], expected)
                self.assertTrue(snapshot["result"]["remote_verified"])
                self.assertEqual(snapshot["result"]["ci"]["status"],
                                 "passed" if conclusion == "success" else "failed")
                self.assertEqual(app.progress.record["result"]["ci"], snapshot["result"]["ci"])
                self.assertFalse(app._native_worker_lock.locked())


if __name__ == "__main__":
    unittest.main()
