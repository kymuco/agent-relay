import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_relay import AgentRelay, Config
from relay_media import markdown_to_telegram_html
from telegram_presentation import delivery_chunks, utf16_size


class DeliveryTests(unittest.TestCase):
    def test_preserves_paragraph_and_complete_fence(self):
        fence = "\x60\x60\x60python\nprint('hello')\n\x60\x60\x60\n"
        text = "First paragraph.\n\n" + fence + "\nNext paragraph."
        parts = delivery_chunks(text, 35)
        self.assertEqual("".join(x for x, _ in parts), text)
        self.assertTrue(any(fence in x and formatted for x, formatted in parts))
        self.assertTrue(all(utf16_size(x) <= 35 for x, _ in parts))

    def test_large_fence_is_literal_not_reparsed(self):
        fence = "\x60\x60\x60python\n" + "print('abc')\n" * 30 + "\x60\x60\x60\n"
        parts = delivery_chunks(fence, 50)
        self.assertEqual("".join(x for x, _ in parts), fence)
        self.assertTrue(all(not formatted for _, formatted in parts))

    def test_unclosed_fence_is_literal(self):
        text = "\x60\x60\x60python\nhello"
        self.assertEqual(delivery_chunks(text), [(text, False)])

    def test_long_paragraph_prefers_whitespace_and_preserves_unicode(self):
        text = "слово ❤️ 🧠 " * 40
        parts = delivery_chunks(text, 49)
        self.assertEqual("".join(x for x, _ in parts), text)
        self.assertTrue(all(utf16_size(x) <= 49 for x, _ in parts))
        self.assertTrue(all(not formatted for _, formatted in parts))
        self.assertTrue(all(x[-1].isspace() for x, _ in parts[:-1]))

    def test_complete_fence_does_not_insert_newline(self):
        self.assertEqual(markdown_to_telegram_html("\x60\x60\x60py\nabc\n\x60\x60\x60"),
                         "<pre><code>abc</code></pre>")

    def test_verified_turn_requested_and_delivered_once(self):
        with tempfile.TemporaryDirectory() as d:
            api, cwa = Mock(), Mock()
            api.call.return_value = {"id": 5, "username": "MyBot"}
            cfg = Config("secret", -123, "main", "auth.json", frozenset(),
                         allowed_user_ids=frozenset({10}), media_enabled=False,
                         min_interval_seconds=0, state_file=str(Path(d)/"cursor.json"),
                         control_file=str(Path(d)/"control.json"))
            app = AgentRelay(cfg, api, cwa)
            cwa.send.return_value.text = "Обычный проверенный ответ"
            app.process_update({"update_id": 1, "message": {
                "message_id": 7, "chat": {"id": -123, "type": "supergroup"},
                "from": {"id": 10, "is_bot": False}, "text": "@MyBot привет"}})
            cwa.send.assert_called_once()
            self.assertIs(cwa.send.call_args.kwargs["verify_final_text"], True)
            sent = [x.args[1] for x in api.call.call_args_list if x.args[0] == "sendMessage"]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["text"], "Обычный проверенный ответ")
            cwa.send.side_effect = RuntimeError("read failed")
            app.process_update({"update_id": 2, "message": {
                "message_id": 8, "chat": {"id": -123, "type": "supergroup"},
                "from": {"id": 10, "is_bot": False}, "text": "@MyBot ещё"}})
            self.assertEqual(cwa.send.call_count, 2)
            self.assertEqual(len([x for x in api.call.call_args_list
                                  if x.args[0] == "sendMessage"]), 1)
