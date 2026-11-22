"""End-to-end-ish generated-image delivery tests, without live CWA/Telegram."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from relay_media import wants_generated_image


class ImageDeliveryTests(unittest.TestCase):
    def app(self, folder):
        api, main, image_client = Mock(), Mock(), Mock()
        api.call.return_value = {"id": 5, "username": "ragnacobot"}
        config = Config(
            "telegram-secret", -123, "existing-ragna-chat", "auth.json",
            frozenset(), allowed_user_ids=frozenset({10}),
            state_file=str(Path(folder) / "state.json"),
            control_file=str(Path(folder) / "control.json"),
            min_interval_seconds=0, media_enabled=True, media_auto_reply=False,
            bot_aliases=("Рагна",))
        return AgentRelay(config, api, main, image_client=image_client), api, main, image_client

    def update(self, text):
        return {"update_id": 12, "message": {
            "chat": {"id": -123, "type": "supergroup"},
            "message_id": 77,
            "from": {"id": 10, "is_bot": False},
            "text": text,
        }}

    def test_natural_russian_image_requests_are_detected(self):
        for message in (
            "Рагна, нарисуй кота",
            "Рагна, сгенерируй картинку с горами",
            "Рагна, сгенерировать картинку можешь?",
            "Рагна, сделай мне картинку с котом",
            "Рагна, создай изображение леса",
            "Рагна, нарисуйте иллюстрацию города",
            "Рагна, generate a picture of a cat",
            "Рагна, create an image of a mountain",
        ):
            with self.subTest(message=message):
                self.assertTrue(wants_generated_image(message))
        for message in ("проверь мой Python", "как проходит разработка?",
                        "Рагна, создай функцию Python"):
            with self.subTest(message=message):
                self.assertFalse(wants_generated_image(message))

    def test_generation_schedules_monitor_and_keeps_main_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, image_client = self.app(folder)
            main.send.return_value.text = "Изображение готовится."
            image_client.get_generated_images.return_value = []
            with patch.object(app, "_start_image_monitor") as start:
                app.process_update(self.update("Рагна, сделай мне картинку с котом"))
            start.assert_called_once()
            args = start.call_args
            self.assertEqual(args.args[0], set())
            self.assertEqual(args.kwargs["reply_message_id"], 77)
            self.assertIn("CREATE an image", main.send.call_args.args[0])
            self.assertEqual(main.send.call_args.kwargs["model"], "gpt-6")
            self.assertEqual(main.send.call_args.kwargs["reasoning_effort"], "high")
            self.assertTrue(any(x.args[0] == "sendMessage"
                                for x in api.call.call_args_list))

    def test_eventual_readback_sends_exact_image_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, image_client = self.app(folder)
            new_image = Mock(message_id="image-tool-message-1",
                             filename="generated_image.png", size_bytes=32)
            image_client.get_generated_images.side_effect = [
                [], [new_image], [new_image],
            ]
            app.pending_image_baseline = set()
            app._image_monitor_serial = 1
            def handoff(_cid, _filename, destination, **kwargs):
                Path(destination).write_bytes(b"\x89PNG\r\n\x1a\nDATA")
            image_client.handoff_generated_artifact.side_effect = handoff
            with patch("agent_relay.send_file", return_value={"message_id": 9}) as send:
                app._monitor_generated_images(set(), 77, app.control.generation(), 1,
                                              intervals=(0, 0, 0))
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[0], "telegram-secret")
                self.assertEqual(send.call_args.args[1], "sendPhoto")
                self.assertEqual(send.call_args.kwargs["reply_message_id"], 77)
                # Same file is not resent even when discovered again by /images.
                self.assertEqual(app._send_ready_images(set()), 0)
                send.assert_called_once()
            self.assertIsNone(app.pending_image_baseline)
            self.assertEqual(main.send.call_count, 0)

    def test_no_ready_file_sends_honest_status_without_regeneration(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, image_client = self.app(folder)
            image_client.get_generated_images.return_value = []
            app.pending_image_baseline = set()
            app._image_monitor_serial = 1
            app._monitor_generated_images(set(), 77, app.control.generation(), 1,
                                          intervals=(0, 0))
            main.send.assert_not_called()
            posts = [x.args[1]["text"] for x in api.call.call_args_list
                     if x.args[0] == "sendMessage"]
            self.assertEqual(len(posts), 1)
            self.assertIn("нет подтверждённого", posts[0])
            self.assertIn("/images@ragnacobot", posts[0])
            # Delayed artifacts may still be manually checked later.
            self.assertEqual(app.pending_image_baseline, set())

    def test_ambiguous_telegram_publish_is_never_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, image_client = self.app(folder)
            image_client.get_generated_images.return_value = [
                Mock(message_id="id-new", filename="generated.png", size_bytes=32)
            ]
            image_client.handoff_generated_artifact.side_effect = (
                lambda _cid, _filename, dest, **kw:
                Path(dest).write_bytes(b"\x89PNG\r\n\x1a\nDATA"))
            app.pending_image_baseline = set()
            app._image_monitor_serial = 1
            with patch("agent_relay.send_file",
                       side_effect=TimeoutError("ambiguous Telegram response")) as send:
                app._monitor_generated_images(set(), 77, app.control.generation(), 1,
                                              intervals=(0, 0, 0))
                # Readback sees a claimed file; never duplicate the uncertain send.
                self.assertEqual(app._send_ready_images(set()), 0)
                send.assert_called_once()

    def test_stop_prevents_background_image_send(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, main, image_client = self.app(folder)
            app.pending_image_baseline = set()
            app._image_monitor_serial = 1
            app.control.stop()
            app._monitor_generated_images(set(), 77, app.control.generation()-1, 1,
                                          intervals=(0, 0))
            image_client.get_generated_images.assert_not_called()


if __name__ == "__main__":
    unittest.main()
