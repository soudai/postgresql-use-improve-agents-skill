import copy
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/postgres-schema-review/scripts/lifecycle.py"
SPEC = importlib.util.spec_from_file_location("schema_lifecycle", SCRIPT)
LIFECYCLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LIFECYCLE)

RELATION = 'public.orders'
TIME = "2026-11-06T09:00:00Z"


def snapshot(relation=RELATION):
    return {
        "schema_version": 1,
        "sections": {
            "tables": {"status": "ok", "truncated": False, "rows": [
                {"relation": relation, "schema": "public", "name": "orders", "kind": "r"}]},
            "columns": {"status": "ok", "truncated": False, "rows": [
                {"relation": relation, "name": name}
                for name in ("id", "paid_at", "shipped_at", "is_paid")]},
        },
    }


def contract(relation=RELATION):
    return {
        "schema_version": 1,
        "tables": [{
            "relation": relation, "row_meaning": "one order", "kind": "entity",
            "source": "design/orders.md:20",
            "column_groups": [
                {"name": "payment", "columns": ["paid_at"], "lifecycle": "payment",
                 "writers": ["billing"], "created_by": "accepted payment",
                 "finalized_by": "payment confirmed", "retention": "7 years"},
                {"name": "shipment", "columns": ["shipped_at"], "lifecycle": "shipment",
                 "writers": ["warehouse"], "created_by": "dispatch",
                 "finalized_by": "delivered", "retention": "1 year"},
            ],
        }],
    }


def writes(relation=RELATION):
    return [
        {"relation": relation, "operation": "UPDATE", "columns": ["paid_at"],
         "business_operation": "payment", "source": "trace/payment:1", "observed_at": TIME},
        {"relation": relation, "operation": "UPDATE", "columns": ["shipped_at"],
         "business_operation": "shipment", "source": "trace/shipment:1", "observed_at": TIME},
    ]


def evidence(relation=RELATION):
    return {
        "schema_version": 1, "writes": writes(relation),
        "contention": [{"relation": relation, "operations": ["payment", "shipment"],
                        "same_key": True, "wait_ms": 125.5, "blocker_chain": True,
                        "transaction_boundary": True, "source": "trace/lock:2",
                        "observed_at": TIME}],
    }


def select(findings, rule):
    return [finding for finding in findings if finding["rule"] == "schema." + rule]


class LifecycleReviewTests(unittest.TestCase):
    def test_independent_lifecycles_and_responsibilities_are_candidates(self):
        found = LIFECYCLE.review(snapshot(), contract(), {})
        self.assertEqual({f["rule"] for f in found},
                         {"schema.lifecycle", "schema.responsibility"})
        self.assertTrue(all(f["status"] == "candidate" for f in found))
        self.assertEqual({f["group"] for f in found}, {RELATION + ":lifecycle"})
        self.assertTrue(all(f["recommendation"] and f["verification"] for f in found))

    def test_groups_with_same_lifecycle_and_writer_are_normal(self):
        document = contract()
        first, second = document["tables"][0]["column_groups"]
        for field in ("lifecycle", "writers", "created_by", "finalized_by", "retention"):
            second[field] = copy.deepcopy(first[field])
        self.assertEqual(LIFECYCLE.review(snapshot(), document, {}), [])

    def test_many_columns_or_timestamps_alone_do_not_trigger(self):
        snap = snapshot()
        snap["sections"]["columns"]["rows"] += [
            {"relation": RELATION, "name": "extra_" + str(i), "type": "timestamp"}
            for i in range(100)]
        document = {"tables": [{"relation": RELATION, "kind": "entity", "source": "design"}]}
        self.assertEqual(LIFECYCLE.review(snap, document, {}), [])

    def test_missing_contract_source_preserves_candidate_as_unknown(self):
        document = contract()
        document["tables"][0].pop("source")
        found = LIFECYCLE.review(snapshot(), document, {})
        self.assertTrue(all(f["status"] == "needs_evidence" and f["missing"] for f in found))

    def test_unknown_group_columns_require_reconciliation(self):
        document = contract()
        document["tables"][0]["column_groups"][0]["columns"] = ["payment_time"]
        found = LIFECYCLE.review(snapshot(), document, {})
        self.assertTrue(all(f["status"] == "needs_evidence" for f in found))
        self.assertTrue(any("payment_time" in item for f in found for item in f["missing"]))

    def test_overlapping_identical_groups_do_not_count_as_independent(self):
        document = contract()
        document["tables"][0]["column_groups"][1]["columns"] = ["paid_at"]
        self.assertEqual(LIFECYCLE.review(snapshot(), document, {}), [])

    def test_projection_and_cache_can_intentionally_combine_lifecycles(self):
        for kind in ("projection", "cache"):
            with self.subTest(kind=kind):
                document = contract()
                document["tables"][0]["kind"] = kind
                self.assertEqual(LIFECYCLE.review(snapshot(), document, {}), [])

    def test_projection_without_declared_purpose_is_still_candidate(self):
        document = contract()
        document["tables"][0]["kind"] = "projection"
        document["tables"][0].pop("row_meaning")
        self.assertEqual(select(LIFECYCLE.review(snapshot(), document, {}), "lifecycle")[0]["status"],
                         "candidate")

    def test_draft_without_immutability_contract_allows_updates(self):
        document = {"tables": [{"relation": RELATION, "kind": "draft", "source": "design"}]}
        self.assertEqual(LIFECYCLE.review(snapshot(), document, {"writes": writes()}), [])

    def test_immutable_update_requires_declared_boundary_and_execution(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        observed = {"writes": [dict(writes()[0], after_finalization=True)]}
        found = select(LIFECYCLE.review(snapshot(), document, observed), "immutable")
        self.assertEqual(found[0]["status"], "confirmed")
        self.assertEqual(found[0]["severity"], "high")
        self.assertEqual(found[0]["confidence"], "high")
        self.assertIn("trace/payment:1", "\n".join(found[0]["evidence"]))

    def test_immutable_delete_is_detected(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        observed = {"writes": [dict(writes()[0], operation="DELETE", columns=[],
                                   after_finalization=True)]}
        found = select(LIFECYCLE.review(snapshot(), document, observed), "immutable")
        self.assertEqual(found[0]["status"], "confirmed")

    def test_immutable_truncate_is_detected(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        observed = {"writes": [dict(writes()[0], operation="TRUNCATE", columns=[],
                                   after_finalization=True)]}
        found = select(LIFECYCLE.review(snapshot(), document, observed), "immutable")
        self.assertEqual(found[0]["status"], "confirmed")

    def test_prefinalization_updates_are_valid(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        observed = {"writes": [dict(writes()[0], after_finalization=False)]}
        self.assertEqual(select(LIFECYCLE.review(snapshot(), document, observed), "immutable"), [])

    def test_unproven_prefinalization_scope_remains_unknown(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        observed = dict(writes()[0], after_finalization=False)
        observed.pop("source")
        found = select(LIFECYCLE.review(snapshot(), document, {"writes": [observed]}), "immutable")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_unknown_finalization_is_not_confirmation(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        found = select(LIFECYCLE.review(snapshot(), document, {"writes": writes()}), "immutable")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_missing_write_source_or_time_is_not_confirmation(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        for missing in ("source", "observed_at"):
            observed = dict(writes()[0], after_finalization=True)
            observed.pop(missing)
            found = select(LIFECYCLE.review(snapshot(), document, {"writes": [observed]}), "immutable")
            self.assertEqual(found[0]["status"], "needs_evidence")

    def test_missing_contract_source_does_not_confirm_immutable(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        document["tables"][0].pop("source")
        observed = {"writes": [dict(writes()[0], after_finalization=True)]}
        found = select(LIFECYCLE.review(snapshot(), document, observed), "immutable")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_missing_write_observation_is_unknown_not_healthy(self):
        document = contract()
        document["tables"][0]["immutable_after"] = "confirmed"
        found = select(LIFECYCLE.review(snapshot(), document, {}), "immutable")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_event_update_without_immutable_boundary_requests_design(self):
        document = {"tables": [{"relation": RELATION, "kind": "event", "source": "design"}]}
        found = select(LIFECYCLE.review(snapshot(), document, {"writes": writes()}), "immutable")
        self.assertEqual(found[0]["status"], "needs_evidence")
        self.assertTrue(found[0]["missing"])

    def test_event_insert_is_not_a_violation(self):
        document = {"tables": [{"relation": RELATION, "kind": "event", "source": "design"}]}
        observed = {"writes": [dict(writes()[0], operation="INSERT")]}
        self.assertEqual(LIFECYCLE.review(snapshot(), document, observed), [])

    def test_row_contention_with_independent_writes_is_confirmed(self):
        found = select(LIFECYCLE.review(snapshot(), contract(), evidence()), "contention")
        self.assertEqual(found[0]["status"], "confirmed")
        self.assertEqual(found[0]["severity"], "high")
        self.assertTrue(any("p95/p99" in item for item in found[0]["verification"]))

    def test_partial_trace_never_confirms_god_table(self):
        for missing in ("same_key", "blocker_chain", "transaction_boundary", "source", "observed_at"):
            with self.subTest(missing=missing):
                observed = evidence()
                observed["contention"][0].pop(missing)
                found = select(LIFECYCLE.review(snapshot(), contract(), observed), "contention")
                self.assertEqual(found[0]["status"], "needs_evidence")
                self.assertTrue(found[0]["missing"])

    def test_wait_without_write_mapping_is_unknown(self):
        observed = evidence()
        observed.pop("writes")
        found = select(LIFECYCLE.review(snapshot(), contract(), observed), "contention")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_same_orm_full_row_writes_do_not_prove_independent_responsibilities(self):
        observed = evidence()
        for write in observed["writes"]:
            write["columns"] = ["paid_at", "shipped_at"]
        found = select(LIFECYCLE.review(snapshot(), contract(), observed), "contention")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_unknown_write_columns_do_not_supply_independence(self):
        observed = evidence()
        observed["writes"][0]["columns"] = ["paid_at", "not_in_catalog"]
        observed["writes"][1]["columns"] = ["paid_at", "shipped_at"]
        found = select(LIFECYCLE.review(snapshot(), contract(), observed), "contention")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_single_operation_hot_row_is_not_god_table(self):
        observed = evidence()
        observed["contention"][0]["operations"] = ["inventory"]
        self.assertEqual(select(LIFECYCLE.review(snapshot(), contract(), observed), "contention"), [])

    def test_different_keys_or_no_wait_are_not_same_row_contention(self):
        for change in ({"same_key": False}, {"wait_ms": 0}):
            observed = evidence()
            observed["contention"][0].update(change)
            self.assertEqual(select(LIFECYCLE.review(snapshot(), contract(), observed), "contention"), [])

    def test_missing_wait_duration_requires_evidence(self):
        observed = evidence()
        observed["contention"][0].pop("wait_ms")
        found = select(LIFECYCLE.review(snapshot(), contract(), observed), "contention")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_undeclared_relation_with_wait_is_not_confirmed(self):
        found = select(LIFECYCLE.review(snapshot(), {}, evidence()), "contention")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_projection_can_still_have_actual_contention(self):
        document = contract()
        document["tables"][0]["kind"] = "projection"
        found = LIFECYCLE.review(snapshot(), document, evidence())
        self.assertFalse(select(found, "lifecycle"))
        self.assertEqual(select(found, "contention")[0]["status"], "confirmed")

    def test_missing_catalog_table_prevents_confirmation(self):
        snap = snapshot()
        snap["sections"]["tables"] = {"status": "error", "rows": []}
        found = select(LIFECYCLE.review(snap, contract(), evidence()), "contention")
        self.assertEqual(found[0]["status"], "needs_evidence")

    def test_truncated_catalog_keeps_positive_evidence_but_not_absence(self):
        snap = snapshot()
        snap["sections"]["tables"]["truncated"] = True
        snap["sections"]["columns"]["truncated"] = True
        self.assertEqual(select(LIFECYCLE.review(snap, contract(), evidence()), "contention")[0]["status"],
                         "confirmed")
        snap["sections"]["columns"]["rows"] = []
        self.assertEqual(select(LIFECYCLE.review(snap, contract(), evidence()), "contention")[0]["status"],
                         "needs_evidence")

    def test_quoted_relation_is_an_opaque_token(self):
        relation = '"sales.v2"."order.entries"'
        found = LIFECYCLE.review(snapshot(relation), contract(relation), evidence(relation))
        self.assertTrue(all(f["relation"] == relation for f in found))
        self.assertEqual(select(found, "contention")[0]["status"], "confirmed")

    def test_inputs_are_not_mutated(self):
        args = snapshot(), contract(), evidence()
        original = copy.deepcopy(args)
        LIFECYCLE.review(*args)
        self.assertEqual(args, original)


if __name__ == "__main__":
    unittest.main()
