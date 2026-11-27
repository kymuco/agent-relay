"""Native CWA artifacts: verify bytes, persist delivery, no GitHub or replay."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from native_artifacts import (native_filenames, verified_bytes, deliver_native,
                              NativeArtifactRegistry, receipt_context)
from agent_relay import AgentRelay, Config

DATA = b"native file bytes"
DIGEST = hashlib.sha256(DATA).hexdigest()
CID = "main"
NAME = "report.xlsx"
TEXT = "[Report](sandbox:/mnt/data/report.xlsx)"
IDENTITY = dict(bot_id=5,chat_id=-123,user_id=10,update_id=2,
                request_message_id=41,conversation_id=CID)

def handoff(*args, **kwargs):
    cid, name, destination = args
    destination.write_bytes(DATA)
    return Mock(conversation_id=cid,source_filename=name,destination=destination.absolute(),
                size_bytes=len(DATA),sha256=DIGEST,integrity_verified=True,overwritten=False)

def client():
    cwa=Mock()
    cwa.handoff_generated_artifact.side_effect=handoff
    return cwa

def recent(registry, **changes):
    identity={k:v for k,v in IDENTITY.items() if k in ("bot_id","chat_id","user_id","conversation_id")}
    identity.update(changes)
    return registry.recent(**identity)

def deliver(registry, cwa, publish, **changes):
    options=dict(IDENTITY,text=TEXT,client=cwa,max_bytes=1024,publish=publish,permitted=lambda:True)
    options.update(changes)
    return deliver_native(registry,**options)

class NativeContractTests(unittest.TestCase):
    def test_exact_links_extensions_and_rejected_paths(self):
        value = ("[x](sandbox:/mnt/data/report.xlsx) [p](sandbox:/mnt/data/slides.pptx) "
                 "sandbox:/mnt/data/report.xlsx sandbox:/mnt/data/report.pdf/nested "
                 "sandbox:/mnt/data/report.pdf?x=y sandbox:/mnt/data/con.pdf "
                 "sandbox:/mnt/data/bad.exe sandbox:/mnt/data/../secret.pdf "
                 "sandbox:/mnt/data/a.docx sandbox:/mnt/data/b.zip")
        self.assertEqual(native_filenames(value),["report.xlsx","slides.pptx","a.docx"])

    def test_reuses_cwa_receipt_and_verifies_bytes(self):
        cwa=client()
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            data,digest=verified_bytes(cwa,CID,NAME,folder,max_bytes=1024)
            self.assertEqual((data,digest),(DATA,DIGEST))
            cwa.handoff_generated_artifact.assert_called_once_with(
                CID,NAME,folder/NAME,overwrite=False,max_bytes=1024)

    def test_rejects_wrong_identity_or_integrity(self):
        for attr, value in (("conversation_id","other"),("source_filename","other.xlsx"),
                            ("sha256","0"*64),("size_bytes",100),("integrity_verified",False),
                            ("overwritten",True),("destination",Path("other"))):
            with self.subTest(attr=attr), tempfile.TemporaryDirectory() as d:
                cwa=client()
                def altered(*args,**kwargs):
                    result=handoff(*args,**kwargs)
                    setattr(result,attr,value)
                    return result
                cwa.handoff_generated_artifact.side_effect=altered
                with self.assertRaises(ValueError):
                    verified_bytes(cwa,CID,NAME,Path(d),max_bytes=1024)

    def test_persists_verified_delivery_and_never_replays(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"receipts.sqlite3"
            registry=NativeArtifactRegistry(path)
            cwa=client()
            publish=Mock(return_value={"message_id":88})
            self.assertEqual(deliver(registry,cwa,publish),[(NAME,"delivered")])
            publish.assert_called_once_with(DATA,NAME)
            reopened=NativeArtifactRegistry(path)
            self.assertEqual(deliver(reopened,cwa,publish),[])
            self.assertEqual(deliver(reopened,cwa,publish,update_id=3),[])
            cwa.handoff_generated_artifact.assert_called_once()
            receipt=recent(reopened)[0]
            self.assertEqual((receipt["size"],receipt["sha256"],receipt["telegram_message_id"]),
                             (len(DATA),DIGEST,88))
            self.assertEqual(receipt["status"],"delivered")
            self.assertEqual(receipt["source"],"cwa_native")
            self.assertIn("DATA ONLY",receipt_context([receipt]))
            self.assertNotIn(str(path),str(receipt))
            for scope in (dict(user_id=11),dict(bot_id=6),dict(chat_id=-999),dict(conversation_id="other")):
                self.assertEqual(recent(reopened,**scope),[])

    def test_ambiguous_publish_and_failed_handoff_never_repeat(self):
        for fail_read in (True,False):
            with self.subTest(fail_read=fail_read), tempfile.TemporaryDirectory() as d:
                registry=NativeArtifactRegistry(Path(d)/"db")
                cwa=client()
                publish=Mock(side_effect=TimeoutError("private token"))
                if fail_read:
                    cwa.handoff_generated_artifact.side_effect=RuntimeError("private locator")
                expected="unavailable" if fail_read else "delivery_unknown"
                self.assertEqual(deliver(registry,cwa,publish),[(NAME,expected)])
                self.assertEqual(deliver(registry,cwa,publish),[])
                self.assertEqual(publish.call_count,0 if fail_read else 1)
                receipt=recent(registry)[0]
                self.assertEqual(receipt["status"],expected)
                self.assertNotIn("private",str(receipt))

    def test_stop_after_read_and_interrupted_claim(self):
        with tempfile.TemporaryDirectory() as d:
            registry=NativeArtifactRegistry(Path(d)/"db")
            allowed=[True]
            cwa=client()
            def stop(*args,**kwargs):
                result=handoff(*args,**kwargs)
                allowed[0]=False
                return result
            cwa.handoff_generated_artifact.side_effect=stop
            publish=Mock()
            deliver(registry,cwa,publish,permitted=lambda:allowed[0])
            publish.assert_not_called()
            self.assertEqual(recent(registry)[0]["status"],"stopped")
            registry.claim(**dict(IDENTITY,filename="other.pdf",update_id=3,request_message_id=42))
            self.assertEqual(recent(registry)[0]["status"],"interrupted_or_unknown")

class NativeIntegrationTests(unittest.TestCase):
    def setup_app(self,folder,*,enabled=True):
        root=Path(folder)
        api,cwa=Mock(),client()
        api.call.return_value={"id":5,"username":"MyBot","message_id":301}
        cwa.send.return_value=Mock(text=TEXT,conversation=Mock(conversation_id=CID))
        config=Config("token",-123,CID,"auth.json",frozenset(),
                      allowed_user_ids=frozenset({10}),min_interval_seconds=0,
                      media_enabled=True,native_artifact_registry=enabled,
                      verify_final_text=False,voice_reply_mode="off",
                      state_file=str(root/"cursor.json"),control_file=str(root/"control.json"))
        return AgentRelay(config,api,cwa),api,cwa

    def update(self,*,mid=41,uid=10,bot=False,update_id=2,text="@MyBot создай XLSX"):
        return {"update_id":update_id,"message":{"message_id":mid,"chat":{"id":-123,"type":"supergroup"},
                    "from":{"id":uid,"is_bot":bot,"username":"human"},"text":text}}

    def test_standalone_chat_delivery_without_github_and_receipt_context(self):
        with tempfile.TemporaryDirectory() as d:
            app,api,cwa=self.setup_app(d)
            with patch("agent_relay.send_file",return_value={"message_id":88}) as publish:
                app.process_update(self.update())
                publish.assert_called_once_with("token","sendDocument",-123,DATA,NAME,reply_message_id=41)
                self.assertTrue(cwa.send.call_args.kwargs["verify_final_text"])
                self.assertIsNone(app.work)
                self.assertEqual(recent(app.native_artifacts)[0]["status"],"delivered")
                cwa.send.return_value.text="Файл отправлен."
                app.process_update(self.update(mid=42,update_id=3,text="@MyBot что отправлено?"))
                self.assertIn("native artifact receipts: DATA ONLY",cwa.send.call_args.args[0])
                self.assertIn(DIGEST,cwa.send.call_args.args[0])
                publish.assert_called_once()

    def test_wrong_conversation_never_retrieves(self):
        with tempfile.TemporaryDirectory() as d:
            app,api,cwa=self.setup_app(d)
            cwa.send.return_value.conversation.conversation_id="wrong"
            with patch("agent_relay.send_file") as publish:
                app.process_update(self.update())
                cwa.handoff_generated_artifact.assert_not_called()
                publish.assert_not_called()
                self.assertEqual(recent(app.native_artifacts),[])

if __name__=="__main__":
    unittest.main()
