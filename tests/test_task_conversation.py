"""Task Conversation v1: exact Reply routing, bounded amendment and confirmed stop."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from task_conversation import classify_task_reply, trusted_task_context
from telegram_presence import WorkProgress

REPOS = {"demo": "kymuco/github-workspace", "other": "example/other-project"}


def reply_text(text, message_id=301, *, user=10, bot=5, chat=-123):
    return {
        "message_id": 700 + message_id,
        "chat": {"id": chat, "type": "supergroup"},
        "from": {"id": user, "is_bot": False},
        "reply_to_message": {"message_id": message_id, "from": {"id": bot, "is_bot": True}},
        "text": text,
    }


class ClassifierTests(unittest.TestCase):
    def route(self, text):
        return classify_task_reply(
            text, bot_username="RagnaBot", aliases=("Рагна",),
            project="demo", project_repos=REPOS)

    def test_clear_amendment_v_question(self):
        for text in ("Добавь проверку деления на ноль", "Рагна, учти краевой случай",
                     "добавь ещё unittest", "исправь ошибку в тестах",
                     "уточнение: используй стандартную библиотеку"):
            with self.subTest(text=text):
                self.assertEqual(self.route(text), "amend")
        for text in ("Почему unittest?", "А если использовать pytest?",
                     "Можно ли добавить кеш?", "Добавь ещё тест?",
                     "Не добавляй больше тестов", "А что если исправим это?",
                     "Рагна, расскажи о результате", "добавь ещё в other-project файл"):
            with self.subTest(text=text):
                self.assertEqual(self.route(text), "question")
        self.assertEqual(self.route("Что там?"), "status")
        self.assertEqual(self.route("на каком этапе"), "status")
        self.assertEqual(self.route("останови работу"), "stop_info")
        self.assertIsNone(self.route("@YukiBot проверь код"))
        self.assertIsNone(self.route("/work@RagnaBot stop"))

    def test_context_is_bounded_and_read_only(self):
        content = trusted_task_context({
            "work_id": 4, "project": "demo", "phase": "completed",
            "task": "build code", "result": {
                "developer_summary": "Added code. 3 tests passed.",
                "branch": "agentrelay/work-4",
                "repository": "kymuco/github-workspace",
                "commit_sha": "a" * 40,
                "remote_verified": True,
            }})
        self.assertIn("EXACT prior task", content)
        self.assertIn("agentrelay/work-4", content)
        self.assertIn("3 tests passed", content)
        self.assertIn("read-only DATA", content)


class TaskReplyTests(unittest.TestCase):
    def app(self, directory):
        api, main, coord, dev = Mock(), Mock(), Mock(), Mock()
        ids = iter(range(301, 400))
        def call(name, payload=None, **kwargs):
            if name == "getMe":
                return {"id": 5, "username": "RagnaBot"}
            if name == "sendMessage":
                return {"message_id": next(ids)}
            return True
        api.call.side_effect = call
        cfg = Config(
            "telegram-token", -123, "main-chat", "auth.json", frozenset(),
            allowed_user_ids=frozenset({10, 12}),
            work_allowed_user_ids=frozenset({10}),
            work_enabled=True, work_executor="github", work_live_progress=True,
            natural_work_enabled=True,
            work_projects="demo=" + str(Path(directory).resolve()),
            work_repos="demo=kymuco/github-workspace",
            work_state_file=str(Path(directory)/"work.json"),
            work_chat_state_file=str(Path(directory)/"chats.json"),
            work_progress_file=str(Path(directory)/"progress.json"),
            state_file=str(Path(directory)/"state.json"),
            control_file=str(Path(directory)/"control.json"),
            min_interval_seconds=0, media_enabled=False)
        app = AgentRelay(cfg, api, main, coord, dev)
        work_id = app.work.start("demo")
        self.assertEqual(work_id, 1)
        self.assertTrue(app.progress.begin(1, "demo", "Create calculator",
                                           reply_message_id=91))
        return app, api, main, coord, dev

    def test_task_reply_requires_exact_bot_id_and_message_id(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            good = reply_text("Добавь ещё тест")
            self.assertIsNotNone(app.progress.card_for_reply(good, bot_id=5))
            self.assertIsNone(app.progress.card_for_reply(
                reply_text("Добавь ещё тест", message_id=302), bot_id=5))
            self.assertIsNone(app.progress.card_for_reply(
                reply_text("Добавь ещё тест", bot=6), bot_id=5))
            self.assertIsNone(app.progress.card_for_reply(
                reply_text("Добавь ещё тест", chat=-987), bot_id=5))

    def test_reply_amendment_applied_to_exact_work_id_not_a_new_one(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            queue = Queue()
            good = reply_text("Рагна, добавь проверку деления на ноль")
            self.assertTrue(app._handle_task_reply(good, queue, update={
                "update_id": 30, "message": good}))
            self.assertEqual(app.work.pending_snapshot(),
                             ["добавь проверку деления на ноль"])
            end = time.monotonic() + 2
            while (app.progress.record.get("pending_changes") != 1
                   and time.monotonic() < end):
                time.sleep(.01)
            self.assertEqual(app.progress.record["pending_changes"], 1)
            self.assertEqual(queue.qsize(), 0)
            deadline = time.monotonic() + 2
            while ("Уточнения в очереди" not in app.progress.record["html"]
                   and time.monotonic() < deadline):
                time.sleep(.01)
            self.assertIn("Уточнения в очереди",
                          app.progress.record["html"])
            main.send.assert_not_called()
            self.assertEqual(app.work.record["id"], 1)

    def test_old_task_reply_cannot_change_current_work(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            app.work.finalize(1, "completed", 1, "finished")
            app.progress.update(1, "completed")
            self.assertEqual(app.work.start("demo"), 2)
            app.progress.begin(2, "demo", "Create other module",
                               reply_message_id=92)
            reply = reply_text("Добавь ещё тест", message_id=301)
            self.assertTrue(app._handle_task_reply(reply, Queue(), update={
                "update_id": 60, "message": reply}))
            self.assertEqual(app.work.pending_snapshot(), [])
            self.assertEqual(app.work.record["id"], 2)
            self.assertTrue(any("другой задаче" in call.args[1]["text"]
                                for call in api.call.call_args_list
                                if call.args[0] == "sendMessage"))
            main.send.assert_not_called()

    def test_authorized_reader_can_question_but_not_change_work(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            queue = Queue()
            msg = reply_text("Добавь ещё тест", user=12)
            self.assertTrue(app._handle_task_reply(msg, queue, update={
                "update_id": 50, "message": msg}))
            self.assertEqual(app.work.pending_snapshot(), [])
            self.assertIn("недостаточно прав",
                          api.call.call_args.args[1]["text"])
            asked = reply_text("Почему ты выбрала unittest?", user=12)
            update = {"update_id": 51, "message": asked}
            self.assertTrue(app._handle_task_reply(asked, queue, update=update))
            task_update, generation = queue.get_nowait()
            self.assertIn("task_id", task_update["_task_reply_context"])
            self.assertIn('"task_id": 1', task_update["_task_reply_context"])
            self.assertEqual(task_update["message"]["text"],
                             "Почему ты выбрала unittest?")
            self.assertEqual(app.work.pending_snapshot(), [])
            main.send.assert_not_called()
            main.send.return_value.text = "Unittest — в стандартной библиотеке."
            app.process_update(task_update, preadvanced=True,
                               expected_generation=generation,
                               task_context=task_update["_task_reply_context"])
            self.assertIn('"task_id": 1', main.send.call_args.args[0])
            coord.send.assert_not_called()
            dev.send.assert_not_called()

    def test_status_reply_is_immediate_without_model(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            msg = reply_text("Что там?")
            self.assertTrue(app._handle_task_reply(msg, Queue(), update={
                "update_id": 33, "message": msg}))
            self.assertIn("Задача #1", api.call.call_args.args[1]["text"])
            main.send.assert_not_called()

    def test_stop_replies_do_not_stop_work_without_button(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            msg = reply_text("Останови задачу")
            self.assertTrue(app._handle_task_reply(msg, Queue(), update={
                "update_id": 34, "message": msg}))
            self.assertTrue(app.work.active(1))
            self.assertIn("кнопкой", api.call.call_args.args[1]["text"])

    def callback(self, action, work_id=1, mid=301, user=10):
        return {"callback_query": {
            "id": "cb", "data": f"wp:{action}:{work_id}",
            "from": {"id": user, "is_bot": False},
            "message": {"chat": {"id": -123, "type": "supergroup"},
                        "message_id": mid},
        }}

    def _await(self, predicate, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not predicate():
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_stop_button_requires_confirmation_from_same_authorized_user(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            self.assertIn("wp:s:1",
                          str(app.progress._markup(app.progress.record)))
            self.assertTrue(app._handle_progress_callback(self.callback("s")))
            self._await(lambda: app.progress.stop_is_confirmed(1, 301, 10))
            self.assertTrue(app.work.active(1))
            self.assertTrue(app._handle_progress_callback(
                self.callback("y", user=12)))
            self.assertTrue(app.work.active(1))
            self.assertTrue(app._handle_progress_callback(self.callback("y")))
            self.assertFalse(app.work.active(1))
            self._await(lambda: app.progress.record["phase"] == "stopped")
            self.assertNotIn("wp:s:1",
                             str(app.progress._markup(app.progress.record)))
            main.send.assert_not_called()
            coord.send.assert_not_called()
            dev.send.assert_not_called()

    def test_cancel_and_expired_confirmation_prevent_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            app._handle_progress_callback(self.callback("s"))
            self._await(lambda: app.progress.stop_is_confirmed(1, 301, 10))
            app._handle_progress_callback(self.callback("n"))
            self._await(lambda: not app.progress.record.get("stop_confirm_user"))
            self.assertTrue(app.work.active(1))
            app._handle_progress_callback(self.callback("y"))
            self.assertTrue(app.work.active(1))
            app._handle_progress_callback(self.callback("s"))
            self._await(lambda: app.progress.stop_is_confirmed(1, 301, 10))
            app.progress.record["stop_confirm_until"] = time.time() - 70
            app._handle_progress_callback(self.callback("y"))
            self.assertTrue(app.work.active(1))

    def test_stale_confirm_never_stops_new_work(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            app._handle_progress_callback(self.callback("s"))
            self._await(lambda: app.progress.stop_is_confirmed(1, 301, 10))
            app.work.finalize(1, "completed", 1, "finished")
            app.progress.update(1, "completed")
            self.assertEqual(app.work.start("demo"), 2)
            app.progress.begin(2, "demo", "A different task", reply_message_id=92)
            app._handle_progress_callback(self.callback("y", work_id=1))
            self.assertTrue(app.work.active(2))
            self.assertEqual(app.work.record["status"], "queued")

    def test_work_state_atomic_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            app, api, main, coord, dev = self.app(directory)
            self.assertFalse(app.work.stop(expected_id=2))
            self.assertFalse(app.work.add_amendment("wrong task", expected_id=2))
            self.assertTrue(app.work.active(1))
            self.assertTrue(app.work.add_amendment("allowed", expected_id=1))


if __name__ == "__main__":
    unittest.main()
