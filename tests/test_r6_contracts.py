"""R6 contract conformance (server side).

The browser side runs the same fixtures through ``src/shared/contracts.js`` in
``tests/r6/contracts.test.cjs``. Both sides assert the fixture's own verdict, so
a divergence shows up as a failure on one side rather than a silent drift.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import r6_contract_validate as C  # noqa: E402


class ContractCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = C.load_schema()
        cls.machine = C.load_state_machine()
        cls.cases = C.load_json(C.CASES_PATH)["cases"]
        cls.projection = C.load_json(C.PROJECTION_PATH)["cases"]

    def test_fixture_verdicts_hold(self):
        mismatches = []
        for case in self.cases:
            errors = C.validate(case["doc"], case["def"], self.schema)
            if (not errors) != case["valid"]:
                mismatches.append("%s: %s" % (case["name"], errors[:2]))
        self.assertEqual(mismatches, [])

    def test_valid_documents_are_silent(self):
        for case in self.cases:
            if case["valid"]:
                self.assertEqual(C.validate(case["doc"], case["def"], self.schema), [], case["name"])

    def test_invalid_documents_fail_for_the_documented_reason(self):
        expected = {
            "record-header-missing-provisional": "provisional",
            "record-header-unsafe-integer-offset": "expected string",
            "record-header-null-bytes-but-final": "C10",
            "record-header-impossible-timestamp": "C13",
            "record-header-value-type-lies": "C7",
            "record-header-reversed-bytes": "C1",
            "finding-lowercase-severity": "enum",
            "query-spec-limit-too-large": "maximum",
            "query-state-partial-but-complete": "C5",
            "query-state-paused-without-token": "C5",
            "query-state-unknown-with-number": "C3",
            "query-state-returned-exceeds-matched": "C3",
            "task-uploaded-but-claims-indexed": "C6",
            "task-bytes-over-total": "C6",
            "provider-unknown-kind": "enum",
            "envelope-old-protocol": "const",
        }
        by_name = {case["name"]: case for case in self.cases}
        for name, needle in expected.items():
            self.assertIn(name, by_name)
            errors = C.validate(by_name[name]["doc"], by_name[name]["def"], self.schema)
            self.assertTrue(errors, name)
            self.assertTrue(any(needle in error for error in errors),
                            "%s failed for another reason: %s" % (name, errors))

    def test_decimal_offsets_keep_full_precision(self):
        self.assertEqual(C.compare_decimal("12345678901234567890", "12345678901234567891"), -1)
        self.assertEqual(C.compare_decimal("9007199254740993", "9007199254740992"), 1)
        self.assertEqual(C.compare_decimal("-1", "0"), -1)
        self.assertEqual(C.compare_decimal("0", "0"), 0)

    def test_v1_projection_matches_declared_expectation(self):
        for case in self.projection:
            ok, value = C.project_v1(case["input"])
            self.assertEqual(ok, case["valid"], "%s: %s" % (case["name"], value))
            if ok:
                self.assertEqual(value, case["expect"], case["name"])

    def test_projected_headers_are_valid_v2_documents(self):
        for case in self.projection:
            if not case["valid"]:
                continue
            ok, value = C.project_v1(case["input"])
            self.assertTrue(ok)
            self.assertEqual(C.validate(value, "RecordHeader", self.schema), [], case["name"])

    def test_bigint_offsets_survive_a_json_round_trip(self):
        case = next(c for c in self.cases if c["name"] == "record-header-typed-values")
        doc = dict(case["doc"])
        doc["ref"] = dict(doc["ref"])
        doc["ref"]["bytes"] = {"start": "9007199254740993", "end": "9007199254740994"}
        again = C.json.loads(C.json.dumps(doc))
        self.assertEqual(C.validate(again, "RecordHeader", self.schema), [])
        self.assertEqual(again["ref"]["bytes"]["start"], "9007199254740993")


class StateMachine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.machine = C.load_state_machine()

    def test_query_edges(self):
        self.assertTrue(C.can_transition(self.machine, "query", "queued", "running"))
        self.assertFalse(C.can_transition(self.machine, "query", "done", "running"))
        self.assertTrue(C.can_transition(self.machine, "query", "partial", "running"))
        self.assertFalse(C.can_transition(self.machine, "query", "paused", "done"))
        self.assertFalse(C.can_transition(self.machine, "query", "running", "totally-done"))

    def test_task_edges_refuse_completion_shortcuts(self):
        self.assertTrue(C.can_transition(self.machine, "task", "uploading", "uploaded"))
        self.assertFalse(C.can_transition(self.machine, "task", "uploading", "indexed"))
        self.assertTrue(C.can_transition(self.machine, "task", "uploaded", "indexing"))
        self.assertFalse(C.can_transition(self.machine, "task", "cancelled", "indexing"))

    def test_transition_table_covers_every_state_once(self):
        for kind in ("query", "task"):
            group = self.machine[kind]
            self.assertEqual(sorted(group["transitions"]), sorted(group["states"]), kind)
            for state in group["terminal"]:
                self.assertEqual(group["transitions"][state], [], "%s.%s" % (kind, state))

    def test_every_edge_target_is_a_known_state(self):
        for kind in ("query", "task"):
            group = self.machine[kind]
            for source, targets in group["transitions"].items():
                self.assertIn(source, group["states"])
                for target in targets:
                    self.assertIn(target, group["states"], "%s: %s -> %s" % (kind, source, target))


class SchemaOwnership(unittest.TestCase):
    """Downstream entries must be able to name a single schema owner."""

    OWNERS = {
        "RecordRef": "RecordRef",
        "RecordHeader": "RecordHeader",
        "Finding": "Finding",
        "TaskProgress": "TaskProgress",
        "QuerySpec": "QuerySpec",
        "QueryState": "QueryState",
        "ReaderProvider": "ReaderProvider",
        "WorkerEnvelope": "WorkerEnvelope",
    }

    def test_required_definitions_exist(self):
        schema = C.load_schema()
        for name in self.OWNERS:
            self.assertIn(name, schema["$defs"], name)

    def test_only_declared_entry_points_are_unreferenced(self):
        """Anything unreachable must still be a named public entry point."""
        schema = C.load_schema()
        blob = C.json.dumps(schema["$defs"])
        referenced = {name for name in schema["$defs"] if "#/$defs/%s" % name in blob}
        roots = set(schema["$defs"]) - referenced
        self.assertTrue(roots.issubset(set(self.OWNERS)),
                        "orphan definitions: %s" % sorted(roots - set(self.OWNERS)))
        # RecordRef is deliberately imported by RecordHeader and Finding.
        self.assertNotIn("RecordRef", roots)

    def test_contract_version_is_pinned(self):
        schema = C.load_schema()
        self.assertEqual(schema["contract_version"], "logbench.r6/1")

    def test_v2_protocol_bumped_from_r5(self):
        schema = C.load_schema()
        self.assertEqual(schema["$defs"]["WorkerEnvelope"]["properties"]["protocol"]["const"], "logbench.worker/2")


if __name__ == "__main__":
    unittest.main()
