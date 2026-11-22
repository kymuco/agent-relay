"""Offline bootstrap checks; no real installation or external requests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_supported_python(self):
        self.assertTrue(bootstrap.supported_python((3, 14)))
        self.assertTrue(bootstrap.supported_python((3, 10)))
        self.assertFalse(bootstrap.supported_python((3, 9)))
        self.assertFalse(bootstrap.supported_python((3, 15)))

    def test_node_version(self):
        self.assertEqual(bootstrap.node_major("v24.22.3\n"), 24)
        self.assertEqual(bootstrap.node_major("v26.0.0"), 26)
        self.assertIsNone(bootstrap.node_major("garbage"))

    def test_checksum_manifest_parsing(self):
        h = "a" * 64
        self.assertEqual(
            bootstrap.node_archive_entry(h + "  node-v24.22.3-win-x64.zip\n", "x64"),
            ("node-v24.22.3-win-x64.zip", h),
        )
        with self.assertRaises(RuntimeError):
            bootstrap.node_archive_entry(h + "  node-v26.22.3-win-x64.zip\n", "x64")
        with self.assertRaises(RuntimeError):
            bootstrap.node_archive_entry(h + "  node-v24.22.3-win-x64.zip\n", "x86")

    def test_parse_env_without_executing_content(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / ".env"
            p.write_text("# comment\nTELEGRAM_CHAT_ID=-100123\nMALFORMED\nCWA_AUTH_FILE='auth_data.json'\n", encoding="utf-8")
            self.assertEqual(bootstrap.read_env_values(p), {
                "TELEGRAM_CHAT_ID": "-100123",
                "CWA_AUTH_FILE": "auth_data.json",
            })

    def test_config_creation_does_not_read_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env.example").write_text("TELEGRAM_CHAT_ID=REPLACE_ME\n", encoding="utf-8")
            with patch.object(bootstrap, "ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "Created .env"):
                    bootstrap.validate_local_config()
            self.assertTrue((root / ".env").is_file())

    def test_setup_only_does_not_start_bot(self):
        with patch.object(bootstrap, "validate_python"), \
             patch.object(bootstrap, "setup_venv", return_value=Path("python.exe")), \
             patch.object(bootstrap, "install_requirements"), \
             patch.object(bootstrap, "installed_node", return_value="node"), \
             patch.object(bootstrap, "validate_local_config") as validate, \
             patch.object(bootstrap.subprocess, "call") as launch:
            self.assertEqual(bootstrap.main(["--setup-only"]), 0)
            validate.assert_not_called()
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
