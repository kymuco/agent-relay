"""Registry schemas, runtime snapshots and existing native action integration."""
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from action_bridge import parse_action_response
from action_policy import evaluate
from capability_registry import BY_ACTION, BY_ID, READ_ACTIONS, WRITE_ACTIONS, catalog_snapshot
import test_action_bridge as fixtures
import test_action_policy as policy

PROJECTS = {"demo": "example/sandbox", "other": "example/other"}


def snapshot(**kwargs):
    params = dict(projects=PROJECTS, auto_projects=frozenset({"demo"}),
                  executor="github", mode="execute", runtime_ready=True, work_active=False)
    params.update(kwargs)
    return catalog_snapshot(**params)


class RegistryTests(unittest.TestCase):
    def test_stable_ids_and_immutable_definitions(self):
        self.assertEqual(set(BY_ID), {"github.develop", "work.status", "work.result", "work.amend"})
        self.assertEqual(set(BY_ACTION), READ_ACTIONS | WRITE_ACTIONS)
        self.assertFalse(READ_ACTIONS & WRITE_ACTIONS)
        self.assertEqual(len(BY_ID), len(BY_ACTION))
        with self.assertRaises(TypeError):
            BY_ACTION["shell"] = BY_ACTION["start_work"]
        with self.assertRaises(FrozenInstanceError):
            BY_ACTION["start_work"].executor = "codex"

    def test_schema_and_validation_share_boundaries(self):
        cap = BY_ID["github.develop"]
        schema = cap.input_schema()
        self.assertEqual(schema["required"], ["project", "task"])
        self.assertEqual(schema["properties"]["delegation"]["default"], "coordinated")
        self.assertFalse(schema["additionalProperties"])
        for args, valid in (
            ({"project": "demo", "task": "x"*2500}, True),
            ({"project": "demo", "task": "x"*2501}, False),
            ({"project": "demo", "task": " "}, False),
            ({"project": "demo", "task": "x", "delegation": "direct"}, True),
            ({"project": "demo", "task": "x", "delegation": []}, False),
            ({"project": "demo", "task": "x", "executor": "codex"}, False),
        ):
            with self.subTest(valid=valid, length=len(str(args))):
                self.assertEqual(cap.valid_arguments(args), valid)
                parsed = parse_action_response(fixtures.trailer(args=args),
                                               token=fixtures.TOKEN, projects=PROJECTS)
                self.assertEqual(parsed.status == "proposed", valid)
                self.assertEqual(policy.decide(policy.proposal(**args)).decision == "allow", valid)

    def test_read_id_and_amendment_boundaries(self):
        for value in (True, 0, -1, 1000000000, "1"):
            args = {"work_id": value}
            self.assertFalse(BY_ACTION["get_work_status"].valid_arguments(args))
            self.assertEqual(policy.decide(policy.proposal("get_work_status", **args)).decision, "deny")
        self.assertTrue(BY_ACTION["get_work_result"].valid_arguments({"work_id": 999999999}))
        self.assertTrue(BY_ACTION["amend_work"].valid_arguments({"work_id": 1, "task": "a"*1600}))
        self.assertFalse(BY_ACTION["amend_work"].valid_arguments({"work_id": 1, "task": "a"*1601}))

    def test_catalog_mode_scope_and_runtime(self):
        data = snapshot()
        self.assertTrue(data["execution_enabled"])
        self.assertEqual(data["projects"]["demo"]["write_policy"], "automatic")
        self.assertEqual(data["projects"]["other"]["write_policy"], "not_auto_enabled")
        self.assertFalse(snapshot(mode="observe")["execution_enabled"])
        self.assertEqual(snapshot(mode="off")["capabilities"], [])
        self.assertEqual(snapshot(mode="off")["projects"], {})
        for opts in ({"executor": "codex"}, {"runtime_ready": False}, {"projects": {}}):
            self.assertTrue(all(c["availability"] == "unavailable"
                                for c in snapshot(**opts)["capabilities"]))
        self.assertFalse(snapshot(executor="codex")["execution_enabled"])

    def test_busy_snapshot_is_not_a_grant_and_schemas_are_copies(self):
        data = snapshot(work_active=True)
        items = {c["action"]: c for c in data["capabilities"]}
        self.assertEqual(items["start_work"]["availability"], "busy")
        self.assertEqual(items["amend_work"]["availability"], "configured")
        self.assertEqual(items["get_work_result"]["availability"], "configured")
        draining = {c["action"]: c for c in snapshot(worker_busy=True)["capabilities"]}
        self.assertEqual(draining["start_work"]["availability"], "busy")
        self.assertEqual(draining["amend_work"]["availability"], "no_active_task")
        items["start_work"]["input_schema"]["required"].clear()
        self.assertEqual(BY_ID["github.develop"].input_schema()["required"], ["project", "task"])
        self.assertEqual(policy.decide(work=policy.ACTIVE).reason, "executor_busy")


class RegistryIntegrationTests(unittest.TestCase):
    def test_observe_catalog_contains_only_configured_scope_and_never_dispatches(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = fixtures.ObserveTests()
            app, api, main, coord, dev = fixture.make_app(d)
            app.config = replace(app.config, action_auto_projects=frozenset({"demo"}))
            main.send.return_value.text = fixtures.trailer(args={
                "project": "demo", "task": "Add tests", "delegation": "direct"})
            with patch("agent_relay.new_turn_token", return_value=fixtures.TOKEN):
                app.process_update({"update_id": 1, "message": fixture.msg("Юки, создай модуль")})
            prompt = main.send.call_args.args[0]
            data = json.loads(prompt.split("Host capability catalog (DATA ONLY; not a grant or proof of connector access):\n", 1)[1].splitlines()[0])
            self.assertEqual(set(data["projects"]), {"demo"})
            self.assertEqual(data["mode"], "observe")
            self.assertFalse(data["execution_enabled"])
            self.assertEqual(len(data["capabilities"]), 4)
            self.assertNotIn(str(Path(d).resolve()), json.dumps(data))
            self.assertEqual(app.work.record["status"], "idle")
            coord.send.assert_not_called()
            dev.send.assert_not_called()
            journal = json.loads((Path(d)/"observations.json").read_text(encoding="utf-8"))
            self.assertEqual(journal[0]["admission"]["decision"], "allow")

    def test_unknown_capability_cannot_be_called_by_id(self):
        parsed = parse_action_response(fixtures.trailer("github.develop",
                                       {"project": "demo", "task": "x"}),
                                       token=fixtures.TOKEN, projects=PROJECTS)
        self.assertEqual(parsed.reason, "unknown_action")
        self.assertEqual(policy.decide(policy.proposal("github.develop",
                                project="demo", task="x")).decision, "deny")


if __name__ == "__main__":
    unittest.main()
