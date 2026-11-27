"""NA-06.6: no fixed repair cap, exact continuation and observed evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from feedback_loop import (parse_feedback_projects, read_commit_identity, repairable,
                           preserves_checks, feedback_record)
from github_work import parse_developer
from verification_routing import admit_plan
import test_action_bridge as fixtures
import test_action_dispatch as dispatch
from test_local_execution import SHA, REPO, STAMP, WORKSPACE, observed

CAPS = {"cloud_ci": True, "local_presets": ["unittest"]}
SHAS = [SHA] + [str(i)*40 for i in range(1, 7)]

def route(name="cloud"):
    result = {"route": name, "reason": "Required verification"}
    if name in ("local", "hybrid"):
        result["preset"] = "unittest"
    return result

def ci_for(sha, status):
    ids = [int(sha[0], 16)+1] if status in ("passed", "failed", "pending") else []
    return dict(status=status, commit_sha=sha, run_count=len(ids), run_ids=ids, observed_at=STAMP)

def identity_for(sha, parent=None, tree=None):
    return dict(sha=sha, tree=tree or ("f"*39 + sha[0]), parents=[parent] if parent else [])

class FeedbackContractTests(unittest.TestCase):
    def test_unchanged_is_only_valid_in_exact_feedback_turn(self):
        body = dict(status="unchanged", repository=REPO, branch="task", commit_sha=SHA,
                    summary="Intentional failure")
        with self.assertRaises(ValueError):
            parse_developer(json.dumps(body), expected_repo=REPO, expected_branch="task")
        self.assertEqual(parse_developer(json.dumps(body), expected_repo=REPO,
                         expected_branch="task", feedback_sha=SHA).status, "unchanged")
        for bad in (dict(body, commit_sha="b"*40), dict(body, branch="other"),
                    dict(body, verification=route())):
            with self.assertRaises(ValueError):
                parse_developer(json.dumps(bad), expected_repo=REPO, expected_branch="task",
                                feedback_sha=SHA)

    def test_admission_and_check_preservation(self):
        self.assertEqual(parse_feedback_projects("demo", {"demo": REPO}), {"demo"})
        for bad in ("other", "demo,demo", "demo,"):
            with self.assertRaises(ValueError):
                parse_feedback_projects(bad, {"demo": REPO})
        cloud = admit_plan(route(), SHA, CAPS)
        local = admit_plan(route("local"), SHA, CAPS)
        hybrid = admit_plan(route("hybrid"), SHA, CAPS)
        self.assertTrue(preserves_checks(cloud, hybrid))
        self.assertTrue(preserves_checks(local, hybrid))
        self.assertFalse(preserves_checks(hybrid, local))
        self.assertFalse(preserves_checks(cloud, admit_plan(route("none"), SHA, CAPS)))
        for state in ("not_run", "not_configured", "unavailable", "pending", "incomplete", "passed"):
            self.assertFalse(repairable(cloud, ci_for(SHA, state), None))
        self.assertTrue(repairable(cloud, ci_for(SHA, "failed"), None))
        self.assertTrue(repairable(local, None, observed("failed")))
        self.assertFalse(repairable(local, None, dict(observed(), status="timeout")))

    def test_commit_metadata_read_only_exact_identity(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"sha": SHA, "tree": {"sha": "c"*40},
                                                "parents": [{"sha": "b"*40}]}).encode()
        with patch("feedback_loop.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(read_commit_identity(REPO, SHA, permitted=lambda: True),
                             {"sha": SHA, "tree": "c"*40, "parents": ["b"*40]})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertTrue(request.full_url.endswith("/git/commits/" + SHA))
            self.assertIsNone(read_commit_identity(REPO, "d"*40, permitted=lambda: True))
            self.assertIsNone(read_commit_identity(REPO, SHA, permitted=lambda: False))

class FeedbackIntegrationTests(unittest.TestCase):
    def setup_job(self, folder, *, enabled=True, local=False):
        base = Path(folder).resolve()
        source, root = base/"source", base/"root"
        source.mkdir()
        root.mkdir()
        fixture = dispatch.ExecuteTests()
        app, api, main, coord, dev = fixture.make_app(
            str(source), work_feedback_projects="demo" if enabled else "",
            work_workspace_root=str(root), work_local_tests="demo=unittest",
            work_ci_verify=True, work_ci_wait_seconds=0, work_max_rounds=1)
        main.send.return_value.text = fixtures.trailer(args={
            "project": "demo", "task": "Add tests and preserve expected behavior", "delegation": "direct"})
        with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
             patch("agent_relay.threading.Thread") as thread:
            app.process_update(fixture.update("Юки, создай модуль в demo"))
            worker = thread.call_args.kwargs["target"]
        branch = app.work.record["branch"]
        def reply(sha, status="ready", path="local" if local else "cloud"):
            data = dict(status=status, repository=REPO, branch=branch, commit_sha=sha,
                        summary="Developer report" if status == "ready" else "Intentional failure; no changes")
            if status == "ready":
                data["verification"] = route(path)
            return dispatch.response(json.dumps(data), "dev")
        return app, main, coord, dev, worker, reply

    def test_cloud_repairs_continue_beyond_configured_round_limit_without_local(self):
        with tempfile.TemporaryDirectory() as d:
            app, main, coord, dev, worker, reply = self.setup_job(d)
            ids = SHAS[:5]
            dev.send.side_effect = [reply(sha) for sha in ids]
            metadata = [identity_for(sha, ids[i-1] if i else None) for i, sha in enumerate(ids)]
            with patch("agent_relay.verify_remote_branch", return_value=True), \
                 patch("agent_relay.read_commit_identity", side_effect=metadata), \
                 patch("agent_relay.observe_ci", side_effect=[ci_for(s, "failed" if i < 4 else "passed")
                                                               for i, s in enumerate(ids)]) as cloud, \
                 patch("agent_relay.prepare_workspace") as prepare, \
                 patch("agent_relay.run_local_tests") as local, \
                 patch("agent_relay.diagnose_local_failure") as diagnose:
                worker()
            self.assertEqual(dev.send.call_count, 5)
            self.assertEqual(cloud.call_count, 5)
            prepare.assert_not_called()
            local.assert_not_called()
            diagnose.assert_not_called()
            coord.send.assert_not_called()
            snap = app.work.feedback_snapshot(1)
            self.assertEqual(snap["status"], "completed")
            self.assertEqual(snap["stage"], 5)
            self.assertEqual(snap["result"]["ci"]["commit_sha"], ids[-1])
            loop = snap["result"]["feedback"]
            self.assertEqual(loop["status"], "resolved")
            self.assertEqual(loop["attempts"], 4)
            self.assertEqual(loop["commits"], ids)
            self.assertEqual([r["ci"] for r in loop["observations"]], ["failed"]*4+["passed"])
            self.assertEqual(feedback_record(loop, ids[-1]), loop)
            self.assertEqual(app.progress.record["result"]["feedback"], loop)
            for call in dev.send.call_args_list[1:]:
                self.assertIn("AUTONOMOUS FEEDBACK", call.args[0])
                self.assertIn("Add tests and preserve expected behavior", call.args[0])
                self.assertIn("Юки, создай модуль в demo", call.args[0])
            self.assertFalse(app._native_worker_lock.locked())

    def test_intentional_failure_retained_and_local_evidence_forwarded(self):
        with tempfile.TemporaryDirectory() as d:
            app, main, coord, dev, worker, reply = self.setup_job(d, local=True)
            dev.send.side_effect = [reply(SHA), reply(SHA, "unchanged")]
            def execute(*args, **kwargs):
                kwargs["failure_output"].append("AssertionError: 5 != 6")
                return observed("failed")
            with patch("agent_relay.verify_remote_branch", return_value=True), \
                 patch("agent_relay.read_commit_identity", return_value=identity_for(SHA)), \
                 patch("agent_relay.observe_ci") as cloud, \
                 patch("agent_relay.prepare_workspace", return_value=WORKSPACE) as prepare, \
                 patch("agent_relay.run_local_tests", side_effect=execute) as local:
                worker()
            cloud.assert_not_called()
            prepare.assert_called_once()
            local.assert_called_once()
            self.assertEqual(dev.send.call_count, 2)
            self.assertIn("AssertionError: 5 != 6", dev.send.call_args.args[0])
            snap = app.work.feedback_snapshot(1)
            self.assertEqual(snap["status"], "needs_review")
            self.assertEqual(snap["result"]["feedback"]["status"], "developer_finished")
            self.assertEqual(snap["result"]["execution"]["status"], "failed")
            self.assertNotIn("AssertionError: 5 != 6", json.dumps(snap))

    def test_cycle_wrong_parent_or_weakened_checks_stop_without_reverification(self):
        for mode, reason in (("cycle", "no_progress"), ("parent", "identity_unavailable"),
                             ("weaken", "verification_weakened")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                app, main, coord, dev, worker, reply = self.setup_job(d)
                new = SHAS[1]
                dev.send.side_effect = [reply(SHA), reply(new, path="none" if mode=="weaken" else "cloud")]
                first = identity_for(SHA)
                second = identity_for(new, SHA if mode != "parent" else "b"*40,
                                      tree=first["tree"] if mode == "cycle" else None)
                with patch("agent_relay.verify_remote_branch", return_value=True), \
                     patch("agent_relay.read_commit_identity", side_effect=[first, second]), \
                     patch("agent_relay.observe_ci", return_value=ci_for(SHA, "failed")) as cloud, \
                     patch("agent_relay.prepare_workspace") as prepare:
                    worker()
                cloud.assert_called_once()
                prepare.assert_not_called()
                self.assertEqual(dev.send.call_count, 2)
                snap = app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"], "needs_review")
                self.assertEqual(snap["result"]["feedback"]["status"], reason)
                self.assertEqual(snap["result"]["commit_sha"], new if mode=="weaken" else SHA)

    def test_nonrepairable_or_disabled_does_not_call_developer_again(self):
        for enabled, state in ((True, "pending"), (True, "unavailable"), (False, "failed")):
            with self.subTest(enabled=enabled, state=state), tempfile.TemporaryDirectory() as d:
                app, main, coord, dev, worker, reply = self.setup_job(d, enabled=enabled)
                dev.send.return_value = reply(SHA)
                with patch("agent_relay.verify_remote_branch", return_value=True), \
                     patch("agent_relay.read_commit_identity") as metadata, \
                     patch("agent_relay.observe_ci", return_value=ci_for(SHA, state)), \
                     patch("agent_relay.prepare_workspace") as prepare:
                    worker()
                dev.send.assert_called_once()
                metadata.assert_not_called()
                prepare.assert_not_called()
                self.assertEqual(app.work.feedback_snapshot(1)["status"], "needs_review")

    def test_stop_or_ambiguous_response_preserves_last_observation(self):
        for stopped in (True, False):
            with self.subTest(stopped=stopped), tempfile.TemporaryDirectory() as d:
                app, main, coord, dev, worker, reply = self.setup_job(d)
                def send(*args, **kwargs):
                    if dev.send.call_count == 1:
                        return reply(SHA)
                    if stopped:
                        app.work.stop(expected_id=1)
                        return reply(SHAS[1])
                    raise RuntimeError("ambiguous connector result")
                dev.send.side_effect = send
                with patch("agent_relay.verify_remote_branch", return_value=True), \
                     patch("agent_relay.read_commit_identity", return_value=identity_for(SHA)), \
                     patch("agent_relay.observe_ci", return_value=ci_for(SHA, "failed")) as cloud:
                    worker()
                cloud.assert_called_once()
                self.assertEqual(dev.send.call_count, 2)
                snap = app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"], "stopped" if stopped else "needs_review")
                self.assertEqual(snap["result"]["ci"]["status"], "failed")
                self.assertEqual(snap["result"]["feedback"]["status"], "evaluating" if stopped else "ambiguous")
                self.assertFalse(app._native_worker_lock.locked())

if __name__ == "__main__":
    unittest.main()
