"""NA-06.7.3: exact-run archives, lazy locators and final-result integration."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import test_artifact_registry as base
from test_feedback_loop import ci_for, identity_for
from test_local_execution import REPO, SHA
from artifact_registry import (parse_artifact_requests, merge_required, register_artifacts,
                               artifact_records, artifact_url, artifact_label, withhold_artifacts,
                               bound_artifact_records)
from github_actions_artifacts import _get, collect_actions_artifacts
from work_card_views import work_keyboard, render_work_journal
from github_work import parse_developer

BRANCH = "agentrelay/task"
NAME = "demo-build"
NOW = datetime.now(timezone.utc)
START = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
CREATED = (NOW - timedelta(minutes=30)).isoformat(timespec="seconds")
EXPIRY = (NOW + timedelta(days=1)).isoformat(timespec="seconds")
EXPIRED = (NOW - timedelta(minutes=1)).isoformat(timespec="seconds")
DIGEST = "sha256:" + "a"*64
RUN = ci_for(SHA, "passed")["run_ids"][0]

def request(name=NAME, required=True):
    return dict(source="github_actions", name=name, required=required, delivery="link")

def run(run_id=RUN, sha=SHA, branch=BRANCH, **changes):
    value = dict(id=run_id, head_sha=sha, head_branch=branch, status="completed",
                 conclusion="success", run_attempt=1, run_started_at=START,
                 repository=dict(id=99, full_name=REPO), head_repository=dict(id=99, full_name=REPO))
    value.update(changes)
    return value

def archive(run_id=RUN, sha=SHA, branch=BRANCH, **changes):
    value = dict(id=42, name=NAME, size_in_bytes=2048, expired=False, created_at=CREATED,
                 expires_at=EXPIRY, digest=DIGEST,
                 workflow_run=dict(id=run_id, repository_id=99, head_repository_id=99,
                                   head_branch=branch, head_sha=sha))
    value.update(changes)
    return value

def listing(*entries):
    return dict(total_count=len(entries), artifacts=list(entries))

def collect(sequence, *, ci=None):
    with patch("github_actions_artifacts._get", side_effect=sequence):
        return collect_actions_artifacts(REPO, BRANCH, SHA, ci or ci_for(SHA,"passed"),
                                         permitted=lambda: True)

def register(sequence, *, requests=None, ci=None, enabled=True, final=True):
    with patch("github_actions_artifacts._get", side_effect=sequence):
        return register_artifacts(requests or [request()], REPO, SHA, enabled=enabled,
                                  verified_final=final, branch=BRANCH,
                                  ci=ci or ci_for(SHA,"passed"), permitted=lambda: True)

class ActionsContractTests(unittest.TestCase):
    def test_source_declarations_distinct_and_required_survives(self):
        reqs = [request(), base.request(NAME)]
        self.assertEqual(parse_artifact_requests(reqs), reqs)
        self.assertEqual(merge_required(reqs,[request(required=False)]), reqs)
        for bad in (request("../a"), request("http://x"), request(" x"),
                    dict(request(), run_id=RUN), dict(request(), path="a"),
                    dict(request(), delivery="file")):
            with self.assertRaises(ValueError):
                parse_artifact_requests([bad])
        data = dict(status="ready", repository=REPO, branch=BRANCH, commit_sha=SHA,
                    summary="report", artifacts=reqs)
        self.assertEqual(parse_developer(json.dumps(data), expected_repo=REPO).artifacts, reqs)

    def test_only_read_only_metadata_endpoints(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(listing(archive())).encode()
        with patch("github_actions_artifacts.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(_get(REPO,RUN,artifacts=True,permitted=lambda:True), listing(archive()))
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.full_url,"https://api.github.com/repos/"+REPO+"/actions/runs/"+str(RUN)+"/artifacts?per_page=100")
            self.assertIsNone(_get(REPO,0,artifacts=False,permitted=lambda:True))
            self.assertIsNone(_get(REPO,RUN,artifacts=False,permitted=lambda:False))
            opener.return_value.open.assert_called_once()

    def test_successful_run_and_archive_identity(self):
        value = register([run(), listing(archive()), run()])[0]
        self.assertEqual(value["status"],"available")
        self.assertEqual(value["github_artifact_id"],42)
        self.assertEqual(value["run_id"],RUN)
        self.assertEqual(value["digest"],DIGEST)
        self.assertEqual(value["size"],2048)
        self.assertEqual(value["filename"],NAME+".zip")
        self.assertEqual(artifact_records([value],REPO,SHA),[value])
        self.assertEqual(bound_artifact_records([value],REPO,SHA,ci_for(SHA,"passed")),[value])
        self.assertEqual(bound_artifact_records([value],REPO,SHA,ci_for(SHA,"failed")),[])
        self.assertEqual(bound_artifact_records([dict(value,run_id=RUN+1)],REPO,SHA,ci_for(SHA,"passed")),[])
        url = "https://github.com/"+REPO+"/actions/runs/"+str(RUN)+"/artifacts/42"
        self.assertEqual(artifact_url(value),url)
        self.assertIn(url,artifact_label(value,include_url=True))
        self.assertIsNone(artifact_url(dict(value,expires_at=EXPIRED)))
        self.assertIn("истёк",artifact_label(dict(value,expires_at=EXPIRED)))
        withheld = withhold_artifacts([value])[0]
        self.assertIsNone(artifact_url(withheld))
        self.assertEqual(artifact_records([withheld],REPO,SHA),[withheld])

    def test_reject_wrong_run_fork_and_rerun(self):
        for bad, reason in ((run(head_sha="f"*40),"actions_run_mismatch"),
                            (run(head_branch="other"),"actions_run_mismatch"),
                            (run(head_repository=dict(id=100,full_name="other/repo")),"actions_run_mismatch"),
                            (run(conclusion="failure"),"actions_run_mismatch"),
                            (run(run_attempt=2),"actions_attempt_ambiguous")):
            with self.subTest(reason=reason):
                self.assertEqual(collect([bad]),(reason,{}))
        self.assertEqual(collect([run(),listing(archive()),run(run_attempt=2)]),("actions_run_mismatch",{}))
        self.assertEqual(collect([run(),listing(archive(workflow_run=dict(id=999)))]),
                         ("actions_run_mismatch",{}))

    def test_missing_ambiguous_expired_and_partial_results(self):
        for items, reason in ((listing(),"not_found"),
                              (listing(archive(),archive(id=43)),"ambiguous_name"),
                              (listing(archive(expired=True)),"expired"),
                              (listing(archive(expires_at=EXPIRED)),"expired")):
            with self.subTest(reason=reason):
                value=register([run(),items,run()])[0]
                self.assertEqual(value["reason"],reason)
                self.assertIsNone(artifact_url(value))
        for data in (dict(total_count=2,artifacts=[archive()]),
                     listing(archive(digest="invalid")), listing(archive(id=0)),
                     listing(archive(created_at=(NOW-timedelta(days=1)).isoformat()))):
            self.assertEqual(collect([run(),data]),("metadata_unavailable",{}))

    def test_no_ci_no_api_and_stop_fence(self):
        with patch("github_actions_artifacts._get") as get:
            for state in ("failed","pending","unavailable","not_run"):
                self.assertEqual(collect_actions_artifacts(REPO,BRANCH,SHA,ci_for(SHA,state),
                        permitted=lambda:True),("actions_ci_not_passed",{}))
            self.assertIsNone(collect_actions_artifacts(REPO,BRANCH,SHA,ci_for(SHA,"passed"),
                         permitted=lambda:False))
            get.assert_not_called()
        state=[True]
        def stop(*args,**kwargs):
            state[0]=False
            return run()
        with patch("github_actions_artifacts._get",side_effect=stop) as get:
            self.assertIsNone(collect_actions_artifacts(REPO,BRANCH,SHA,ci_for(SHA,"passed"),
                            permitted=lambda:state[0]))
            get.assert_called_once()

class ActionsIntegrationTests(unittest.TestCase):
    def test_required_archive_completed_and_no_local_execution(self):
        for exists,status in ((True,"completed"),(False,"needs_review")):
            with self.subTest(exists=exists), tempfile.TemporaryDirectory() as d:
                app,dev,worker,reply=base.ArtifactIntegrationTests().setup_job(d)
                branch=app.work.record["branch"]
                dev.send.return_value=reply(SHA,[request()],"cloud")
                run_meta=run(branch=branch)
                item=archive(branch=branch)
                with patch("agent_relay.verify_remote_branch",return_value=True), \
                     patch("agent_relay.observe_ci",return_value=ci_for(SHA,"passed")), \
                     patch("github_actions_artifacts._get",side_effect=[run_meta,listing(item) if exists else listing(),run_meta]), \
                     patch("artifact_registry._tree_entries") as tree, \
                     patch("agent_relay.prepare_workspace") as prepare, \
                     patch("agent_relay.run_local_tests") as local:
                    worker()
                tree.assert_not_called()
                prepare.assert_not_called()
                local.assert_not_called()
                snap=app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"],status)
                output=snap["result"]["artifacts"][0]
                self.assertEqual(output["status"],"available" if exists else "unavailable")
                self.assertEqual(app.progress.record["result"]["artifacts"],[output])
                buttons=work_keyboard(1,False,snap["result"],phase=status)["inline_keyboard"]
                self.assertEqual(any("/artifacts/" in b.get("url","") for row in buttons for b in row),exists)
                if exists:
                    self.assertIn(artifact_url(output),render_work_journal(
                        dict(work_id=1,project="demo",phase=status,result=snap["result"])).decode())

    def test_actions_requires_explicit_cloud_route(self):
        with tempfile.TemporaryDirectory() as d:
            app,dev,worker,reply=base.ArtifactIntegrationTests().setup_job(d)
            dev.send.return_value=reply(SHA,[request()],"none")
            with patch("agent_relay.verify_remote_branch",return_value=True), \
                 patch("agent_relay.observe_ci") as cloud, \
                 patch("github_actions_artifacts._get") as get, \
                 patch("agent_relay.prepare_workspace") as prepare:
                worker()
            cloud.assert_not_called()
            get.assert_not_called()
            prepare.assert_not_called()
            snap=app.work.feedback_snapshot(1)
            self.assertEqual(snap["status"],"needs_review")
            self.assertEqual(snap["result"]["artifacts"][0]["reason"],"actions_ci_not_passed")

    def test_feedback_carries_archive_to_final_successful_run_only(self):
        with tempfile.TemporaryDirectory() as d:
            app,dev,worker,reply=base.ArtifactIntegrationTests().setup_job(d,feedback=True)
            branch=app.work.record["branch"]
            final=base.NEW
            final_ci=ci_for(final,"passed")
            final_id=final_ci["run_ids"][0]
            run_meta=run(final_id,final,branch)
            dev.send.side_effect=[reply(SHA,[request()],"cloud"),reply(final,None,"cloud")]
            with patch("agent_relay.verify_remote_branch",return_value=True), \
                 patch("agent_relay.read_commit_identity",side_effect=[identity_for(SHA),identity_for(final,SHA)]), \
                 patch("agent_relay.observe_ci",side_effect=[ci_for(SHA,"failed"),final_ci]), \
                 patch("github_actions_artifacts._get",side_effect=[run_meta,listing(archive(final_id,final,branch)),run_meta]) as get, \
                 patch("agent_relay.prepare_workspace") as prepare:
                worker()
            prepare.assert_not_called()
            self.assertEqual(get.call_count,3)
            self.assertTrue(all(call.args==(REPO,final_id) for call in get.call_args_list))
            snap=app.work.feedback_snapshot(1)
            self.assertEqual(snap["status"],"completed")
            self.assertEqual(snap["result"]["artifacts"][0]["commit_sha"],final)
            self.assertEqual(snap["result"]["artifacts"][0]["run_id"],final_id)

if __name__ == "__main__":
    unittest.main()
