"""NA-06.3: real offline Git worktrees and mocked native task integration."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_workspace import prepare_workspace, validate_root, workspace_record, verify_prepared_workspace
from work_feedback import result_record
from work_card_views import work_keyboard
import test_action_bridge as fixtures
import test_action_dispatch as dispatch

SHA = "a"*40
REPO = "example/sandbox"
IDENTITY = "bot-5-work-1-" + "b"*32


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


class WorkspaceTests(unittest.TestCase):
    def test_root_requires_explicit_separate_existing_directory(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            source = base / "source"
            source.mkdir()
            root = base / "workspaces"
            root.mkdir()
            nested = source / "nested"
            nested.mkdir()
            self.assertEqual(validate_root(str(root), [source]), root)
            for bad in ("", "relative", str(base), str(source), str(nested), str(root/"missing")):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    validate_root(bad, [source])

    @unittest.skipUnless(shutil.which("git"), "Git required for offline worktree tests")
    def test_exact_commit_preserves_dirty_primary_and_disables_filters(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            remote, source, root = (base / name for name in ("remote", "source", "workspaces"))
            remote.mkdir()
            source.mkdir()
            root.mkdir()
            git(remote, "init", "-b", "main")
            git(remote, "config", "user.name", "Test")
            git(remote, "config", "user.email", "test@example.invalid")
            (remote/"value.txt").write_text("old\n", encoding="utf-8")
            (remote/".gitattributes").write_text("value.txt filter=guard\n", encoding="utf-8")
            git(remote, "add", ".")
            git(remote, "commit", "-m", "base")
            git(source, "clone", str(remote), ".")
            original_head = git(source, "rev-parse", "HEAD")
            (source/"value.txt").write_text("dirty", encoding="utf-8")
            (source/"untracked.txt").write_text("keep", encoding="utf-8")
            git(source, "config", "filter.guard.smudge", "false")
            git(source, "config", "filter.guard.required", "true")
            (remote/"value.txt").write_text("new", encoding="utf-8")
            git(remote, "commit", "-am", "next")
            sha = git(remote, "rev-parse", "HEAD")
            (remote/"value.txt").write_text("later", encoding="utf-8")
            git(remote, "commit", "-am", "moving branch")
            # Only origin identity is mocked: actual Git commands stay offline.
            with patch("github_work._github_origin_matches", return_value=True):
                observed = prepare_workspace(source, root, REPO, sha, bot_id=5,
                                             work_id=1, permitted=lambda: True)
            self.assertEqual(observed["status"], "prepared", observed)
            workspace = root / observed["workspace_id"]
            self.assertEqual(git(workspace, "rev-parse", "HEAD"), sha)
            self.assertEqual((workspace/"value.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(git(source, "rev-parse", "HEAD"), original_head)
            self.assertEqual(git(source, "rev-parse", "--abbrev-ref", "HEAD"), "main")
            self.assertEqual((source/"value.txt").read_text(encoding="utf-8"), "dirty")
            self.assertEqual((source/"untracked.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual(git(source, "config", "filter.guard.smudge"), "false")
            self.assertEqual(workspace_record(observed, sha), observed)
            with patch("github_work._github_origin_matches", return_value=True):
                self.assertEqual(verify_prepared_workspace(source, root, REPO, sha,
                    observed["workspace_id"], permitted=lambda: True), workspace)
                self.assertIsNone(verify_prepared_workspace(source, root, REPO, original_head,
                    observed["workspace_id"], permitted=lambda: True))
                (workspace/"value.txt").write_text("changed", encoding="utf-8")
                self.assertIsNone(verify_prepared_workspace(source, root, REPO, sha,
                    observed["workspace_id"], permitted=lambda: True))

    def test_stop_before_git_never_materializes(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            source, root = base/"source", base/"root"
            source.mkdir()
            root.mkdir()
            with patch("local_workspace.subprocess.run") as run:
                self.assertIsNone(prepare_workspace(source, root, REPO, SHA, bot_id=5,
                                                    work_id=1, permitted=lambda: False))
                run.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_record_is_bound_to_verified_sha_and_safe_identifier(self):
        data = dict(status="prepared", commit_sha=SHA, workspace_id=IDENTITY,
                    reason="", observed_at="2026-11-26T12:00:00+00:00")
        self.assertEqual(workspace_record(data, SHA), data)
        self.assertIsNone(workspace_record(dict(data, workspace_id="../evil"), SHA))
        self.assertIsNone(workspace_record(data, "c"*40))
        evidence = dict(developer_status="ready", repository=REPO, branch="test",
                        commit_sha=SHA, remote_verified=True, workspace=data)
        self.assertEqual(result_record(evidence, 0)["workspace"], data)
        self.assertNotIn("workspace", result_record(dict(evidence, remote_verified=False), 0))
        self.assertTrue(any(b.get("callback_data") == "wp:s:1"
                            for row in work_keyboard(1, False, {}, phase="workspace")["inline_keyboard"]
                            for b in row))


class WorkspaceIntegrationTests(unittest.TestCase):
    def test_preparation_preserves_ci_and_never_executes_code(self):
        for outcome in ("prepared", "unavailable"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as d:
                base = Path(d).resolve()
                source, root = base/"source", base/"root"
                source.mkdir()
                root.mkdir()
                fixture = dispatch.ExecuteTests()
                app, api, main, coord, dev = fixture.make_app(
                    str(source), work_workspace_root=str(root), work_local_tests="demo=unittest",
                    work_ci_verify=True, work_ci_wait_seconds=0)
                main.send.return_value.text = fixtures.trailer(args={
                    "project": "demo", "task": "Add tests", "delegation": "direct"})
                with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN), \
                     patch("agent_relay.threading.Thread") as thread:
                    app.process_update(fixture.update())
                    worker = thread.call_args.kwargs["target"]
                branch = app.work.record["branch"]
                dev.send.return_value = dispatch.response(json.dumps({
                    "status": "ready", "repository": REPO, "branch": branch,
                    "commit_sha": SHA, "summary": "3 tests passed",
                    "verification": {"route": "local", "reason": "Authorized local smoke test",
                                     "preset": "unittest"}}), "dev")
                observed = dict(status=outcome, commit_sha=SHA, workspace_id=IDENTITY,
                                reason="" if outcome == "prepared" else "fetch_failed",
                                observed_at="2026-11-26T12:00:00+00:00")
                ci = dict(status="not_configured", commit_sha=SHA, run_count=0, run_ids=[],
                          observed_at="2026-11-26T12:00:00+00:00")
                from test_local_execution import observed as tested
                with patch("agent_relay.verify_remote_branch", return_value=True), \
                     patch("agent_relay.observe_ci", return_value=ci) as observe, \
                     patch("agent_relay.run_local_tests", return_value=tested()) as execute, \
                     patch("agent_relay.prepare_workspace", return_value=observed) as prepare, \
                     patch("local_codex.CodexRunner.run") as codex, \
                     patch("agent_relay.checkout_verified_commit") as legacy:
                    worker()
                prepare.assert_called_once()
                self.assertEqual(prepare.call_args.args, (source, root, REPO, SHA))
                codex.assert_not_called()
                legacy.assert_not_called()
                coord.send.assert_not_called()
                snap = app.work.feedback_snapshot(1)
                self.assertEqual(snap["status"], "completed" if outcome == "prepared" else "needs_review")
                self.assertTrue(snap["result"]["remote_verified"])
                observe.assert_not_called()
                self.assertEqual(execute.call_count, int(outcome == "prepared"))
                self.assertNotIn("ci", snap["result"])
                self.assertEqual(snap["result"]["workspace"], observed)
                self.assertEqual(app.progress.record["result"]["workspace"], observed)


if __name__ == "__main__":
    unittest.main()
