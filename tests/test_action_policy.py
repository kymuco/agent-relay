"""NA-02 policy and dry-run integration. No live writes."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from action_bridge import Observation, ObservationJournal
from action_policy import Source, evaluate, request_digest
import test_action_bridge as fixtures

PROJECTS = {"demo": "example/sandbox", "other": "example/other"}
SOURCE = Source(5, -123, 1, 41, 10, 0, request_digest("do it"))
IDLE = {"id": 0, "status": "idle", "project": ""}
ACTIVE = {"id": 7, "status": "running", "project": "demo"}
CARD = {"work_id": 7, "project": "demo", "phase": "developer"}


def proposal(action="start_work", **args):
    return Observation("", "proposed", True, action,
                       args or {"project": "demo", "task": "Add tests"})


def decide(p=None, **overrides):
    params = dict(source=SOURCE, authorized=True, generation_current=True,
                  projects=PROJECTS, auto_projects=frozenset({"demo"}),
                  executor="github", work=IDLE, bound_card=None)
    params.update(overrides)
    return evaluate(p or proposal(), **params)


class PolicyTests(unittest.TestCase):
    def test_automatic_only_explicit_project(self):
        self.assertEqual(decide().decision, "allow")
        self.assertEqual(decide(auto_projects=frozenset()).decision, "ask")
        self.assertEqual(decide(proposal(project="other", task="Add tests")).decision, "ask")
        self.assertEqual(decide(proposal(project="unknown", task="Add tests")).decision, "deny")
        self.assertFalse(decide().to_record()["execution_enabled"])

    def test_identity_generation_and_executor_fences(self):
        for kwargs, reason in (
            ({"authorized": False}, "unauthorized_source"),
            ({"generation_current": False}, "stale_generation"),
            ({"executor": "codex"}, "executor_unavailable"),
            ({"work": None}, "executor_unavailable"),
            ({"work": ACTIVE}, "executor_busy"),
            ({"bound_card": CARD}, "start_from_task_reply"),
        ):
            with self.subTest(reason=reason):
                result = decide(**kwargs)
                self.assertEqual((result.decision, result.reason), ("deny", reason))

    def test_unrecognized_and_malformed_never_allowed(self):
        for p in (
            proposal("merge", project="demo"),
            proposal("shell", command="test"),
            proposal(task=""),
            proposal("amend_work", work_id=True, task="test"),
            Observation("", "rejected", True),
        ):
            with self.subTest(p=p):
                self.assertEqual(decide(p).decision, "deny")

    def test_route_is_validated_and_bound_to_proposal(self):
        direct = decide(proposal(project="demo", task="Add tests", delegation="direct"))
        self.assertEqual(direct.decision, "allow")
        self.assertEqual(direct.delegation, "direct")
        self.assertEqual(direct.to_record()["delegation"], "direct")
        self.assertEqual(decide().delegation, "coordinated")
        self.assertNotEqual(direct.proposal_id, decide().proposal_id)
        self.assertEqual(decide(proposal(project="demo", task="Add tests", delegation="bad")).decision, "deny")
        self.assertEqual(decide(proposal(project="demo", task="Add tests", delegation="direct"),
                                auto_projects=frozenset()).decision, "ask")

    def test_read_exact_task_only(self):
        p = proposal("get_work_status", work_id=7)
        self.assertEqual(decide(p, work=ACTIVE).decision, "allow")
        self.assertEqual(decide(p, work=IDLE, bound_card=CARD).decision, "allow")
        self.assertEqual(decide(p).reason, "unknown_task")
        self.assertEqual(decide(p, work=ACTIVE, bound_card={**CARD, "work_id": 6}).reason,
                         "task_reply_mismatch")
        result = proposal("get_work_result", work_id=7)
        self.assertEqual(decide(result, work=ACTIVE).reason, "result_not_ready")
        self.assertEqual(decide(result, work={**ACTIVE, "status": "completed"}).decision, "allow")

    def test_amendment_bound_to_same_active_task(self):
        p = proposal("amend_work", work_id=7, task="Edge case")
        self.assertEqual(decide(p, work=ACTIVE, bound_card=CARD).decision, "allow")
        self.assertEqual(decide(p, work=ACTIVE).reason, "exact_task_reply_required")
        self.assertEqual(decide(p, work={**ACTIVE, "id": 8}, bound_card=CARD).reason,
                         "task_not_active")
        self.assertEqual(decide(p, work={**ACTIVE, "status": "completed"}, bound_card=CARD).reason,
                         "task_not_active")
        self.assertEqual(decide(p, work=ACTIVE, bound_card={**CARD, "project": "other"}).reason,
                         "task_not_active")

    def test_digest_is_bound_to_source_arguments_scope_generation(self):
        first = decide()
        self.assertEqual(first.proposal_id, decide().proposal_id)
        variants = [
            decide(source=replace(SOURCE, generation=1)),
            decide(source=replace(SOURCE, user_id=11)),
            decide(source=replace(SOURCE, update_id=2)),
            decide(source=replace(SOURCE, request_sha256=request_digest("different"))),
            decide(proposal(task="Different task", project="demo")),
            decide(projects={"demo": "example/changed"}),
        ]
        self.assertTrue(all(x.proposal_id != first.proposal_id for x in variants))
        self.assertEqual(len({x.proposal_id for x in variants}), len(variants))

    def test_journal_decision_not_a_replay_grant(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "observations.json"
            j = ObservationJournal(str(path))
            params = dict(bot_id=5, chat_id=-123, update_id=1, message_id=41,
                          user_id=10, observation=proposal(), admission=decide().to_record(),
                          generation=0, request_sha256=SOURCE.request_sha256)
            self.assertTrue(j.record(**params))
            self.assertFalse(ObservationJournal(str(path)).record(**params))
            record = json.loads(path.read_text(encoding="utf-8"))[0]
            self.assertFalse(record["admission"]["execution_enabled"])
            self.assertEqual(record["admission"]["proposal_id"], decide().proposal_id)
            self.assertNotIn("request", record)


class AdmissionIntegrationTests(unittest.TestCase):
    def test_policy_allow_and_ask_never_start_work_or_send_confirmation_button(self):
        for auto in (frozenset(), frozenset({"demo"})):
            with self.subTest(auto=auto), tempfile.TemporaryDirectory() as d:
                fixture = fixtures.ObserveTests()
                app, api, main, coord, dev = fixture.make_app(d)
                app.config = replace(app.config, action_auto_projects=auto)
                main.send.return_value.text = fixtures.trailer()
                update = {"update_id": 1, "message": fixture.msg("Юки, создай модуль в demo")}
                with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN):
                    app.process_update(update)
                    app.process_update(update)
                main.send.assert_called_once()
                coord.send.assert_not_called()
                dev.send.assert_not_called()
                self.assertEqual(app.work.admission_snapshot()["status"], "idle")
                records = json.loads((Path(d)/"observations.json").read_text(encoding="utf-8"))
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["admission"]["decision"], "allow" if auto else "ask")
                self.assertFalse(records[0]["admission"]["execution_enabled"])
                sent = [x.args[1] for x in api.call.call_args_list if x.args[0] == "sendMessage"]
                self.assertEqual(len(sent), 1)
                self.assertNotIn("reply_markup", sent[0])
                self.assertIn("не выполнено", sent[0]["text"])

    def test_exact_reply_cannot_switch_to_another_task(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = fixtures.ObserveTests()
            app, api, main, coord, dev = fixture.make_app(d)
            app.config = replace(app.config, action_auto_projects=frozenset({"demo"}))
            app.work.start("demo")
            app.progress.begin(1, "demo", "Test", reply_message_id=41)
            msg = fixture.msg("Добавь тест")
            msg["reply_to_message"] = {"message_id": 301, "from": {"id": 5, "is_bot": True}}
            main.send.return_value.text = fixtures.trailer("amend_work", {"work_id": 2, "task": "Test"})
            with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN):
                app.process_update({"update_id": 1, "message": msg})
            record = json.loads((Path(d)/"observations.json").read_text(encoding="utf-8"))[0]
            self.assertEqual(record["admission"]["reason"], "task_reply_mismatch")
            self.assertEqual(app.work.pending_snapshot(), [])
            dev.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
