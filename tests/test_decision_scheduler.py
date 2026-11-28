"""NA-07.2 deterministic event classification and durable cursor tests."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_events import EventJournal, transition_event
from decision_scheduler import DecisionScheduler
from agent_relay import AgentRelay


class DecisionSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = EventJournal(self.root / "events.sqlite3", "bot-5")
        self.scheduler = DecisionScheduler(self.journal)

    def emit(self, *statuses):
        events = [transition_event("bot-5", {"id": i + 1, "project": "demo",
                   "stage": 0, "status": status}) for i, status in enumerate(statuses)]
        self.journal.append(events)
        return events

    def test_all_lifecycle_types_and_no_execution_authority(self):
        self.emit("queued", "running", "completed", "failed",
                  "needs_review", "interrupted", "stopped")
        self.assertEqual(self.scheduler.tick(), 7)
        rows = self.scheduler.read()
        self.assertEqual([r["decision"] for r in rows],
                         ["ignore", "ignore", "candidate", "candidate", "candidate",
                          "recovery_required", "ignore"])
        self.assertTrue(all(r["mode"] == "observe" and not r["execution_enabled"] for r in rows))
        self.assertEqual(self.scheduler.tick(), 0)

    def test_bounded_batches_restart_and_cursor(self):
        events = self.emit("queued", "completed", "failed")
        self.assertEqual(self.scheduler.tick(limit=2), 2)
        restarted = DecisionScheduler(EventJournal(self.journal.path, "bot-5"))
        self.assertEqual(restarted.tick(limit=1), 1)
        self.assertEqual(restarted.tick(), 0)
        rows = restarted.read()
        self.assertEqual([r["event_id"] for r in rows], [e["event_id"] for e in events])
        self.assertEqual(restarted.read(after=rows[0]["seq"], limit=1), rows[1:2])

    def test_duplicate_delivery_cannot_duplicate_decisions(self):
        events = self.emit("completed")
        self.scheduler.tick()
        self.journal.append(events)
        self.assertEqual(self.scheduler.tick(), 0)
        self.assertEqual(len(self.scheduler.read()), 1)

    def test_failed_batch_rolls_back_both_cursor_and_observations(self):
        self.emit("queued", "completed")
        with sqlite3.connect(self.journal.path) as db:
            db.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON decision_observations
                          WHEN NEW.event_seq=2 BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        # sqlite context manager commits but does not close; close explicitly.
        db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.scheduler.tick()
        self.assertEqual(self.scheduler.read(), [])
        with sqlite3.connect(self.journal.path) as db:
            db.execute("DROP TRIGGER fail_second")
        db.close()
        self.assertEqual(self.scheduler.tick(), 2)
        self.assertEqual(len(self.scheduler.read()), 2)

    def test_independent_profiles(self):
        other = DecisionScheduler(EventJournal(self.root / "other.sqlite3", "bot-6"))
        self.emit("completed")
        self.scheduler.tick()
        self.assertEqual(other.tick(), 0)
        self.assertEqual(other.read(), [])

    def test_invalid_limits(self):
        for limit in (0, -1, True, 1001):
            with self.assertRaises(ValueError):
                self.scheduler.tick(limit=limit)

    def test_host_observation_pauses_without_dropping_events(self):
        self.emit("completed")
        host = Mock()
        host.work.flush_events.return_value = True
        host.decision_scheduler = self.scheduler
        host.control.paused = True
        host._shutdown.is_set.return_value = False
        AgentRelay._observe_work_events(host)
        self.assertEqual(self.scheduler.read(), [])
        host.control.paused = False
        AgentRelay._observe_work_events(host)
        self.assertEqual(len(self.scheduler.read()), 1)
        host.client.send.assert_not_called()
        host.api.call.assert_not_called()

    def test_host_journal_error_is_nonfatal(self):
        host = Mock()
        host.control.paused = False
        host._shutdown.is_set.return_value = False
        host.decision_scheduler.tick.side_effect = sqlite3.OperationalError("busy")
        AgentRelay._observe_work_events(host)
        host.client.send.assert_not_called()
        host.api.call.assert_not_called()
