"""NA-07.1 offline lifecycle, crash reconciliation and provenance tests."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_events import EventJournal
from local_codex import WorkState


class DurableEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "work.json"
        self.journal = EventJournal(self.root / "events.sqlite3", "bot-5")

    def work(self):
        return WorkState(str(self.path), event_journal=self.journal)

    def types(self):
        return [e["event_type"] for e in self.journal.read()]

    def test_lifecycle_and_cursor_no_duplicate_stage_events(self):
        work = self.work()
        i = work.start("demo", task="private task", repository="owner/repo")
        work.update(i, "running", 1)
        work.update(i, "running", 2)
        work.finalize(i, "completed", 2, "done")
        self.assertEqual(self.types(), ["work.accepted", "work.started", "work.completed"])
        events = self.journal.read()
        self.assertFalse(events[-1]["payload"]["remote_verified"])
        self.assertNotIn("private task", json.dumps(events))
        self.assertEqual(self.journal.read(after=events[0]["seq"], limit=1), events[1:2])
        self.assertFalse(work.update(i, "failed", note="Telegram error"))
        self.assertEqual(self.work().record["status"], "completed")
        self.assertEqual(len(self.journal.read()), 3)

    def test_failed_append_replays_exact_ids_without_replaying_work(self):
        with patch.object(self.journal, "append", side_effect=sqlite3.OperationalError("busy")):
            work = self.work()
            i = work.start("demo")
            work.update(i, "running")
            work.finalize(i, "failed", 1, "failure")
        pending = json.loads(self.path.read_text())["event_outbox"]
        self.assertEqual(self.journal.read(), [])
        restored = self.work()
        self.assertEqual(restored.record["status"], "failed")
        self.assertEqual([e["event_id"] for e in self.journal.read()],
                         [e["event_id"] for e in pending])
        self.work()
        self.assertEqual(len(self.journal.read()), 3)

    def test_failure_then_next_task_retains_old_events(self):
        with patch.object(self.journal, "append", side_effect=sqlite3.OperationalError("busy")):
            work = self.work()
            for _ in range(32):
                i = work.start("demo")
                work.finalize(i, "failed", 0, "")
        self.assertTrue(work.flush_events())
        self.assertEqual(len(self.journal.read()), 64)
        self.assertEqual(self.journal.read()[0]["work_id"], 1)

    def test_restart_marks_active_interrupted_once(self):
        work = self.work()
        i = work.start("demo")
        work.update(i, "running")
        self.assertEqual(self.work().record["status"], "interrupted")
        self.work()
        self.assertEqual(self.types(), ["work.accepted", "work.started", "work.interrupted"])

    def test_stop_and_late_worker_result(self):
        work = self.work()
        i = work.start("demo")
        self.assertTrue(work.stop())
        self.assertIsNone(work.finalize(i, "completed", 1, "late"))
        self.assertEqual(self.types(), ["work.accepted", "work.stopped"])

    def test_pending_amendment_uses_actual_terminal_status(self):
        work = self.work()
        i = work.start("demo")
        work.add_amendment("private amendment")
        self.assertEqual(work.finalize(i, "completed", 1, "done"), "needs_review")
        self.assertEqual(self.types(), ["work.accepted", "work.needs_review"])
        self.assertNotIn("private amendment", json.dumps(self.journal.read()))

    def test_verified_evidence_must_match_repository_and_branch(self):
        work = self.work()
        i = work.start("demo", repository="owner/repo", branch="work/1")
        result = {"remote_verified": True, "developer_status": "ready",
                  "repository": "owner/repo", "branch": "work/1", "commit_sha": "a"*40}
        work.finalize(i, "completed", 1, "", result=result)
        payload = self.journal.read()[-1]["payload"]
        self.assertTrue(payload["remote_verified"])
        self.assertEqual(payload["commit_sha"], "a"*40)
        self.assertNotIn("ci_status", payload)
        i = work.start("demo", repository="owner/repo", branch="work/2")
        work.finalize(i, "needs_review", 1, "", result=result)
        self.assertFalse(self.journal.read()[-1]["payload"]["remote_verified"])

    def test_identity_and_local_isolation(self):
        other = EventJournal(self.root / "other.sqlite3", "bot-6")
        self.work().start("demo")
        self.assertEqual(other.read(), [])
        with self.assertRaises(ValueError):
            EventJournal(self.journal.path, "bot-6")
        with self.assertRaises(ValueError):
            other.append([self.journal.read()[0]])

    def test_legacy_terminal_not_replayed(self):
        legacy = WorkState(str(self.path))
        i = legacy.start("demo")
        legacy.finalize(i, "completed", 0, "")
        self.assertEqual(self.work().record["status"], "completed")
        self.assertEqual(self.journal.read(), [])

    def test_failed_state_write_does_not_emit_event(self):
        work = self.work()
        with patch("local_codex.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                work.start("demo")
        self.assertEqual(self.journal.read(), [])
        self.assertEqual(self.work().record["status"], "idle")

    def test_conflicting_event_id_is_not_silently_accepted(self):
        self.work().start("demo")
        event = dict(self.journal.read()[0])
        event.pop("seq")
        event["payload"]["project"] = "different"
        with self.assertRaises(ValueError):
            self.journal.append([event])
        self.assertEqual(len(self.journal.read()), 1)
