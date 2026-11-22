"""Non-model Telegram typing and editable background-work progress tests."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from telegram_presence import TypingPulse, WorkProgress, format_work_progress


def reply(text, conversation_id):
    return Mock(text=text, conversation=Mock(conversation_id=conversation_id))


class TypingPresenceTests(unittest.TestCase):
    def test_typing_pulse_emits_immediately_then_stops(self):
        api = Mock()
        with TypingPulse(api, -123, thread_id=42,
                         interval=0.04, max_seconds=1):
            end = time.monotonic() + .3
            while api.call.call_count < 2 and time.monotonic() < end:
                time.sleep(.01)
            self.assertGreaterEqual(api.call.call_count, 2)
            for call in api.call.call_args_list:
                self.assertEqual(call.args[0], "sendChatAction")
                self.assertEqual(call.args[1]["chat_id"], -123)
                self.assertEqual(call.args[1]["action"], "typing")
                self.assertEqual(call.args[1]["message_thread_id"], 42)
        count = api.call.call_count
        time.sleep(.1)
        self.assertEqual(api.call.call_count, count)

    def test_typing_pulse_respects_cancel_and_backend_error(self):
        api = Mock()
        permitted = lambda: False
        with TypingPulse(api, -123, permitted=permitted, interval=0.01):
            time.sleep(.03)
        api.call.assert_not_called()
        api.call.side_effect = RuntimeError("network")
        with TypingPulse(api, -123, interval=.01):
            time.sleep(.06)
        self.assertEqual(api.call.call_count, 1)

    def test_main_chat_typing_does_not_block_model(self):
        with tempfile.TemporaryDirectory() as folder:
            api, model = Mock(), Mock()
            api.call.return_value = {"id": 5, "username": "MyBot"}
            config = Config("secret", -123, "main-conversation", "auth.json",
                            frozenset(), allowed_user_ids=frozenset({10}),
                            chat_typing=True, media_enabled=False,
                            state_file=str(Path(folder)/"cursor.json"),
                            control_file=str(Path(folder)/"control.json"),
                            min_interval_seconds=0)
            app = AgentRelay(config, api, model)
            entered, release = threading.Event(), threading.Event()
            def blocked(*args, **kwargs):
                entered.set()
                release.wait(2)
                return reply("Ок, отвечаю.", "main-conversation")
            model.send.side_effect = blocked
            msg = {"update_id": 1, "message": {
                "chat": {"id": -123, "type": "supergroup"},
                "message_id": 17, "from": {"id":10, "is_bot": False},
                "text": "@MyBot объясни",
            }}
            t = threading.Thread(target=app.process_update,args=(msg,))
            t.start()
            self.assertTrue(entered.wait(1))
            end = time.monotonic()+1
            while not any(c.args[0]=="sendChatAction"
                          for c in api.call.call_args_list) and time.monotonic()<end:
                time.sleep(.01)
            self.assertTrue(any(c.args[0]=="sendChatAction"
                                for c in api.call.call_args_list))
            release.set()
            t.join(2)
            self.assertFalse(t.is_alive())
            self.assertTrue(any(c.args[0]=="sendMessage"
                                for c in api.call.call_args_list))


class WorkProgressTests(unittest.TestCase):
    def api(self):
        api = Mock()
        api.call.side_effect = lambda name, payload, **kwargs: (
            {"message_id": 300} if name=="sendMessage" else True)
        return api

    def test_one_card_edited_not_repeated_and_terminal_immutable(self):
        with tempfile.TemporaryDirectory() as folder:
            api = self.api()
            path = str(Path(folder)/"progress.json")
            tracker = WorkProgress(api,-123,path)
            self.assertTrue(tracker.begin(3,"demo","Fix <calculator> & test",
                                          reply_message_id=19,thread_id=88))
            self.assertFalse(tracker.update(4,"developer"))
            self.assertTrue(tracker.update(3,"coordinator"))
            self.assertFalse(tracker.update(3,"coordinator"))
            self.assertTrue(tracker.update(3,"developer"))
            self.assertTrue(tracker.update(3,"verify"))
            self.assertTrue(tracker.update(3,"completed",note="SHA verified; no local tests"))
            self.assertFalse(tracker.update(3,"failed"))
            self.assertEqual(len([c for c in api.call.call_args_list
                                  if c.args[0]=="sendMessage"]),1)
            self.assertEqual(len([c for c in api.call.call_args_list
                                  if c.args[0]=="editMessageText"]),4)
            initial=api.call.call_args_list[0].args[1]
            self.assertEqual(initial["message_thread_id"],88)
            self.assertIn("&lt;calculator&gt; &amp; test",initial["text"])
            self.assertEqual(WorkProgress(api,-123,path).record["message_id"],300)

    def test_edit_network_failure_does_not_duplicate_card(self):
        with tempfile.TemporaryDirectory() as folder:
            api = self.api()
            tracker = WorkProgress(api,-123,str(Path(folder)/"progress.json"))
            tracker.begin(1,"demo","Run tests",reply_message_id=7)
            api.call.side_effect = RuntimeError("ambiguous edit")
            self.assertFalse(tracker.update(1,"developer"))
            self.assertEqual(tracker.record["phase"],"queued")
            self.assertEqual([c.args[0] for c in api.call.call_args_list],
                             ["sendMessage","editMessageText"])

    def test_bad_id_still_sends_only_one_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            api = Mock()
            api.call.return_value = {"ok": True}
            progress=WorkProgress(api,-123,str(Path(folder)/"progress.json"))
            self.assertFalse(progress.begin(1,"demo","Task",reply_message_id=7))
            self.assertEqual(api.call.call_count,1)

    def test_formatting_never_claims_unobserved_test_success(self):
        msg=format_work_progress(5,"demo","Generate code","verify")
        self.assertIn("Проверяю ветку",msg)
        self.assertNotIn("Тесты пройдены",msg)
        with self.assertRaises(ValueError):
            format_work_progress(5,"demo","test","invented")

    def test_explicit_command_creates_card_before_worker_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            api, main, coordinator, developer = Mock(), Mock(), Mock(), Mock()
            def call(method, data=None, **kwargs):
                if method=="getMe":
                    return {"id":5,"username":"MyBot"}
                if method=="sendMessage":
                    return {"message_id": 301}
                return True
            api.call.side_effect=call
            cfg=Config(
                "secret",-123,"main","auth.json",frozenset(),
                allowed_user_ids=frozenset({10}),
                state_file=str(Path(folder)/"cursor.json"),
                control_file=str(Path(folder)/"control.json"),
                work_enabled=True,work_executor="github",
                work_allowed_user_ids=frozenset({10}),
                work_projects="demo="+str(Path(folder).resolve()),
                work_repos="demo=owner/demo",
                work_chat_state_file=str(Path(folder)/"chats.json"),
                work_state_file=str(Path(folder)/"work.json"),
                work_live_progress=True,
                work_progress_file=str(Path(folder)/"progress.json"))
            app=AgentRelay(cfg,api,main,coordinator,developer)
            msg={"chat":{"id":-123,"type":"supergroup"},"message_id":11,
                 "from":{"id":10,"is_bot":False},
                 "text":"/work@MyBot start demo Build module"}
            with patch("agent_relay.threading.Thread") as thread:
                self.assertTrue(app._handle_work_command(msg,Queue()))
            self.assertEqual(app.progress.record["work_id"],1)
            self.assertEqual(app.progress.record["message_id"],301)
            thread.return_value.start.assert_called_once()
            self.assertEqual([c.args[0] for c in api.call.call_args_list],
                             ["getMe","sendMessage"])

    def test_actual_background_events_update_one_progress_card(self):
        with tempfile.TemporaryDirectory() as folder:
            api, main, coordinator, developer = Mock(), Mock(), Mock(), Mock()
            def call(method, data=None, **kwargs):
                if method=="getMe":
                    return {"id":5,"username":"MyBot"}
                if method=="sendMessage":
                    return {"message_id": 301}
                return True
            api.call.side_effect=call
            cfg=Config(
                "secret",-123,"main","auth.json",frozenset(),
                allowed_user_ids=frozenset({10}),
                state_file=str(Path(folder)/"cursor.json"),
                control_file=str(Path(folder)/"control.json"),
                work_enabled=True,work_executor="github",
                work_allowed_user_ids=frozenset({10}),
                work_projects="demo="+str(Path(folder).resolve()),
                work_repos="demo=owner/demo",
                work_chat_state_file=str(Path(folder)/"chats.json"),
                work_state_file=str(Path(folder)/"work.json"),
                work_live_progress=True,
                work_progress_file=str(Path(folder)/"progress.json"))
            app=AgentRelay(cfg,api,main,coordinator,developer)
            coordinator.send.side_effect=[
                reply('{"action":"delegate","instruction":"build code"}',"coord"),
                reply('{"action":"finish","message":"Verified commit."}',"coord"),
            ]
            developer.send.return_value=reply(
                '{"status":"ready","repository":"owner/demo",'
                '"branch":"agentrelay/work-1","commit_sha":"'+"a"*40+'",'
                '"summary":"Added code"}',"dev")
            msg={"chat":{"id":-123,"type":"supergroup"},"message_id":11,
                 "from":{"id":10,"is_bot":False},
                 "text":"/work@MyBot start demo Build module"}
            with patch("agent_relay.verify_remote_branch",return_value=True):
                app._handle_work_command(msg,Queue())
                deadline=time.monotonic()+3
                while time.monotonic()<deadline and app.work.active(1):
                    time.sleep(.02)
            self.assertEqual(app.work.record["status"],"completed")
            self.assertEqual(app.progress.record["phase"],"completed")
            texts=[c.args[1]["text"] for c in api.call.call_args_list
                   if c.args[0]=="editMessageText"]
            self.assertTrue(any("Составляю план" in t for t in texts))
            self.assertTrue(any("Разработчик работает" in t for t in texts))
            self.assertTrue(any("Проверяю ветку" in t for t in texts))
            self.assertTrue(any("Коммит подтверждён" in t for t in texts))
            self.assertEqual(len([c for c in api.call.call_args_list
                                  if c.args[0]=="sendMessage"]),2)


if __name__ == "__main__":
    unittest.main()
