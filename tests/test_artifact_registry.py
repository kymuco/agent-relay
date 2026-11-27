"""NA-06.7.1/2: artifact contracts, lazy immutable metadata, and integration."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from artifact_registry import (artifact_records, artifact_url, merge_required,
                               parse_artifact_projects, parse_artifact_requests,
                               register_artifacts, _tree_entries)
from github_work import parse_developer
from work_card_views import work_keyboard, render_work_card, render_work_journal
from work_feedback import result_record, context_prompt
from local_codex import WorkState
from test_feedback_loop import identity_for, ci_for
from test_local_execution import SHA, REPO

NEW = "b"*40 if SHA != "b"*40 else "c"*40
TREE = "f"*40
BLOB = "e"*40

def request(path="output/report.md", required=True):
    return dict(source="github_repository", path=path, required=required, delivery="link")

def entry(path="output/report.md", mode="100644", kind="blob"):
    return dict(path=path, sha=BLOB, mode=mode, type=kind, size=123)

def resolve(requests, entries, **kwargs):
    with patch("feedback_loop.read_commit_identity", return_value=dict(sha=SHA, tree=TREE, parents=[])), \
         patch("artifact_registry._tree_entries", return_value=entries):
        return register_artifacts(requests, REPO, SHA, enabled=kwargs.get("enabled", True),
                                  verified_final=kwargs.get("final", True), permitted=lambda: True)

class ArtifactContractTests(unittest.TestCase):
    def test_declarations_are_strict_and_canonical(self):
        obj = dict(status="ready", repository=REPO, branch="task", commit_sha=SHA,
                   summary="report", artifacts=[request()])
        self.assertEqual(parse_developer(json.dumps(obj), expected_repo=REPO).artifacts, [request()])
        for path in ("../secret", "a/../b", "/etc/file", "C:\\x", "https://evil/x",
                     "a//b", ".git/config", ".env.ragna", "a/%2e%2e/x", "x\nz", "a/"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                parse_artifact_requests([request(path)])
        for bad in ([request(), request()], [dict(request(), required="yes")],
                    [dict(request(), url="https://evil")], [dict(request(), delivery="file")],
                    [dict(request(), source="local")], [request(str(i)) for i in range(6)]):
            with self.assertRaises(ValueError):
                parse_artifact_requests(bad)
        self.assertEqual(parse_artifact_projects("demo", {"demo": REPO}), {"demo"})
        for raw in ("other", "demo,demo"):
            with self.assertRaises(ValueError):
                parse_artifact_projects(raw, {"demo": REPO})

    def test_merge_preserves_required_artifacts_without_downgrade(self):
        first = [request("must.md"), request("old.md", False)]
        self.assertEqual(merge_required(first, [request("must.md", False)]), [request("must.md")])
        self.assertEqual(merge_required(first, []), [request("must.md")])

    def test_tree_metadata_only_exact_identity(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"sha": TREE, "truncated": False, "tree": [entry()]}).encode()
        with patch("artifact_registry.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(_tree_entries(REPO, TREE, permitted=lambda: True), {"output/report.md": entry()})
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_method(), "GET")
            self.assertTrue(req.full_url.endswith("/git/trees/"+TREE+"?recursive=1"))
            self.assertIsNone(_tree_entries(REPO, BLOB, permitted=lambda: True))
            response.read.return_value = json.dumps({"sha": TREE, "truncated": True, "tree": [entry()]}).encode()
            self.assertIsNone(_tree_entries(REPO, TREE, permitted=lambda: True))

    def test_metadata_records_and_immutable_links(self):
        path = "docs/Report 1.md"
        values = resolve([request(path), request("missing.md"), request("link")],
                         {path: entry(path), "link": entry("link", "120000")})
        self.assertEqual([r["status"] for r in values], ["available", "unavailable", "unavailable"])
        self.assertEqual(values[0]["size"], 123)
        self.assertEqual(values[0]["git_blob_sha"], BLOB)
        self.assertEqual(artifact_url(values[0]), "https://github.com/"+REPO+"/blob/"+SHA+"/docs/Report%201.md")
        self.assertIsNone(artifact_url(values[1]))
        self.assertEqual(artifact_records(values, REPO, SHA), values)
        self.assertEqual(artifact_records(values, REPO, NEW), [])
        self.assertEqual(artifact_records([dict(values[0], path="other")], REPO, SHA), [])
        self.assertNotIn("sha256", values[0])

    def test_disabled_failed_or_stopped_never_access_api(self):
        with patch("feedback_loop.read_commit_identity") as identity, \
             patch("artifact_registry._tree_entries") as tree:
            for enabled, final, reason in ((False, True, "not_authorized"),
                                           (True, False, "verification_incomplete")):
                values = register_artifacts([request()], REPO, SHA, enabled=enabled,
                                            verified_final=final, permitted=lambda: True)
                self.assertEqual(values[0]["reason"], reason)
            self.assertIsNone(register_artifacts([request()], REPO, SHA, enabled=True,
                                                 verified_final=True, permitted=lambda: False))
            identity.assert_not_called()
            tree.assert_not_called()

    def test_persisted_result_and_card_links_are_host_metadata(self):
        records = resolve([request()], {"output/report.md": entry()})
        result = result_record(dict(developer_status="ready", repository=REPO, branch="task",
                                    commit_sha=SHA, remote_verified=True, artifacts=records), 0)
        card = dict(work_id=1, phase="completed", project="demo", result=result)
        self.assertIn("Артефакты", render_work_card(card))
        url = artifact_url(records[0])
        self.assertIn(url, render_work_journal(card).decode())
        self.assertTrue(any(b.get("url")==url for row in work_keyboard(1, False, result,
                        phase="completed")["inline_keyboard"] for b in row))
        self.assertFalse(any(b.get("url")==url for row in work_keyboard(1, False, result,
                         phase="running")["inline_keyboard"] for b in row))
        self.assertNotIn("artifacts", result_record(dict(result, remote_verified=False), 0))

class ArtifactIntegrationTests(unittest.TestCase):
    def setup_job(self, folder, *, enabled=True, feedback=False):
        # Build the existing direct native task fixture with artifact opt-in.
        from dataclasses import replace
        from pathlib import Path
        import test_action_dispatch as dispatch
        import test_action_bridge as fixtures
        from agent_relay import AgentRelay
        source = Path(folder).resolve()
        fixture = dispatch.ExecuteTests()
        app, api, main, coord, dev = fixture.make_app(str(source), work_artifact_projects="demo" if enabled else "",
            work_feedback_projects="demo" if feedback else "", work_ci_verify=True, work_ci_wait_seconds=0)
        main.send.return_value.text = fixtures.trailer(args={"project":"demo","task":"Create report.md",
                                                                "delegation":"direct"})
        with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
             patch("agent_relay.threading.Thread") as thread:
            app.process_update(fixture.update("Юки, создай отчёт в demo"))
            worker = thread.call_args.kwargs["target"]
        branch = app.work.record["branch"]
        def reply(sha, requests, route="none"):
            report = dict(status="ready", repository=REPO, branch=branch, commit_sha=sha,
                          summary="Developer report", verification={"route":route,"reason":"Required check"})
            if requests is not None:
                report["artifacts"] = requests
            return dispatch.response(json.dumps(report), "dev")
        return app, dev, worker, reply

    def test_required_missing_and_optional_missing_have_distinct_outcomes(self):
        for enabled, present, required, status in ((True, True, True, "completed"),
            (True, False, True, "needs_review"), (True, False, False, "completed"),
            (False, True, True, "needs_review")):
            with self.subTest(enabled=enabled, present=present, required=required), tempfile.TemporaryDirectory() as d:
                app, dev, worker, reply = self.setup_job(d, enabled=enabled)
                dev.send.return_value = reply(SHA, [request(required=required)])
                with patch("agent_relay.verify_remote_branch", return_value=True), \
                     patch("feedback_loop.read_commit_identity", return_value=identity_for(SHA)) as identity, \
                     patch("artifact_registry._tree_entries", return_value={"output/report.md":entry()} if present else {}) as tree, \
                     patch("agent_relay.prepare_workspace") as prepare, \
                     patch("agent_relay.observe_ci") as cloud:
                    worker()
                prepare.assert_not_called()
                cloud.assert_not_called()
                self.assertEqual(identity.call_count, int(enabled))
                self.assertEqual(tree.call_count, int(enabled))
                snap = app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"], status)
                self.assertEqual(snap["result"]["artifacts"][0]["status"], "available" if enabled and present else "unavailable")
                self.assertEqual(app.progress.record["result"]["artifacts"], snap["result"]["artifacts"])

    def test_feedback_registers_only_final_sha_and_carries_required_file(self):
        with tempfile.TemporaryDirectory() as d:
            app, dev, worker, reply = self.setup_job(d, feedback=True)
            dev.send.side_effect = [reply(SHA, [request()], "cloud"), reply(NEW, None, "cloud")]
            first, second = identity_for(SHA), identity_for(NEW, SHA)
            with patch("agent_relay.verify_remote_branch", return_value=True), \
                 patch("agent_relay.read_commit_identity", side_effect=[first, second]), \
                 patch("feedback_loop.read_commit_identity", return_value=second) as artifact_identity, \
                 patch("artifact_registry._tree_entries", return_value={"output/report.md":entry()}) as tree, \
                 patch("agent_relay.observe_ci", side_effect=[ci_for(SHA,"failed"), ci_for(NEW,"passed")]), \
                 patch("agent_relay.prepare_workspace") as prepare:
                worker()
            prepare.assert_not_called()
            artifact_identity.assert_called_once()
            self.assertEqual(artifact_identity.call_args.args, (REPO, NEW))
            tree.assert_called_once()
            snap = app.work.feedback_snapshot(1)
            self.assertEqual(snap["status"], "completed")
            self.assertEqual(snap["result"]["artifacts"][0]["commit_sha"], NEW)
            self.assertTrue(snap["result"]["artifacts"][0]["required"])

    def test_pending_amendment_atomically_withholds_final_links(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            state = WorkState(str(Path(d)/"work.json"))
            wid = state.start("demo", repository=REPO)
            state.add_amendment("More changes", expected_id=wid)
            values = resolve([request()], {"output/report.md": entry()})
            result = dict(developer_status="ready", repository=REPO, branch="task", commit_sha=SHA,
                          remote_verified=True, artifacts=values)
            self.assertEqual(state.finalize(wid,"completed",1,"done",result=result),"needs_review")
            self.assertEqual(state.feedback_snapshot(wid)["result"]["artifacts"][0]["reason"],"verification_incomplete")

if __name__ == "__main__":
    unittest.main()
