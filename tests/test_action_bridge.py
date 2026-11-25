import json
import tempfile
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from action_bridge import OPEN, CLOSE, ObservationJournal, parse_action_response
from agent_relay import AgentRelay, Config

TOKEN = "test-turn"
PROJECTS = {"demo": "example/sandbox"}


def trailer(action="start_work", args=None, token=TOKEN):
    return "Обычный ответ\n" + OPEN + "\n" + json.dumps({
        "version": 1, "turn": token, "action": action,
        "arguments": args if args is not None else {"project": "demo", "task": "Add tests"},
    }) + "\n" + CLOSE


class ProtocolTests(unittest.TestCase):
    def parse(self, text, token=TOKEN):
        return parse_action_response(text, token=token, projects=PROJECTS)

    def test_plain_answer_and_json_unchanged(self):
        text = 'Обсудим. {"action":"start_work"}'
        self.assertEqual(self.parse(text).reply, text)
        self.assertEqual(self.parse(text).status, "no_action")

    def test_valid_actions(self):
        for action, args in (
            ("start_work", {"project": "demo", "task": "Add tests"}),
            ("get_work_status", {"work_id": 1}),
            ("get_work_result", {"work_id": 1}),
            ("amend_work", {"work_id": 1, "task": "Add edge case"}),
        ):
            with self.subTest(action=action):
                result = self.parse(trailer(action, args))
                self.assertEqual(result.status, "proposed")
                self.assertEqual(result.action, action)
                self.assertEqual(result.arguments, args)
                self.assertEqual(result.reply, "Обычный ответ")

    def test_reject_bad_scope_and_shapes(self):
        for text in (
            trailer(token="old"), trailer("shell", {"command": "test"}),
            trailer(args={"project": "unknown", "task": "test"}),
            trailer(args={"project": "demo", "task": "test", "extra": True}),
            trailer("amend_work", {"work_id": True, "task": "test"}),
            trailer("get_work_status", {"work_id": 0}),
            trailer().replace('"version": 1', '"version": 1, "version": 1'),
            trailer() + "\ntrailing text", trailer() + "\n" + trailer(),
            "ordinary\n" + OPEN + "\n{bad",
        ):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual(result.status, "rejected")
                self.assertNotIn(OPEN, result.reply)
                self.assertIsNone(result.action)
        self.assertEqual(self.parse(trailer(), None).reason, "ineligible_turn")

    def test_journal_is_bounded_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "journal.json"
            journal = ObservationJournal(str(path))
            for i in range(101):
                self.assertTrue(journal.record(bot_id=5, chat_id=-123, update_id=i,
                                message_id=i, user_id=10, observation=self.parse(trailer())))
            self.assertFalse(journal.record(bot_id=5, chat_id=-123, update_id=100,
                             message_id=100, user_id=10, observation=self.parse(trailer())))
            records = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(records), 100)
            self.assertEqual(records[0]["update_id"], 1)
            self.assertNotIn("reply", records[-1])
            self.assertNotIn("turn", records[-1])


class ObserveTests(unittest.TestCase):
    def make_app(self, d, *, observe=True, ack=True):
        p = Path(d)
        api, main, coord, dev = Mock(), Mock(), Mock(), Mock()
        api.call.return_value = {"id": 5, "username": "YukiBot", "message_id": 301}
        cfg = Config(
            "token", -123, "main", "auth.json", frozenset({"ragnabot"}),
            allowed_user_ids=frozenset({10, 11}), peer_bot_ids=frozenset({20}),
            bot_aliases=("Юки",), work_allowed_user_ids=frozenset({10}),
            work_enabled=True, work_executor="github", natural_work_enabled=True,
            work_live_progress=True, work_projects="demo=" + str(p.resolve()),
            work_repos="demo=example/sandbox", min_interval_seconds=0, media_enabled=False,
            work_state_file=str(p/"work.json"), work_chat_state_file=str(p/"chats.json"),
            work_progress_file=str(p/"progress.json"), state_file=str(p/"cursor.json"),
            control_file=str(p/"control.json"), action_mode="observe" if observe else "off",
            action_observe_read_only_ack=ack, action_observation_file=str(p/"observations.json"))
        return AgentRelay(cfg, api, main, coord, dev), api, main, coord, dev

    def msg(self, text, user=10, bot=False):
        return {"chat": {"id": -123, "type": "supergroup"}, "message_id": 41,
                "from": {"id": user, "is_bot": bot, "username": "ragnabot" if bot else "human"},
                "text": text}

    def test_observe_requires_operator_ack(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self.make_app(d, ack=False)

    def test_legacy_and_explicit_start_cannot_execute(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            msg = self.msg("Юки, создай модуль в demo")
            self.assertFalse(app._handle_natural_work(msg, Queue()))
            self.assertTrue(app._handle_work_command(
                self.msg("/work@YukiBot start demo Add tests"), Queue()))
            self.assertEqual(app.work.record["status"], "idle")
            coord.send.assert_not_called()
            dev.send.assert_not_called()

    def test_main_turn_proposes_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            main.send.return_value.text = trailer()
            msg = self.msg("Юки, создай модуль в demo")
            with patch("agent_relay.new_turn_token", return_value=TOKEN):
                app.process_update({"update_id": 1, "message": msg})
            main.send.assert_called_once()
            self.assertIn("NA-01 observation-only", main.send.call_args.args[0])
            records = json.loads((Path(d)/"observations.json").read_text(encoding="utf-8"))
            self.assertEqual(records[0]["action"], "start_work")
            self.assertEqual(records[0]["status"], "proposed")
            self.assertEqual(app.work.record["status"], "idle")
            coord.send.assert_not_called()
            dev.send.assert_not_called()
            output = str(api.call.call_args_list)
            self.assertNotIn(OPEN, output)
            self.assertNotIn(TOKEN, output)
            self.assertIn("не выполнено", output)

    def test_reply_binds_card_without_legacy_amendment(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            app.work.start("demo")
            app.progress.begin(1, "demo", "test", reply_message_id=41)
            msg = self.msg("Добавь тест")
            msg["reply_to_message"] = {"message_id": 301, "from": {"id": 5, "is_bot": True}}
            q = Queue()
            self.assertTrue(app._handle_task_reply(msg, q, update={"update_id": 2, "message": msg}))
            self.assertEqual(app.work.pending_snapshot(), [])
            event, gen = q.get_nowait()
            self.assertIn('"task_id": 1', event["_task_reply_context"])
            main.send.return_value.text = trailer("amend_work", {"work_id": 1, "task": "Add test"})
            with patch("agent_relay.new_turn_token", return_value=TOKEN):
                app.process_update(event, preadvanced=True, expected_generation=gen,
                                   task_context=event["_task_reply_context"])
            self.assertEqual(app.work.pending_snapshot(), [])
            records = json.loads((Path(d)/"observations.json").read_text(encoding="utf-8"))
            self.assertEqual(records[0]["action"], "amend_work")
            dev.send.assert_not_called()

    def test_unprivileged_and_peer_turns_never_observe_proposal(self):
        for user, bot in ((11, False), (20, True)):
            with self.subTest(user=user), tempfile.TemporaryDirectory() as d:
                app, api, main, coord, dev = self.make_app(d)
                main.send.return_value.text = trailer()
                app.process_update({"update_id": 1, "message": self.msg("@YukiBot hello", user, bot)})
                main.send.assert_called_once()
                self.assertNotIn("NA-01 observation-only", main.send.call_args.args[0])
                self.assertFalse((Path(d)/"observations.json").exists())
                self.assertNotIn(OPEN, str(api.call.call_args_list))
                dev.send.assert_not_called()

    def test_stop_during_cwa_discards_proposal(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d)
            def response(*a, **kw):
                app.control.stop()
                app.control.resume()
                return Mock(text=trailer())
            main.send.side_effect = response
            with patch("agent_relay.new_turn_token", return_value=TOKEN):
                app.process_update({"update_id": 1, "message": self.msg("@YukiBot do it")})
            self.assertFalse((Path(d)/"observations.json").exists())
            self.assertEqual(app.work.record["status"], "idle")
            dev.send.assert_not_called()

    def test_off_keeps_legacy_route(self):
        with tempfile.TemporaryDirectory() as d:
            app, api, main, coord, dev = self.make_app(d, observe=False, ack=False)
            with patch("agent_relay.threading.Thread") as thread:
                self.assertTrue(app._handle_natural_work(
                    self.msg("Юки, создай модуль в demo"), Queue()))
            thread.return_value.start.assert_called_once()
            main.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
