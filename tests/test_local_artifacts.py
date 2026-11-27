"""NA-06.7.5 local artifacts: real child output, bounds and task evidence."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artifact_registry import (parse_artifact_requests, merge_required, register_artifacts,
    artifact_records, bound_artifact_records, artifact_url, artifact_label, withhold_artifacts)
from local_artifacts import (prepare_output_dir, observe_file, collect_local_outputs,
                             MAX_LOCAL_ARTIFACT_BYTES)
from local_execution import run_local_tests
from work_card_views import render_work_card
from work_feedback import result_record
from github_work import parse_developer
import test_action_dispatch as dispatch
import test_action_bridge as fixtures
import test_local_execution as base

NAME="report.csv"
DATA=b"a,b\n1,2\n"
DIGEST=hashlib.sha256(DATA).hexdigest()
SHA,REPO,WS=base.SHA,base.REPO,base.WORKSPACE
ALLOW=lambda:True

def request(name=NAME, required=True):
    return dict(source="local_runner",name=name,required=required,delivery="retain")

def register(outputs=None,**changes):
    params=dict(enabled=True,verified_final=True,permitted=ALLOW,workspace=WS,
                execution=base.observed(),local_outputs=outputs)
    params.update(changes)
    return register_artifacts([request()],REPO,SHA,**params)[0]

class LocalArtifactTests(unittest.TestCase):
    def test_declaration_and_required_retention(self):
        self.assertEqual(parse_artifact_requests([request()]),[request()])
        for name in ("../a.csv","x/a.csv","C:\\x.csv",".env","auth_data.json",
                     "CON.txt","abc.exe","a.csv:stream","a.csv "):
            with self.subTest(name=name),self.assertRaises(ValueError):
                parse_artifact_requests([request(name)])
        for items in ([request(),request("REPORT.CSV")],[dict(request(),delivery="link")],
                      [dict(request(),path="x")]):
            with self.assertRaises(ValueError):
                parse_artifact_requests(items)
        self.assertEqual(merge_required([request()],[request(required=False)]),[request()])
        self.assertEqual(merge_required([request()],[]),[request()])

    def test_fresh_output_directory_and_no_reuse(self):
        with tempfile.TemporaryDirectory() as d:
            base_dir=Path(d).resolve()
            source,root=base_dir/"source",base_dir/"root"
            source.mkdir();root.mkdir()
            directory=prepare_output_dir(source,root,SHA,WS,[NAME],permitted=ALLOW)
            self.assertEqual(directory,root/(WS["workspace_id"]+".artifacts"))
            with self.assertRaises(FileExistsError):
                prepare_output_dir(source,root,SHA,WS,[NAME],permitted=ALLOW)

    def test_regular_file_digest_and_missing_or_oversized(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d).resolve()
            self.assertEqual(observe_file(directory,NAME,permitted=ALLOW),{"reason":"not_found"})
            (directory/NAME).write_bytes(DATA)
            self.assertEqual(observe_file(directory,NAME,permitted=ALLOW),
                             dict(reason="",size=len(DATA),sha256=DIGEST))
            (directory/NAME).write_bytes(b"")
            self.assertEqual(observe_file(directory,NAME,permitted=ALLOW)["reason"],"local_output_size")
            with (directory/NAME).open("wb") as stream:
                stream.truncate(MAX_LOCAL_ARTIFACT_BYTES+1)
            self.assertEqual(observe_file(directory,NAME,permitted=ALLOW)["reason"],"local_output_size")
            self.assertIsNone(observe_file(directory,NAME,permitted=lambda:False))

    def test_links_and_directories_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d).resolve()
            (directory/NAME).mkdir()
            self.assertEqual(observe_file(directory,NAME,permitted=ALLOW)["reason"],"unsafe_local_output")
            (directory/NAME).rmdir()
            outside=directory/"other.csv"
            outside.write_bytes(DATA)
            try:
                os.link(outside,directory/NAME)
            except OSError:
                self.skipTest("hard links unavailable")
            self.assertEqual(observe_file(directory,NAME,permitted=ALLOW)["reason"],"unsafe_local_output")
            (directory/NAME).unlink()
            try:
                (directory/NAME).symlink_to(outside)
            except OSError:
                return
            self.assertEqual(observe_file(directory,NAME,permitted=ALLOW)["reason"],"unsafe_local_output")

    def test_real_child_produces_only_requested_outputs_after_success(self):
        for ok in (True,False):
            with self.subTest(ok=ok),tempfile.TemporaryDirectory() as d:
                root=Path(d).resolve()/"root"
                source=Path(d).resolve()/"source"
                root.mkdir();source.mkdir()
                directory=root/WS["workspace_id"]
                directory.mkdir()
                (directory/"test_output.py").write_text(
                    "import unittest, os\nfrom pathlib import Path\n"
                    "class Tests(unittest.TestCase):\n"
                    "    def test_report(self):\n"
                    "        out=Path(os.environ['AGENTRELAY_ARTIFACT_DIR'])\n"
                    "        (out/'report.csv').write_bytes(b'a,b\\n1,2\\n')\n"
                    "        (out/'unrequested.csv').write_bytes(b'ignored')\n"
                    "        self.assertTrue("+str(ok)+")\n",encoding="utf-8")
                outputs={}
                with patch("local_execution.verify_prepared_workspace",return_value=directory), \
                     patch("local_artifacts.verify_prepared_workspace",return_value=directory):
                    result=run_local_tests(source,root,REPO,SHA,WS,"unittest",timeout=10,
                                           permitted=ALLOW,artifact_names=(NAME,),artifact_output=outputs)
                self.assertEqual(result["status"],"passed" if ok else "failed")
                self.assertEqual(outputs,{NAME:dict(reason="",size=len(DATA),sha256=DIGEST)} if ok else {})

    def test_dirty_checkout_blocks_output_observation(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("local_artifacts.verify_prepared_workspace",return_value=None), \
                 patch("local_artifacts.observe_file") as read:
                result=collect_local_outputs(Path(d),Path(d),REPO,SHA,WS,[NAME],permitted=ALLOW)
            self.assertEqual(result,{NAME:{"reason":"local_workspace_changed"}})
            read.assert_not_called()

    def test_evidence_bound_to_workspace_and_successful_execution(self):
        item=register({NAME:dict(reason="",size=len(DATA),sha256=DIGEST)})
        self.assertEqual(item["status"],"available")
        self.assertEqual(item["sha256"],DIGEST)
        self.assertIsNone(artifact_url(item))
        self.assertIn("не отправлен",artifact_label(item))
        self.assertEqual(artifact_records([item],REPO,SHA),[item])
        self.assertEqual(bound_artifact_records([item],REPO,SHA,None,WS,base.observed()),[item])
        self.assertEqual(bound_artifact_records([item],REPO,SHA,None,WS,base.observed("failed")),[])
        other=dict(WS,workspace_id=WS["workspace_id"][:-1]+"c")
        execution=dict(base.observed(),workspace_id=other["workspace_id"])
        self.assertEqual(bound_artifact_records([item],REPO,SHA,None,other,execution),[])
        item2=register({NAME:dict(reason="",size=len(DATA),sha256=DIGEST)},workspace=other,execution=execution)
        self.assertNotEqual(item2["artifact_id"],item["artifact_id"])
        withheld=withhold_artifacts([item])[0]
        self.assertEqual(artifact_records([withheld],REPO,SHA),[withheld])
        self.assertEqual(withheld["reason"],"verification_incomplete")

    def test_no_metadata_when_disabled_or_failed(self):
        meta={NAME:dict(reason="",size=len(DATA),sha256=DIGEST)}
        for changes, reason in ((dict(enabled=False),"not_authorized"),
                               (dict(verified_final=False),"verification_incomplete"),
                               (dict(execution=base.observed("failed")),"local_not_passed")):
            value=register(meta,**changes)
            self.assertEqual(value["reason"],reason)
            self.assertIsNone(value["sha256"])
        self.assertEqual(register({})["reason"],"not_found")

class LocalArtifactIntegrationTests(unittest.TestCase):
    def test_host_admitted_local_output_and_required_missing(self):
        for present in (True,False):
            with self.subTest(present=present),tempfile.TemporaryDirectory() as d:
                folder=Path(d).resolve()
                source,root=folder/"source",folder/"root"
                source.mkdir();root.mkdir()
                fixture=dispatch.ExecuteTests()
                app,api,main,coord,dev=fixture.make_app(str(source),work_workspace_root=str(root),
                    work_local_tests="demo=unittest",work_artifact_projects="demo")
                self.assertIn("local_runner",app._verification_capabilities("demo")["artifact_sources"])
                main.send.return_value.text=fixtures.trailer(args={"project":"demo","task":"Create report.csv",
                                                                     "delegation":"direct"})
                with patch("agent_relay.new_turn_token",return_value=fixtures.TOKEN), \
                     patch("agent_relay.threading.Thread") as thread:
                    app.process_update(fixture.update())
                    worker=thread.call_args.kwargs["target"]
                report=dict(status="ready",repository=REPO,branch=app.work.record["branch"],
                            commit_sha=SHA,summary="Generated report",
                            verification=dict(route="local",preset="unittest",reason="Local output"),
                            artifacts=[request()])
                dev.send.return_value=dispatch.response(json.dumps(report),"dev")
                def run(*args,**kwargs):
                    self.assertEqual(kwargs["artifact_names"],(NAME,))
                    if present:
                        kwargs["artifact_output"][NAME]=dict(reason="",size=len(DATA),sha256=DIGEST)
                    return base.observed()
                with patch("agent_relay.verify_remote_branch",return_value=True), \
                     patch("agent_relay.prepare_workspace",return_value=WS), \
                     patch("agent_relay.run_local_tests",side_effect=run), \
                     patch("agent_relay.observe_ci") as cloud, \
                     patch("agent_relay.send_file") as send:
                    worker()
                cloud.assert_not_called()
                send.assert_not_called()
                snap=app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"],"completed" if present else "needs_review")
                output=snap["result"]["artifacts"][0]
                self.assertEqual(output["status"],"available" if present else "unavailable")
                self.assertEqual(app.progress.record["result"]["artifacts"],[output])

if __name__=="__main__":
    unittest.main()
