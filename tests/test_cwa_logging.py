"""CWA outbound request lifecycle logs are useful and do not leak payloads."""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config


class CwaRequestLogTests(unittest.TestCase):
    def create(self, root):
        api, cwa = Mock(), Mock()
        api.call.return_value = {"id": 5, "username": "MyBot"}
        config = Config("secret", -123, "existing-conversation", "auth.json",
                        frozenset(), allowed_user_ids=frozenset({10}),
                        state_file=str(Path(root) / "cursor.json"),
                        control_file=str(Path(root) / "control.json"))
        return AgentRelay(config, api, cwa), cwa

    def test_dispatch_and_completion_with_stable_id_and_no_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            relay, cwa = self.create(folder)
            cwa.send.return_value.text = "secret response body"
            with self.assertLogs("agentrelay", level="DEBUG") as records:
                result = relay._cwa_send("secret request body", purpose="work#7.plan1",
                                         conversation="existing-conversation")
                relay._cwa_send("second secret", purpose="chat", conversation="existing-conversation")
            self.assertEqual(result.text, "secret response body")
            self.assertEqual(cwa.send.call_count, 2)
            output = "\n".join(records.output)
            self.assertIn("CWA #1 → work#7.plan1", output)
            self.assertIn("CWA #1 ✓ work#7.plan1", output)
            self.assertIn("CWA #2 → chat", output)
            self.assertIn("CWA #2 ✓ chat", output)
            self.assertIn("с", output)
            for private in ("secret request body", "secret response body",
                            "existing-conversation", "second secret"):
                self.assertNotIn(private, output)

    def test_failed_send_logs_once_and_raises_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            relay, cwa = self.create(folder)
            cwa.send.side_effect = TimeoutError("secret error details")
            with self.assertLogs("agentrelay", level="DEBUG") as records:
                with self.assertRaises(TimeoutError):
                    relay._cwa_send("sensitive prompt", purpose="catchup", conversation="existing")
            self.assertEqual(cwa.send.call_count, 1)
            output = "\n".join(records.output)
            self.assertIn("CWA #1 → catchup", output)
            self.assertIn("CWA #1 × catchup · ошибка TimeoutError", output)
            self.assertNotIn("secret error details", output)
            self.assertNotIn("sensitive prompt", output)

    def test_request_events_are_debug_only(self):
        with tempfile.TemporaryDirectory() as folder:
            relay, cwa = self.create(folder)
            with self.assertLogs("agentrelay", level="INFO") as records:
                logging.getLogger("agentrelay").info("lifecycle")
                relay._cwa_send("private", purpose="chat", conversation="existing")
            self.assertEqual(records.output, ["INFO:agentrelay:lifecycle"])


if __name__ == "__main__":
    unittest.main()
