"""Behavioral tests for structural schema review rules."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "skills/postgres-schema-review/scripts/structure.py"
SPEC = importlib.util.spec_from_file_location("schema_structure", MODULE)
STRUCTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STRUCTURE)


def section(rows):
    return {"status": "ok", "truncated": False, "rows": rows}


def fixture():
    snapshot = {"schema_version": 1, "metadata": {"collected_at": "2026-11-06T01:00:00Z"},
                "sections": {
        "tables": section([
            {"relation": "public.orders", "schema": "public", "name": "orders", "kind": "r"},
            {"relation": "public.users", "schema": "public", "name": "users", "kind": "r"}]),
        "columns": section([]),
        "constraints": section([]), "indexes": section([]), "statistics": section([])}}
    # Keep the column fixture explicit; no application row values are required.
    snapshot["sections"]["columns"]["rows"] = [
        {"relation": "public.orders", "name": "tenant_id", "type": "integer", "not_null": True},
        {"relation": "public.orders", "name": "user_id", "type": "integer", "not_null": True},
        {"relation": "public.orders", "name": "paid_at", "type": "timestamp", "not_null": False},
        {"relation": "public.orders", "name": "is_paid", "type": "boolean", "not_null": True},
        {"relation": "public.users", "name": "tenant_id", "type": "integer", "not_null": True},
        {"relation": "public.users", "name": "id", "type": "integer", "not_null": True}]
    contract = {"schema_version": 1, "tables": [{
        "relation": "public.orders", "source": "design/orders.md:12",
        "relationships": [{
            "columns": ["tenant_id", "user_id"], "referenced_relation": "public.users",
            "referenced_columns": ["tenant_id", "id"], "enforce": True, "allow_null": False}]}]}
    evidence = {"schema_version": 1, "profiles": []}
    return snapshot, contract, evidence


def foreign_key(**changes):
    item = {"relation": "public.orders", "name": "orders_user_fkey", "type": "f",
            "columns": ["tenant_id", "user_id"], "referenced_relation": "public.users",
            "referenced_columns": ["tenant_id", "id"], "enforced": True, "validated": True,
            "match_type": "s", "delete_action": "a", "update_action": "a"}
    item.update(changes)
    return item


def profile(**changes):
    item = {"relation": "public.orders", "kind": "null", "columns": ["paid_at"],
            "violations": 4, "rows_checked": 40, "scope": "all current orders",
            "complete": True, "source": "profile-12", "observed_at": "2026-11-06T01:00:00Z"}
    item.update(changes)
    return item


class StructureTest(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.contract, self.evidence = fixture()
        self.table = self.contract["tables"][0]
        self.expected = self.table["relationships"][0]

    def review(self, rule=None):
        result = STRUCTURE.review(self.snapshot, self.contract, self.evidence)
        return [f for f in result if rule is None or f["rule"] == "schema." + rule]

    def add_fk(self, **changes):
        self.snapshot["sections"]["constraints"]["rows"].append(foreign_key(**changes))

    def col(self, name):
        return next(r for r in self.snapshot["sections"]["columns"]["rows"]
                    if r["relation"] == "public.orders" and r["name"] == name)

    def test_missing_expected_fk_is_confirmed(self):
        findings = self.review("fk")
        self.assertEqual([f["status"] for f in findings], ["confirmed"])
        self.assertIn("tenant_id", findings[0]["evidence"][0])
        self.assertTrue(findings[0]["verification"])

    def test_uncontracted_id_does_not_imply_missing_fk(self):
        self.contract["tables"] = []
        self.assertEqual(self.review(), [])

    def test_missing_fk_without_contract_source_needs_evidence(self):
        self.table.pop("source")
        self.assertEqual(self.review("fk")[0]["status"], "needs_evidence")

    def test_partial_constraint_inventory_cannot_prove_missing_fk(self):
        self.snapshot["sections"]["constraints"]["truncated"] = True
        self.assertEqual(self.review("fk")[0]["status"], "needs_evidence")
        self.snapshot["sections"]["constraints"] = {"status": "unavailable", "reason": "denied"}
        self.assertEqual(self.review("fk")[0]["status"], "needs_evidence")

    def test_valid_fk_has_no_finding(self):
        self.add_fk()
        self.assertEqual(self.review(), [])

    def test_same_permutation_is_equivalent_mapping(self):
        self.add_fk(columns=["user_id", "tenant_id"], referenced_columns=["id", "tenant_id"])
        self.assertEqual(self.review("fk"), [])

    def test_swapping_only_target_is_not_equivalent(self):
        self.add_fk(referenced_columns=["id", "tenant_id"])
        self.assertEqual(self.review("fk")[0]["status"], "confirmed")

    def test_tenant_omission_is_not_covered_by_single_column_fk(self):
        self.add_fk(columns=["user_id"], referenced_columns=["id"])
        self.assertEqual(self.review("fk")[0]["status"], "confirmed")

    def test_unvalidated_fk_is_not_reported_as_absent(self):
        self.add_fk(validated=False)
        result = self.review("fk")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertIn("未検証", " ".join(result[0]["evidence"]))
        self.assertNotIn("見つかりません", result[0]["summary"])
        self.snapshot["sections"]["constraints"]["rows"][0]["validated"] = True
        self.assertEqual(self.review("fk"), [])

    def test_unenforced_fk_remains_finding(self):
        self.add_fk(enforced=False)
        result = self.review("fk")[0]
        self.assertEqual((result["status"], result["severity"]), ("confirmed", "high"))

    def test_unknown_enforcement_is_not_success(self):
        self.add_fk(enforced=None)
        self.assertEqual(self.review("fk")[0]["status"], "needs_evidence")

    def test_match_and_delete_action_follow_explicit_contract(self):
        self.expected.update(match_type="f", delete_action="r", update_action="c")
        self.add_fk(match_type="s", delete_action="c", update_action="a")
        result = self.review("fk")[0]
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len([x for x in result["evidence"] if "expected=" in x]), 3)

    def test_nullable_fk_is_legitimate_when_declared(self):
        self.expected["allow_null"] = True
        self.col("user_id")["not_null"] = False
        self.add_fk()
        self.assertEqual(self.review(), [])

    def test_all_nullable_match_full_does_not_guarantee_required_reference(self):
        self.col("tenant_id")["not_null"] = False
        self.col("user_id")["not_null"] = False
        self.add_fk(match_type="f")
        result = self.review("fk")
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r["status"] == "confirmed" for r in result))
        self.assertIn("MATCH FULL", " ".join(result[0]["verification"]))

    def test_match_full_with_one_not_null_guarantees_all_members_required(self):
        self.col("user_id")["not_null"] = False
        self.table["required_columns"] = ["user_id"]
        self.add_fk(match_type="f")
        self.assertEqual(self.review("fk"), [])
        self.assertEqual(self.review("null"), [])

    def test_all_nullable_match_full_does_not_suppress_required_columns(self):
        self.col("tenant_id")["not_null"] = False
        self.col("user_id")["not_null"] = False
        self.table["required_columns"] = ["user_id"]
        self.add_fk(match_type="f")
        self.assertEqual(self.review("null")[0]["status"], "confirmed")

    def test_unvalidated_match_full_cannot_prove_existing_rows_nonnull(self):
        self.col("user_id")["not_null"] = False
        self.table["required_columns"] = ["user_id"]
        self.add_fk(match_type="f", validated=False)
        self.assertTrue(any("NULL" in f["summary"] for f in self.review("fk")))
        self.assertEqual(self.review("null")[0]["status"], "confirmed")

    def test_unenforced_match_full_does_not_suppress_null_finding(self):
        self.col("user_id")["not_null"] = False
        self.table["required_columns"] = ["user_id"]
        self.add_fk(match_type="f", enforced=False)
        self.assertTrue(any("NULL" in f["summary"] for f in self.review("fk")))
        self.assertEqual(self.review("null")[0]["status"], "confirmed")

    def test_unknown_match_full_validation_does_not_suppress_null_finding(self):
        self.col("user_id")["not_null"] = False
        self.table["required_columns"] = ["user_id"]
        self.add_fk(match_type="f")
        self.snapshot["sections"]["constraints"]["rows"][0].pop("validated")
        self.assertEqual(self.review("null")[0]["status"], "confirmed")

    def test_null_profile_conflicting_with_match_full_requires_reconciliation(self):
        self.col("user_id")["not_null"] = False
        self.add_fk(match_type="f")
        self.evidence["profiles"] = [profile(columns=["user_id"])]
        result = self.review("null")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "needs_evidence")
        self.assertIn("時点", " ".join(result[0]["missing"]))

    def test_equivalent_check_protection_requires_review(self):
        self.col("user_id")["not_null"] = False
        self.add_fk()
        self.snapshot["sections"]["constraints"]["rows"].append({
            "relation": "public.orders", "name": "required_user", "type": "c",
            "columns": ["user_id"], "validated": True, "enforced": True})
        result = self.review("fk")[0]
        self.assertEqual(result["status"], "candidate")

    def test_external_reference_is_documented_exception(self):
        self.expected["enforce"] = False
        result = self.review("fk")[0]
        self.assertEqual(result["status"], "exception")
        self.table.pop("source")
        self.assertEqual(self.review("fk")[0]["status"], "needs_evidence")

    def test_missing_enforcement_intent_stays_unknown(self):
        self.expected.pop("enforce")
        self.assertEqual(self.review("fk")[0]["status"], "needs_evidence")

    def test_explicit_name_contract_and_rename_completion(self):
        self.table.update(expected_name="purchases", column_names={"user_id": "customer_id"})
        results = self.review("naming")
        self.assertEqual([r["status"] for r in results], ["confirmed", "confirmed"])
        self.snapshot["sections"]["tables"]["rows"][0]["name"] = "purchases"
        self.col("user_id")["name"] = "customer_id"
        self.assertEqual(self.review("naming"), [])

    def test_dictionary_similarity_only_candidate_and_abbreviation_exempt(self):
        self.col("paid_at")["name"] = "adress"
        self.contract["vocabulary"] = {"terms": ["address"], "allowed_abbreviations": []}
        self.assertEqual(self.review("naming")[0]["status"], "candidate")
        self.contract["vocabulary"]["allowed_abbreviations"] = ["adress"]
        self.assertEqual(self.review("naming"), [])

    def test_zero_null_statistic_does_not_imply_requiredness(self):
        self.snapshot["sections"]["statistics"]["rows"].append({
            "relation": "public.orders", "column": "paid_at", "null_frac": 0.0})
        self.assertEqual(self.review("null"), [])

    def test_required_null_column_with_check_is_candidate(self):
        self.table["required_columns"] = ["paid_at"]
        self.snapshot["sections"]["constraints"]["rows"].append({
            "relation": "public.orders", "type": "c", "columns": ["paid_at"]})
        self.assertEqual(self.review("null")[0]["status"], "candidate")

    def test_domain_check_requires_equivalent_protection_review_for_required_column(self):
        self.table["required_columns"] = ["paid_at"]
        self.col("paid_at")["domain_has_check"] = True
        result = self.review("null")[0]
        self.assertEqual(result["status"], "candidate")
        self.assertTrue(result["missing"])

    def test_domain_check_requires_equivalent_protection_review_for_required_fk(self):
        self.col("user_id").update(not_null=False, domain_has_check=True)
        self.add_fk()
        result = self.review("fk")[0]
        self.assertEqual(result["status"], "candidate")
        self.assertTrue(result["missing"])

    def test_arbitrary_domain_check_does_not_hide_observed_null_violations(self):
        self.table["required_columns"] = ["paid_at"]
        self.col("paid_at")["domain_has_check"] = True
        self.evidence["profiles"] = [profile()]
        results = self.review("null")
        self.assertEqual(results[0]["status"], "candidate")
        self.assertEqual(results[-1]["status"], "confirmed")

    def test_required_null_column_with_incomplete_constraints_is_candidate(self):
        self.table["required_columns"] = ["paid_at"]
        self.snapshot["sections"]["constraints"]["truncated"] = True
        self.assertEqual(self.review("null")[0]["status"], "candidate")

    def test_required_null_profile_proves_observed_violation_only(self):
        self.table["required_columns"] = ["paid_at"]
        self.evidence["profiles"] = [profile(complete=False)]
        result = self.review("null")
        self.assertTrue(any(r["status"] == "confirmed" and "集計" in r["summary"] for r in result))
        self.assertIn("complete=False", result[-1]["evidence"][0])

    def test_null_profile_without_requiredness_is_unknown(self):
        self.evidence["profiles"] = [profile()]
        result = self.review("null")[0]
        self.assertEqual(result["status"], "needs_evidence")

    def test_profile_provenance_missing_is_unknown(self):
        self.table["required_columns"] = ["paid_at"]
        self.evidence["profiles"] = [profile(observed_at="")]
        self.assertEqual(self.review("null")[-1]["status"], "needs_evidence")

    def test_profile_conflicting_with_current_not_null_needs_reconciliation(self):
        self.table["required_columns"] = ["paid_at"]
        self.col("paid_at")["not_null"] = True
        self.evidence["profiles"] = [profile()]
        result = self.review("null")[0]
        self.assertEqual(result["status"], "needs_evidence")
        self.assertIn("時点", " ".join(result["missing"]))

    def test_independent_boolean_does_not_imply_bad_flag(self):
        self.table["flags"] = [{"columns": ["is_paid"], "meaning": "boolean"}]
        self.assertEqual(self.review("flag"), [])

    def test_event_flag_is_candidate_not_automatic_violation(self):
        self.table["flags"] = [{"columns": ["is_paid"], "meaning": "event", "source_column": "paid_at"}]
        self.assertEqual(self.review("flag")[0]["status"], "candidate")

    def test_flag_profile_needs_declared_invariant(self):
        self.table["flags"] = [{"columns": ["is_paid"], "meaning": "event", "source_column": "paid_at"}]
        self.evidence["profiles"] = [profile(kind="flag_combination", columns=["is_paid", "paid_at"])]
        self.assertEqual(self.review("flag")[-1]["status"], "needs_evidence")
        self.table["flags"][0]["invariant"] = "is_paid iff paid_at exists"
        self.assertEqual(self.review("flag")[-1]["status"], "confirmed")

    def test_flag_profile_needs_every_invariant_source_column(self):
        self.table["flags"] = [{
            "columns": ["is_paid"], "meaning": "event", "source_column": "missing_time",
            "invariant": "is_paid iff missing_time present"}]
        self.evidence["profiles"] = [profile(kind="flag_combination", columns=["is_paid"])]
        results = self.review("flag")
        self.assertTrue(all(r["status"] == "needs_evidence" for r in results))
        self.assertIn("missing_time", " ".join(results[-1]["missing"]))

    def test_flag_profile_needs_all_invariant_columns_even_if_profile_is_subset(self):
        self.table["flags"] = [{
            "columns": ["is_paid", "is_cancelled"], "meaning": "state",
            "invariant": "is_paid and is_cancelled must not both be true"}]
        self.evidence["profiles"] = [profile(kind="flag_combination", columns=["is_paid"])]
        result = self.review("flag")[-1]
        self.assertEqual(result["status"], "needs_evidence")
        self.assertIn("is_cancelled", " ".join(result["missing"]))

    def test_zero_profile_does_not_claim_whole_database_safe(self):
        self.evidence["profiles"] = [profile(violations=0, complete=False)]
        self.assertEqual(self.review("null"), [])

    def test_input_not_mutated(self):
        original = deepcopy((self.snapshot, self.contract, self.evidence))
        self.review()
        self.assertEqual(original, (self.snapshot, self.contract, self.evidence))

    def test_quoted_qualified_identifier_not_split(self):
        relation = '"odd.schema"."orders.name"'
        self.snapshot["sections"]["tables"]["rows"][0]["relation"] = relation
        self.table["relation"] = relation
        for column in self.snapshot["sections"]["columns"]["rows"]:
            if column["relation"] == "public.orders":
                column["relation"] = relation
        result = self.review("fk")[0]
        self.assertEqual(result["relation"], relation)


if __name__ == "__main__":
    unittest.main()
