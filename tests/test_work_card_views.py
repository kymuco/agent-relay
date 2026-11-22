"""Display-only Telegram task cards, callbacks and durable journal tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from telegram_presence import WorkProgress
from work_card_views import (github_commit_url, journal_event,
                             render_work_card, render_work_journal, work_keyboard)

SHA = "d4752cee98a63f0ccd916ed211c7238636759322"
REPO = "kymuco/github-workspace"


def sample_result():
    return {
        "developer_summary": (
            "Добавлены healthcheck.py и test_healthcheck.py. "
            "Локально выполнено python -m unittest discover -v: "
            "3 теста пройдены, ошибок нет."
        ),
        "coordinator_summary": "По отчёту работника работа выполнена.",
        "repository": REPO,
        "branch": "agentrelay/work-4",
        "commit_sha": SHA,
        "remote_verified": True,
        "local_check": "Remote branch and SHA independently verified.",
        "pending_amendments": 0,
    }


class WorkCardViewTests(unittest.TestCase):
    def record(self):
        return {"work_id": 4, "project": "demo",
                "task": "Создай модуль healthcheck.py <test> & tests",
                "phase": "completed", "note": "",
                "events": [journal_event("queued"), journal_event("developer"),
                           journal_event("verify")],
                "result": sample_result()}

    def test_compact_card_trusts_reported_tests_and_does_not_repeat_transcript(self):
        card = render_work_card(self.record())
        self.assertIn("3 теста пройдены", card)
        self.assertIn("Коммит подтверждён", card)
        self.assertIn("healthcheck.py", card)
        self.assertIn("test_healthcheck.py", card)
        self.assertNotIn("Координатор самостоятельно", card)
        self.assertNotIn("Локально выполнено", card)
        self.assertIn("&lt;test&gt; &amp; tests", card)

    def test_detail_expands_without_a_new_model_response(self):
        detail = render_work_card(self.record(), detail=True)
        self.assertIn("Результат разработчика", detail)
        self.assertIn("python -m unittest", detail)
        self.assertIn("agentrelay/work-4", detail)
        self.assertIn(SHA, detail)
        self.assertIn("3 теста пройдены", detail)

    def test_tampered_github_url_and_worker_html_not_trusted(self):
        record = self.record()
        record["result"]["repository"] = "evil.example/path"
        record["result"]["developer_summary"] += "<script>alert(1)</script>"
        self.assertIsNone(github_commit_url(record["result"]))
        html = render_work_card(record, detail=True)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        keyboard = work_keyboard(4, False, record["result"])
        self.assertEqual(len(keyboard["inline_keyboard"]), 1)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "wp:d:4")
        self.assertEqual(keyboard["inline_keyboard"][0][1]["callback_data"], "wp:j:4")

    def test_journal_uses_host_events_and_separates_worker_claims(self):
        content = render_work_journal(self.record()).decode("utf-8")
        self.assertIn("События AgentRelay", content)
        self.assertIn("Отчёт доверенного Developer", content)
        self.assertIn("3 теста пройдены", content)
        self.assertIn("Удалённый SHA: подтверждён", content)
        self.assertNotIn("token=", content)
        self.assertNotIn("private tool output", content)

    def test_roundtrip_previous_cards_can_still_expand_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            api = Mock()
            next_id = iter((301, 302))
            api.call.side_effect = lambda m, d, **kw: (
                {"message_id": next(next_id)} if m == "sendMessage" else True)
            path = str(Path(folder) / "progress.json")
            p = WorkProgress(api, -123, path)
            self.assertTrue(p.begin(4, "demo", "Create healthcheck.py", reply_message_id=1))
            self.assertTrue(p.result(4, **sample_result()))
            self.assertTrue(p.update(4, "completed"))
            self.assertTrue(p.begin(5, "demo", "Create other.py", reply_message_id=2))
            restarted = WorkProgress(api, -123, path)
            self.assertEqual(restarted.record["work_id"], 5)
            self.assertTrue(restarted.open_view(4, 301, detail=True))
            self.assertFalse(restarted.open_view(4, 302, detail=True))
            self.assertEqual(WorkProgress(api, -123, path).history["4"]["view"], "detail")
            self.assertIsNotNone(restarted.journal_bytes(4, 301))
            self.assertEqual(len([c for c in api.call.call_args_list
                                  if c.args[0] == "sendMessage"]), 2)

    def test_bounded_history_drops_oldest_and_unknown_callbacks(self):
        with tempfile.TemporaryDirectory() as folder:
            api = Mock()
            api.call.side_effect = lambda m, d, **kw: (
                {"message_id": 1000 + int(d["text"].split("#")[1].split(" ")[0])}
                if m == "sendMessage" else True)
            tracker = WorkProgress(api, -123, str(Path(folder) / "progress.json"))
            for work_id in range(1, 34):
                tracker.begin(work_id, "demo", "Task", reply_message_id=99)
            self.assertNotIn("1", tracker.history)
            self.assertIn("33", tracker.history)
            self.assertFalse(tracker.open_view(1, 1001, detail=True))
            self.assertIsNone(tracker.journal_bytes(1, 1001))


class TelegramCallbackTests(unittest.TestCase):
    def app(self, folder):
        api, main, coord, dev = Mock(), Mock(), Mock(), Mock()
        def call(method, data=None, **kwargs):
            if method == "getMe":
                return {"id": 5, "username": "RagnaBot"}
            if method == "sendMessage":
                return {"message_id": 777}
            return True
        api.call.side_effect = call
        config = Config(
            "telegram-token", -123, "main-chat", "auth.json", frozenset(),
            allowed_user_ids=frozenset({10}),
            work_enabled=True, work_executor="github",
            work_allowed_user_ids=frozenset({10}),
            work_projects="demo=" + str(Path(folder).resolve()),
            work_repos="demo=" + REPO,
            work_chat_state_file=str(Path(folder) / "chats.json"),
            work_state_file=str(Path(folder) / "work.json"),
            work_progress_file=str(Path(folder) / "progress.json"),
            work_live_progress=True, state_file=str(Path(folder) / "state.json"),
            control_file=str(Path(folder) / "control.json"))
        app = AgentRelay(config, api, main, coord, dev)
        app.progress.begin(4, "demo", "Healthcheck", reply_message_id=99)
        app.progress.result(4, **sample_result())
        app.progress.update(4, "completed")
        return app, api, main, coord, dev

    def callback(self, data, *, user=10, chat=-123, mid=777, is_bot=False):
        return {"update_id": 3, "callback_query": {
            "id": "query123", "data": data,
            "from": {"id": user, "is_bot": is_bot},
            "message": {"message_id": mid,
                        "chat": {"id": chat, "type": "supergroup"}}}}

    def test_expand_collapse_only_edits_exact_card_and_acknowledges(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coord, dev = self.app(folder)
            baseline = api.call.call_count
            self.assertTrue(app._handle_progress_callback(self.callback("wp:d:4")))
            self.assertEqual(app.progress.history["4"]["view"], "detail")
            self.assertTrue(app._handle_progress_callback(self.callback("wp:c:4")))
            self.assertEqual(app.progress.history["4"]["view"], "compact")
            methods = [call.args[0] for call in api.call.call_args_list[baseline:]]
            self.assertEqual(methods, ["editMessageText","answerCallbackQuery",
                                       "editMessageText","answerCallbackQuery"])
            main.send.assert_not_called()
            coord.send.assert_not_called()
            dev.send.assert_not_called()

    def test_untrusted_or_replayed_callbacks_cannot_expose_or_modify(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coord, dev = self.app(folder)
            for bad in (
                self.callback("wp:d:4", user=11),
                self.callback("wp:d:4", is_bot=True),
                self.callback("wp:d:4", chat=-999),
                self.callback("wp:d:4", mid=776),
                self.callback("wp:d:3"),
                self.callback("wp:d:4;stop"),
            ):
                before = app.progress.history["4"]["view"]
                self.assertTrue(app._handle_progress_callback(bad))
                self.assertEqual(app.progress.history["4"]["view"], before)
            self.assertFalse(app._handle_progress_callback({"callback_query":{
                "id":"q","data":"other:control",
            }}))
            self.assertEqual(app.progress.history["4"]["view"], "compact")
            self.assertFalse(any(c.args[0] == "editMessageText" and
                                 c.args[1].get("text", "").find("Подробности") >= 0
                                 for c in api.call.call_args_list))
            main.send.assert_not_called()

    def test_journal_sends_one_file_with_cooldown_and_no_model_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coord, dev = self.app(folder)
            with patch("agent_relay.send_file", return_value={"message_id":500}) as send:
                self.assertTrue(app._handle_progress_callback(self.callback("wp:j:4")))
                self.assertTrue(app._handle_progress_callback(self.callback("wp:j:4")))
            send.assert_called_once()
            args = send.call_args.args
            self.assertEqual(args[1], "sendDocument")
            self.assertEqual(args[2], -123)
            self.assertIn(b"Developer", args[3])
            self.assertEqual(args[4], "agentrelay-work-4.txt")
            self.assertEqual(send.call_args.kwargs["reply_message_id"], 777)
            main.send.assert_not_called()

    def test_callback_response_does_not_trigger_work_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, coord, dev = self.app(folder)
            self.assertTrue(app._handle_progress_callback(self.callback("wp:d:4")))
            self.assertEqual(app.work.record["id"], 0)
            self.assertEqual(app.work.record["status"], "idle")


if __name__ == "__main__":
    unittest.main()
