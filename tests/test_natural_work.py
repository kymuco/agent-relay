"""Natural-language routing, persistent amendments and Telegram rich formatting."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch
from agent_relay import AgentRelay, Config
from local_codex import WorkState
from natural_work import route_natural_work
from telegram_presentation import use_rich_message, rich_payload


def resp(body, cid):
    return Mock(text=body, conversation=Mock(conversation_id=cid))


class NaturalWorkTests(unittest.TestCase):
    def route(self, text, active=None):
        return route_natural_work(
            text, bot_username="ragnacobot", aliases=("Рагна",),
            project_repos={"demo": "kymuco/github-workspace",
                           "other": "kymuco/other-project"},
            active_project=active)

    def make_app(self, folder, rounds=2):
        cfg = Config(
            "secret", -123, "main", "auth.json", frozenset(),
            allowed_user_ids=frozenset({10, 11}),
            work_allowed_user_ids=frozenset({10}),
            work_enabled=True, work_executor="github",
            work_projects="demo=" + str(Path(folder).resolve()),
            work_repos="demo=kymuco/github-workspace",
            work_chat_state_file=str(Path(folder)/"chats.json"),
            work_state_file=str(Path(folder)/"work.json"),
            control_file=str(Path(folder)/"control.json"),
            state_file=str(Path(folder)/"cursor.json"),
            natural_work_enabled=True, work_max_rounds=rounds,
            min_interval_seconds=0)
        api, main, coord, dev = Mock(), Mock(), Mock(), Mock()
        api.call.return_value = {"id": 5, "username": "ragnacobot"}
        return AgentRelay(cfg, api, main, coord, dev), api, main, coord, dev

    def message(self, text, user=10):
        return {"message_id":19, "chat":{"id":-123,"type":"supergroup"},
                "from":{"id":user,"is_bot":False}, "text":text}

    def test_clear_imperative_requires_named_project(self):
        result = self.route("Рагна, добавь калькулятор в github-workspace")
        self.assertEqual((result.kind, result.project), ("start","demo"))
        self.assertEqual(self.route("@ragnacobot создай тесты в other-project").project,
                         "other")
        for phrase in (
            "Добавь калькулятор в github-workspace",
            "Рагна, давай обсудим калькулятор в github-workspace",
            "Рагна, а что если добавим в github-workspace калькулятор?",
            "Рагна, можно ли сделать в github-workspace калькулятор?",
            "Рагна, не делай изменений в github-workspace",
            "Рагна, добавь калькулятор",
            "Рагна, создай в github-workspace и other-project тесты",
            "Рагна, расскажи о github-workspace",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.route(phrase))

    def test_amend_only_active_and_only_same_repo(self):
        self.assertIsNone(self.route("Рагна, учти деление на ноль"))
        self.assertEqual(self.route("Рагна, учти деление на ноль",active="demo").kind,"amend")
        self.assertEqual(self.route("Рагна, добавь ещё тест",active="demo").kind,"amend")
        self.assertIsNone(self.route("Рагна, добавь ещё в other-project тест",active="demo"))

    def test_owner_only_and_natural_start_does_not_call_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coord, dev = self.make_app(folder)
            q = Queue()
            self.assertFalse(app._handle_natural_work(
                self.message("Рагна, добавь калькулятор в github-workspace",user=11),q))
            with patch("agent_relay.threading.Thread") as thread:
                self.assertTrue(app._handle_natural_work(
                    self.message("Рагна, добавь калькулятор в github-workspace"),q))
            self.assertEqual(app.work.active_project(), "demo")
            thread.return_value.start.assert_called_once()
            main.send.assert_not_called()
            self.assertIn("Приняла задачу", api.call.call_args.args[1]["text"])

    def test_state_persists_amendments_and_needs_review(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/"state.json")
            state=WorkState(path)
            task_id=state.start("demo")
            self.assertTrue(state.add_amendment("Учти деление на ноль"))
            self.assertEqual(WorkState(path).pending_snapshot(),["Учти деление на ноль"])
            self.assertEqual(state.finalize(task_id,"completed",1,"verified"),"needs_review")
            self.assertFalse(state.add_amendment("too late"))

    def test_new_amendment_reaches_same_worker_branch(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coord, dev = self.make_app(folder)
            entered, release = threading.Event(), threading.Event()
            coord.send.side_effect = [
                resp('{"action":"delegate","instruction":"code"}',"c"),
                resp('{"action":"delegate","instruction":"revise"}',"c"),
                resp('{"action":"finish","message":"Reviewed revisions."}',"c"),
            ]
            def first(*args, **kwargs):
                entered.set()
                release.wait(4)
                return resp('{"status":"ready","repository":"kymuco/github-workspace",'
                            '"branch":"agentrelay/work-1","commit_sha":"' +
                            "a"*40 + '","summary":"Initial"}',"d")
            worker_answers = iter((
                first,
                lambda *args, **kwargs: resp(
                    '{"status":"ready","repository":"kymuco/github-workspace",'
                    '"branch":"agentrelay/work-1","commit_sha":"' +
                    "b"*40 + '","summary":"Revised"}',"d"),
            ))
            def dev_reply(*args, **kwargs):
                return next(worker_answers)(*args, **kwargs)
            dev.send.side_effect = dev_reply
            with patch("agent_relay.verify_remote_branch", return_value=True) as verify:
                app._handle_natural_work(
                    self.message("Рагна, добавь калькулятор в github-workspace"),Queue())
                self.assertTrue(entered.wait(3), (app.work.record, api.call.call_args_list))
                self.assertTrue(app._handle_natural_work(
                    self.message("Рагна, учти деление на ноль"),Queue()))
                release.set()
                end=time.monotonic()+5
                while time.monotonic()<end and app.work.active(1):
                    time.sleep(0.02)
            self.assertEqual(app.work.record["status"],"completed")
            self.assertEqual(dev.send.call_count,2)
            self.assertEqual(verify.call_count,2)
            self.assertIn("деление на ноль",coord.send.call_args_list[1].args[0])

    def test_late_amendment_is_not_marked_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coord, dev = self.make_app(folder,rounds=1)
            entered,release=threading.Event(),threading.Event()
            def finish(*args,**kwargs):
                entered.set()
                release.wait(4)
                return resp('{"action":"finish","message":"Done."}',"c")
            coordinator_calls = iter((
                lambda *args, **kwargs: resp(
                    '{"action":"delegate","instruction":"code"}',"c"),
                finish,
            ))
            coord.send.side_effect = lambda *a, **k: next(coordinator_calls)(*a, **k)
            dev.send.return_value=resp(
                '{"status":"ready","repository":"kymuco/github-workspace",'
                '"branch":"agentrelay/work-1","commit_sha":"' +
                "a"*40 + '","summary":"Done"}',"d")
            with patch("agent_relay.verify_remote_branch",return_value=True):
                app._handle_natural_work(
                    self.message("Рагна, добавь калькулятор в github-workspace"),Queue())
                self.assertTrue(entered.wait(3), (app.work.record, api.call.call_args_list))
                app._handle_natural_work(self.message(
                    "Рагна, учти деление на ноль"),Queue())
                release.set()
                # Terminal WorkState precedes the Telegram report. Wait for
                # the managed thread itself, not merely the status transition.
                self.assertTrue(app.wait_for_background(timeout=10))
            self.assertEqual(app.work.record["status"],"needs_review")
            posts=[x.args[1]["text"] for x in api.call.call_args_list
                   if x.args[0]=="sendMessage"]
            self.assertTrue(any("не применены" in p for p in posts))


class RichTests(unittest.TestCase):
    def test_rich_eligibility_and_schema(self):
        self.assertTrue(use_rich_message("# Report\n- passed\n- done",mode="auto"))
        self.assertFalse(use_rich_message("Привет! Как дела?",mode="auto"))
        self.assertFalse(use_rich_message("## Heading",mode="off"))
        self.assertFalse(use_rich_message("## Heading",mode="auto",from_bot=True))
        payload=rich_payload("# Header\n- one",chat_id=-123,
                             reply_message_id=19,thread_id=7)
        self.assertEqual(payload["rich_message"]["markdown"],"# Header\n- one")
        self.assertEqual(payload["message_thread_id"],7)
        self.assertNotIn("parse_mode",payload)

    def test_rich_integration_is_one_send(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa=Mock(),Mock()
            api.call.return_value={"id":5,"username":"MyBot"}
            cfg=Config("secret",-123,"main","auth.json",frozenset(),
                       allowed_user_ids=frozenset({10}),
                       telegram_rich_mode="auto",
                       state_file=str(Path(folder)/"cursor.json"),
                       control_file=str(Path(folder)/"control.json"),
                       min_interval_seconds=0,media_enabled=False)
            app=AgentRelay(cfg,api,cwa)
            cwa.send.return_value.text="# Report\n\n- a\n- b"
            app.process_update({"update_id":1,"message":{
                "chat":{"id":-123,"type":"supergroup"},
                "message_id":19,
                "from":{"id":10,"is_bot":False},
                "text":"@MyBot расскажи о коде"}})
            methods=[c.args[0] for c in api.call.call_args_list]
            self.assertIn("sendRichMessage",methods)
            self.assertNotIn("sendMessage",methods)


if __name__=="__main__":
    unittest.main()
