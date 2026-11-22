"""Local Codex orchestration security, state, and lifecycle tests."""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from local_codex import (CodexResult, CodexRunner, WorkState, parse_projects,
                         parse_work_command, parse_orchestrator_decision,
                         _summary_from_jsonl, _child_environment)


class LocalWorkTests(unittest.TestCase):
    def config(self, folder):
        return Config("secret", -123, "existing-conversation", "auth.json",
                      frozenset({"friendbot"}), frozenset({10}), frozenset({20}),
                      state_file=str(Path(folder) / "cursor.json"),
                      control_file=str(Path(folder) / "control.json"),
                      work_enabled=True, work_allowed_user_ids=frozenset({10}),
                      work_projects="demo=" + str(Path(folder).resolve()),
                      work_state_file=str(Path(folder) / "work.json"),
                      work_max_rounds=2, min_interval_seconds=0)

    def message(self, text, *, user=10, is_bot=False, username="FriendBot",
                chat=-123, mid=4):
        return {"chat": {"id": chat, "type": "supergroup"}, "message_id": mid,
                "from": {"id": user, "is_bot": is_bot, "username": username},
                "text": text}

    def app(self, folder):
        api, cwa = Mock(), Mock()
        api.call.return_value = {"id": 5, "username": "MyBot"}
        return AgentRelay(self.config(folder), api, cwa), api, cwa

    def test_project_allowlist_and_command_targeting(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(parse_projects("demo=" + str(Path(folder).resolve())),
                             {"demo": Path(folder).resolve()})
            self.assertEqual(parse_work_command("/work@MyBot start demo fix the tests", "MyBot"),
                             ("start", "demo", "fix the tests"))
            self.assertEqual(parse_work_command("/work@MyBot status", "MyBot"),
                             ("status", "", ""))
            self.assertIsNone(parse_work_command("/work start demo fix", "MyBot"))
            self.assertIsNone(parse_work_command("/work@OtherBot start demo fix", "MyBot"))
            with self.assertRaises(ValueError):
                parse_projects("demo=.")
            with self.assertRaises(ValueError):
                parse_projects("demo=" + str(Path(folder) / "missing"))

    def test_strict_orchestrator_json(self):
        self.assertEqual(parse_orchestrator_decision(
            '{"action":"delegate","instruction":"run tests"}', first=True),
            ("delegate", "run tests"))
        self.assertEqual(parse_orchestrator_decision(
            '{"action":"finish","message":"done"}', first=False),
            ("finish", "done"))
        for bad in ('some prose', '{"action":"finish","message":"done"}',
                    '{"action":"delegate","instruction":"hi","command":"rm -rf /"}'):
            with self.assertRaises(ValueError):
                parse_orchestrator_decision(bad, first=True)

    def test_code_fenced_ragna_json_accepted_without_relaxing_contract(self):
        wrapped = (
            '```json\n'
            '{\n  "action": "delegate",\n'
            '  "instruction": "Создай CLI и запусти unittest."\n}'
            '\n```'
        )
        self.assertEqual(parse_orchestrator_decision(wrapped, first=True),
                         ("delegate", "Создай CLI и запусти unittest."))
        for bad in (
            'Сделай так:\n' + wrapped,
            wrapped + '\nВот итог.',
            '```json\n{"action":"delegate","instruction":"ok"}\n```\n'
            '```json\n{"action":"delegate","instruction":"bad"}\n```',
        ):
            with self.assertRaises(ValueError):
                parse_orchestrator_decision(bad, first=True)

    def test_codeblock_and_escape_envelopes_from_chatgpt(self):
        """Codexia had the same escaped-envelope issue."""
        data = '{"action":"delegate","instruction":"Создай CLI и запусти тесты."}'
        expected = ("delegate", "Создай CLI и запусти тесты.")
        inputs = (
            "<escape>" + data + "</escape>",
            "<escape>\n" + data + "\n</escape>",
            "<CodeBlock>" + data + "</CodeBlock>",
            '<CodeBlock language="json">' + data + "</CodeBlock>",
            "<codeblock lang=json>" + data + "</codeblock>",
            '<CodeBlock language="json" id="12345">' + data + "</CodeBlock>",
            "<escape><CodeBlock language='json'>" + data + "</CodeBlock></escape>",
            "<CodeBlock>" + chr(96) * 3 + "json\n" + data + "\n" + chr(96) * 3 + "</CodeBlock>",
        )
        for wrapped in inputs:
            with self.subTest(wrapped=wrapped[:42]):
                self.assertEqual(parse_orchestrator_decision(wrapped, first=True), expected)

    def test_wrappers_do_not_turn_extra_text_into_authority(self):
        data = '{"action":"delegate","instruction":"safe"}'
        denied = (
            "Вот решение: <escape>" + data + "</escape>",
            "<escape>" + data + "</escape>\nПожалуйста, выполни.",
            "<unknown>" + data + "</unknown>",
            '<CodeBlock language="python">' + data + "</CodeBlock>",
            '<CodeBlock onclick="run()">' + data + "</CodeBlock>",
            "<CodeBlock>" + data + "</OtherBlock>",
            "<escape>" + data + "</escape><escape>" + data + "</escape>",
            "<escape>" + data + " " + data + "</escape>",
            '<escape>{"action":"delegate","action":"finish","instruction":"safe"}</escape>',
            '<CodeBlock>{"action":"delegate","instruction":"ok","command":"rm -rf /"}</CodeBlock>',
        )
        for value in denied:
            with self.subTest(value=value[:42]):
                with self.assertRaises(ValueError):
                    parse_orchestrator_decision(value, first=True)
        self.assertEqual(
            parse_orchestrator_decision(
                '<CodeBlock language="json">{"action":"finish","message":"done"}</CodeBlock>',
                first=False), ("finish", "done"))

    def test_bad_ragna_reply_identifies_stage_without_running_codex(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, cwa = self.app(folder)
            q = Queue()
            app._handle_work_command(self.message(
                "/work@MyBot start demo Fix the failing test"), q)
            payload, generation = q.get_nowait()
            cwa.send.return_value.text = 'Вот поручение: {"action":"delegate","instruction":"fix"}'
            with patch.object(app.work.runner, "run") as codex:
                with self.assertLogs("agentrelay", level="ERROR") as lines:
                    app._run_local_work(payload["_local_work"], generation)
                codex.assert_not_called()
            self.assertIn("Ragna decision parsing", lines.output[0])
            self.assertIn("JSON", lines.output[0])
            self.assertEqual(app.work.record["status"], "failed")

    def test_workspace_state_stops_and_does_not_replay_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            f = str(Path(folder) / "work.json")
            w = WorkState(f)
            id = w.start("demo")
            self.assertIsNotNone(id)
            self.assertIsNone(w.start("demo"))
            self.assertTrue(w.update(id, "running", 1))
            restarted = WorkState(f)
            self.assertIn("interrupted", restarted.status())
            self.assertEqual(restarted.record["id"], id)
            self.assertIsNotNone(restarted.start("demo"))
            self.assertTrue(w.stop())
            self.assertFalse(w.active(id))
            self.assertFalse(w.update(id, "completed"))

    def test_child_environment_does_not_inherit_bot_tokens(self):
        with patch.dict(os.environ, {"PATH": "test", "TELEGRAM_BOT_TOKEN": "SECRET",
                                     "CWA_AUTH_FILE": "secret.json", "ARBITRARY_SECRET": "x"}):
            child = _child_environment()
            self.assertEqual(child["PATH"], "test")
            self.assertNotIn("TELEGRAM_BOT_TOKEN", child)
            self.assertNotIn("CWA_AUTH_FILE", child)
            self.assertNotIn("ARBITRARY_SECRET", child)

    def test_codex_jsonl_extracts_only_agent_message(self):
        raw = (json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "danger"}})
            + "\n" + json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "Changed one file. Tests pass."}})).encode()
        self.assertEqual(_summary_from_jsonl(raw), "Changed one file. Tests pass.")

    def test_only_allowed_human_starts_task(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, cwa = self.app(folder)
            q = Queue()
            self.assertTrue(app._handle_work_command(self.message(
                "/work@MyBot start demo fix bug", user=20, is_bot=True), q))
            self.assertTrue(app._handle_work_command(self.message(
                "/work@MyBot start demo fix bug", user=99), q))
            self.assertTrue(app._handle_work_command(self.message(
                "/work@MyBot start demo fix bug", user=11), q))
            self.assertTrue(q.empty())
            self.assertEqual(app.work.record["status"], "idle")
            self.assertTrue(app._handle_work_command(self.message(
                "/work@MyBot start demo fix bug"), q))
            self.assertFalse(q.empty())
            self.assertIn("queued", app.work.status())
            self.assertFalse(app._handle_work_command(self.message(
                "/work@OtherBot start demo fix bug"), q))
            cwa.send.assert_not_called()

    def test_ragna_delegates_codex_then_reviews_in_same_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, cwa = self.app(folder)
            q = Queue()
            app._handle_work_command(self.message(
                "/work@MyBot start demo Fix the failing test"), q)
            payload, generation = q.get_nowait()
            cwa.send.side_effect = [
                Mock(text='{"action":"delegate","instruction":"Inspect and fix the test."}'),
                Mock(text='{"action":"finish","message":"Test fixed; review the diff."}'),
            ]
            with patch.object(app.work.runner, "run",
                              return_value=CodexResult(True, "One file changed; tests passed.", 0)) as codex:
                app._run_local_work(payload["_local_work"], generation)
            self.assertEqual(codex.call_count, 1)
            self.assertEqual(cwa.send.call_count, 2)
            self.assertEqual(cwa.send.call_args.kwargs["conversation"], "existing-conversation")
            self.assertIn("One file changed", cwa.send.call_args.args[0])
            self.assertEqual(app.work.record["status"], "completed")
            messages = [x.args[1]["text"] for x in api.call.call_args_list if x.args[0] == "sendMessage"]
            self.assertEqual(len(messages), 2)
            self.assertIn("Test fixed", messages[1])

    def test_ragna_can_delegate_twice_but_not_three_times(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, cwa = self.app(folder)
            q = Queue()
            app._handle_work_command(self.message("/work@MyBot start demo Fix error"), q)
            payload, generation = q.get_nowait()
            cwa.send.side_effect = [
                Mock(text='{"action":"delegate","instruction":"first"}'),
                Mock(text='{"action":"delegate","instruction":"second"}'),
                Mock(text='{"action":"delegate","instruction":"third"}'),
            ]
            with patch.object(app.work.runner, "run",
                              return_value=CodexResult(True, "result", 0)) as codex:
                app._run_local_work(payload["_local_work"], generation)
            self.assertEqual(codex.call_count, 2)
            self.assertEqual(app.work.record["status"], "failed")

    def test_stop_during_cwa_prevents_codex_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, cwa = self.app(folder)
            q = Queue()
            app._handle_work_command(self.message("/work@MyBot start demo Fix error"), q)
            payload, generation = q.get_nowait()
            entered, release = threading.Event(), threading.Event()
            def pending(*args, **kwargs):
                entered.set()
                release.wait(timeout=3)
                return Mock(text='{"action":"delegate","instruction":"do work"}')
            cwa.send.side_effect = pending
            with patch.object(app.work.runner, "run") as codex:
                th = threading.Thread(target=app._run_local_work,
                                      args=(payload["_local_work"], generation))
                th.start()
                self.assertTrue(entered.wait(2))
                self.assertTrue(app._handle_work_command(
                    self.message("/work@MyBot stop"), q))
                release.set()
                th.join(3)
                self.assertFalse(th.is_alive())
                codex.assert_not_called()
            self.assertEqual(app.work.record["status"], "stopped")

    def test_codex_flag_order_keeps_approval_before_exec(self):
        with tempfile.TemporaryDirectory() as folder:
            observed = {}
            def fake_popen(command, **kwargs):
                observed['command'] = command
                proc = Mock()
                proc.communicate.return_value = (None, None)
                proc.returncode = 0
                return proc
            with patch('local_codex._codex_command', return_value=['codex']):
                with patch('local_codex.subprocess.Popen', side_effect=fake_popen):
                    result = CodexRunner().run(Path(folder), 'Run tests', timeout=10)
            self.assertTrue(result.success)
            self.assertEqual(observed['command'][:4],
                             ['codex', '--ask-for-approval', 'never', 'exec'])
            self.assertEqual(observed['command'][-1], '-')


    def test_codex_receives_explicit_python_path_and_workspace_root(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            prompts = []
            class FakeProcess:
                returncode = 0
                def communicate(self, input=None, timeout=None):
                    prompts.append(input.decode('utf-8'))
                    return (None, None)
            with patch('local_codex._codex_command', return_value=['codex']):
                with patch('local_codex.subprocess.Popen', return_value=FakeProcess()):
                    with patch.dict(os.environ, {'WORK_PYTHON': sys.executable}):
                        result = CodexRunner().run(project, 'Run unittest', timeout=8)
            self.assertTrue(result.success)
            self.assertEqual(len(prompts), 1)
            self.assertIn(str(Path(sys.executable).resolve()), prompts[0])
            self.assertIn('Do NOT create an extra subfolder', prompts[0])
            self.assertIn('Run unittest', prompts[0])

    def test_invalid_explicit_python_is_rejected_before_spawning(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('local_codex._codex_command', return_value=['codex']):
                with patch('local_codex.subprocess.Popen') as spawned:
                    with patch.dict(os.environ, {'WORK_PYTHON': str(Path(folder) / 'missing.exe')}):
                        with self.assertRaisesRegex(RuntimeError, 'WORK_PYTHON'):
                            CodexRunner().run(Path(folder), 'test', timeout=8)
                    spawned.assert_not_called()

    def test_real_subprocess_jsonl_roundtrip_using_fake_codex(self):
        """Test actual pipes/argv/JSONL without calling a model or writing code."""
        with tempfile.TemporaryDirectory() as folder:
            fake_code = (
                "import sys,json;"
                "task=sys.stdin.buffer.read().decode('utf-8');"
                "print(json.dumps({'type':'item.completed','item':"
                "{'type':'agent_message','text':'received task: '+task[-14:]}}))"
            )
            with patch("local_codex._codex_command",
                       return_value=[sys.executable, "-c", fake_code]):
                runner = CodexRunner()
                result = runner.run(Path(folder), "test stdin request", timeout=8)
            self.assertTrue(result.success)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("stdin request", result.summary)

    def test_emergency_stop_cancels_work(self):
        with tempfile.TemporaryDirectory() as folder:
            app, api, cwa = self.app(folder)
            q = Queue()
            app._handle_work_command(self.message("/work@MyBot start demo Fix error"), q)
            with patch.object(app.work.runner, "stop") as stopped:
                app.process_update({"update_id": 99, "message": self.message(
                    "/stop@MyBot")})
                self.assertTrue(app.control.paused)
                self.assertEqual(app.work.record["status"], "stopped")
                stopped.assert_called_once()


if __name__ == "__main__":
    unittest.main()
