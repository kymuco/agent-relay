"""Direct single-process launch and quiet logging contract."""
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import agent_relay
import bootstrap
from agent_relay import AgentRelay, Config
from relay_logging import LOG, configure_logging, close_logging


class LaunchTests(unittest.TestCase):
    def tearDown(self):
        close_logging()

    def test_console_quiet_with_debug_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder) / "app.log"
            with patch("relay_logging.sys.stdout", new_callable=io.StringIO) as out:
                configure_logging("normal", str(log_path))
                try:
                    LOG.info("started")
                    LOG.debug("update=123")
                    LOG.warning("connection issue")
                    self.assertIn("started", out.getvalue())
                    self.assertIn("connection issue", out.getvalue())
                    self.assertNotIn("update=123", out.getvalue())
                    self.assertIn("update=123", log_path.read_text(encoding="utf-8"))
                finally:
                    close_logging()
            log_path.unlink()
            self.assertFalse(log_path.exists())

    def test_quiet_and_no_file(self):
        with patch("relay_logging.sys.stdout", new_callable=io.StringIO) as out:
            configure_logging("quiet", "off")
            LOG.info("invisible")
            LOG.warning("visible")
            self.assertNotIn("invisible", out.getvalue())
            self.assertIn("visible", out.getvalue())
            self.assertFalse(any(isinstance(h, logging.FileHandler) for h in LOG.handlers))
        with self.assertRaisesRegex(ValueError, "LOG_LEVEL"):
            configure_logging("trace", "off")

    def test_shutdown_invalidates_work_without_persistent_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {"id": 5, "username": "MyBot"}
            cfg = Config("secret", -123, "existing", "auth.json", frozenset(),
                         allowed_user_ids=frozenset({10}),
                         state_file=str(Path(folder)/"cursor.json"),
                         control_file=str(Path(folder)/"control.json"))
            app = AgentRelay(cfg, api, cwa)
            generation = app.control.generation()
            self.assertTrue(app._run_permitted(generation))
            app.shutdown()
            app.shutdown()
            self.assertFalse(app._run_permitted(generation))
            self.assertFalse(app.control.paused)

    def test_portable_node_added_for_direct_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / ".runtime").mkdir()
            (base / ".runtime" / "node.exe").write_bytes(b"node")
            with patch.object(agent_relay.os, "name", "nt"), patch.dict(os.environ, {"PATH": "original"}):
                agent_relay.prepare_local_runtime(base)
                self.assertTrue(os.environ["PATH"].startswith(str(base / ".runtime") + os.pathsep))

    def test_bootstrap_ctrl_c_is_clean(self):
        with patch.object(bootstrap, "validate_python"), patch.object(bootstrap, "setup_venv",
                return_value=Path("python.exe")), patch.object(bootstrap, "install_requirements"), \
             patch.object(bootstrap, "installed_node", return_value="node"), \
             patch.object(bootstrap, "validate_local_config"), \
             patch.object(bootstrap.subprocess, "call", side_effect=KeyboardInterrupt):
            self.assertEqual(bootstrap.main([]), 0)

    def test_powershell_launcher_has_no_bootstrap(self):
        content = (Path(__file__).resolve().parent.parent / "run.ps1").read_text(encoding="utf-8")
        self.assertIn("agent_relay.py", content)
        self.assertNotIn("bootstrap.py", content)


if __name__ == "__main__":
    unittest.main()
