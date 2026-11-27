"""Native recovery: bounded reads, identity and no model/Telegram replay."""
import copy
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from artifact_recovery import exact_turn_text, recover_turn, read_error, recovery_eligible
from native_artifacts import NativeArtifactRegistry
import test_native_artifacts as base

MARKER="[AgentRelay recovery identity: "+ "a"*32 + "]"

def snapshot(marker=MARKER, text=base.TEXT, cid=base.CID):
    payload={"conversation_id":cid,"current_node":"a","mapping":{
        "u":{"parent":None,"message":{"id":"user","author":{"role":"user"},
             "content":{"content_type":"text","parts":["prompt\n"+marker+"\n"]}}},
        "tool":{"parent":"u","message":{"id":"tool","author":{"role":"tool"}}},
        "a":{"parent":"tool","message":{"id":"assistant","author":{"role":"assistant"},
             "recipient":"all","channel":"final","status":"finished_successfully","end_turn":True,
             "content":{"content_type":"text","parts":[text]}}}}}
    return Mock(complete=True,conversation_id=cid,current_node="a",
                to_canonical_payload=Mock(return_value=payload))

def error(stage="retrieval", reason="READ_TRANSPORT_FAILED", http=200, state="unchanged"):
    exc=RuntimeError("secret locator")
    exc.stage=stage
    exc.reason=reason
    exc.status_code=http
    exc.destination_state=state
    return exc

def request_error():
    exc=RuntimeError("secret request")
    exc.request_stage="transport"
    exc.status_code=200
    return exc

def immediate(delay,permitted):
    return permitted()

class RecoveryContractTests(unittest.TestCase):
    def test_exact_current_final(self):
        self.assertEqual(exact_turn_text(snapshot(),base.CID,MARKER),base.TEXT)

    def test_wrong_turn_hidden_incomplete_duplicate_or_other_conversation(self):
        for mutation in ("other","hidden","running","duplicate","later_user","cycle","old_marker","wrong_node","no_end","incomplete"):
            with self.subTest(mutation=mutation):
                snap=snapshot()
                p=snap.to_canonical_payload.return_value
                m=p["mapping"]
                if mutation=="other":
                    p["conversation_id"]="wrong"
                elif mutation=="hidden":
                    m["a"]["message"]["channel"]="analysis"
                elif mutation=="running":
                    m["a"]["message"]["status"]="in_progress"
                elif mutation=="duplicate":
                    m["u2"]=copy.deepcopy(m["u"])
                elif mutation=="later_user":
                    m["u2"]={"parent":"u","message":{"id":"later","author":{"role":"user"}}}
                    m["a"]["parent"]="u2"
                elif mutation=="cycle":
                    m["tool"]["parent"]="a"
                elif mutation=="old_marker":
                    m["u"]["message"]["content"]["parts"]=["old user"]
                elif mutation=="wrong_node":
                    snap.current_node="tool"
                elif mutation=="no_end":
                    m["a"]["message"]["end_turn"]=False
                else:
                    snap.complete=False
                self.assertIsNone(exact_turn_text(snap,base.CID,MARKER))

    def test_readback_retry_and_stop_never_send(self):
        c=Mock()
        c.get_conversation_snapshot.side_effect=[RuntimeError("read failed"),snapshot()]
        self.assertEqual(recover_turn(c,base.CID,MARKER,permitted=lambda:True,
                         delays=(0,0),wait=immediate),base.TEXT)
        self.assertEqual(c.get_conversation_snapshot.call_count,2)
        c.send.assert_not_called()
        c.reset_mock()
        self.assertIsNone(recover_turn(c,base.CID,MARKER,permitted=lambda:False,
                          delays=(0,),wait=immediate))
        c.get_conversation_snapshot.assert_not_called()

    def test_transient_errors_and_terminal_failures(self):
        for exc in (error(),error("discovery","ARTIFACT_NOT_FOUND",None),
                    error("resolution","RESOLVER_REQUEST_FAILED",503)):
            self.assertTrue(read_error(exc)[3])
        for exc in (error("verification","SOURCE_SIZE_MISMATCH",200),
                    error("discovery","ARTIFACT_SELECTOR_AMBIGUOUS",None),
                    error("retrieval","ARTIFACT_SIZE_LIMIT_EXCEEDED",200),
                    error(state="published_unverified"),error(http=403),ValueError("private"),
                    RuntimeError("private")):
            self.assertFalse(read_error(exc)[3])
            self.assertNotIn("private",str(read_error(exc)))
        self.assertTrue(recovery_eligible(request_error()))
        self.assertFalse(recovery_eligible(RuntimeError("unknown")))

    def test_read_recovery_then_single_publish_and_durable_claim(self):
        with tempfile.TemporaryDirectory() as d:
            registry=NativeArtifactRegistry(Path(d)/"db")
            c=base.client()
            attempts=[]
            def transfer(*args,**kwargs):
                attempts.append(args[2])
                if len(attempts)==1:
                    raise error()
                return base.handoff(*args,**kwargs)
            c.handoff_generated_artifact.side_effect=transfer
            publish=Mock(return_value={"message_id":88})
            self.assertEqual(base.deliver(registry,c,publish,delays=(0,0),wait=immediate),
                             [(base.NAME,"delivered")])
            self.assertEqual(len(attempts),2)
            self.assertNotEqual(attempts[0],attempts[1])
            publish.assert_called_once()
            self.assertEqual(base.deliver(registry,c,publish),[])
            self.assertEqual(base.recent(registry)[0]["status"],"delivered")

    def test_exhaustion_preserves_safe_reason_and_no_publish(self):
        with tempfile.TemporaryDirectory() as d:
            registry=NativeArtifactRegistry(Path(d)/"db")
            c=base.client()
            c.handoff_generated_artifact.side_effect=error()
            publish=Mock()
            self.assertEqual(base.deliver(registry,c,publish,delays=(0,0),wait=immediate),
                             [(base.NAME,"unavailable")])
            self.assertEqual(c.handoff_generated_artifact.call_count,2)
            publish.assert_not_called()
            self.assertEqual(base.recent(registry)[0]["reason"],"retrieval:READ_TRANSPORT_FAILED:http=200")
            self.assertEqual(base.deliver(registry,c,publish),[])

    def test_ambiguous_telegram_send_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            registry=NativeArtifactRegistry(Path(d)/"db")
            c=base.client()
            publish=Mock(side_effect=TimeoutError("unknown"))
            self.assertEqual(base.deliver(registry,c,publish,delays=(0,0),wait=immediate),
                             [(base.NAME,"delivery_unknown")])
            c.handoff_generated_artifact.assert_called_once()
            publish.assert_called_once()
            self.assertEqual(base.deliver(registry,c,publish),[])

class RecoveryIntegrationTests(unittest.TestCase):
    def test_lost_model_response_recovers_exact_files_without_second_send(self):
        with tempfile.TemporaryDirectory() as d:
            fixture=base.NativeIntegrationTests()
            app,api,c=fixture.setup_app(d)
            def failed(prompt,**kwargs):
                marker=re.search(r"\[AgentRelay recovery identity: [0-9a-f]{32}\]",prompt).group()
                c.get_conversation_snapshot.return_value=snapshot(marker)
                raise request_error()
            c.send.side_effect=failed
            with patch("agent_relay.send_file",return_value={"message_id":88}) as publish:
                app.process_update(fixture.update())
            c.send.assert_called_once()
            c.get_conversation_snapshot.assert_called_once_with(base.CID)
            publish.assert_called_once_with("token","sendDocument",-123,base.DATA,base.NAME,
                                            reply_message_id=41)
            self.assertEqual(base.recent(app.native_artifacts)[0]["status"],"delivered")
            self.assertIn("без повторного запроса",str(api.call.call_args_list))

    def test_recovered_action_is_never_executed_or_delivered(self):
        from action_bridge import OPEN,CLOSE
        with tempfile.TemporaryDirectory() as d:
            fixture=base.NativeIntegrationTests()
            app,api,c=fixture.setup_app(d)
            c.send.side_effect=request_error()
            with patch("agent_relay.recover_turn",return_value=base.TEXT+"\n"+OPEN+"\n{}\n"+CLOSE), \
                 patch("agent_relay.send_file") as publish, \
                 patch.object(app,"_dispatch_native_action") as dispatch:
                app.process_update(fixture.update())
            c.send.assert_called_once()
            publish.assert_not_called()
            dispatch.assert_not_called()
            self.assertIn("не исполнено",str(api.call.call_args_list))

    def test_unconfirmed_turn_notifies_no_replay(self):
        with tempfile.TemporaryDirectory() as d:
            fixture=base.NativeIntegrationTests()
            app,api,c=fixture.setup_app(d)
            c.send.side_effect=request_error()
            with patch("agent_relay.recover_turn",return_value=None), \
                 patch("agent_relay.send_file") as publish:
                app.process_update(fixture.update())
            c.send.assert_called_once()
            publish.assert_not_called()
            self.assertIn("не подтверждён",str(api.call.call_args_list))

if __name__=="__main__":
    unittest.main()
