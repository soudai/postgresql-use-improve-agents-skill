"""Regression cases from PostgreSQL behavior, not inferred guarantees."""
import importlib.util
from pathlib import Path
import unittest

SCRIPTS=Path(__file__).resolve().parents[1]/"skills/postgres-schema-review/scripts"
def load(name):
    spec=importlib.util.spec_from_file_location("schema_guarantees_"+name,SCRIPTS/(name+".py"))
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
REVIEW=load("review")

def fixture():
    relation="public.required"
    snapshot={"schema_version":2,"metadata":{},"sections":{
        k:{"status":"ok","truncated":False,"rows":[]} for k in REVIEW.SECTIONS}}
    snapshot["sections"]["tables"]["rows"]=[{"relation":relation,"schema":"public","name":"required","kind":"r","has_inheritance_children":False}]
    snapshot["sections"]["columns"]["rows"]=[{"relation":relation,"name":"value","type":"integer",
        "column_not_null":False,"domain_not_null":False,"domain_has_check":False}]
    contract={"schema_version":1,"tables":[{"relation":relation,"source":"ADR:required-value","required_columns":["value"]}]}
    evidence={"schema_version":1}
    return snapshot,contract,evidence

def null_profile():
    return {"relation":"public.required","kind":"null","columns":["value"],"violations":1,
            "rows_checked":1,"scope":"all current rows","complete":True,
            "source":"sql-profile/current","observed_at":"2026-11-06T01:00:00Z"}

def nn(**changes):
    item={"relation":"public.required","name":"value_nn","type":"n","columns":["value"],
          "validated":True,"enforced":True}
    item.update(changes);return item

class NullGuarantees(unittest.TestCase):
    def setUp(self):
        self.snapshot,self.contract,self.evidence=fixture()
        self.column=self.snapshot["sections"]["columns"]["rows"][0]
    def findings(self):
        return REVIEW.review(self.snapshot,self.contract,self.evidence)["findings"]
    def test_domain_not_null_does_not_prove_column_guarantee(self):
        self.column["domain_not_null"]=True
        self.assertTrue(any(f["rule"]=="schema.null" for f in self.findings()))
    def test_domain_does_not_discredit_observed_null(self):
        self.column["domain_not_null"]=True
        self.evidence["profiles"]=[null_profile()]
        self.assertTrue(any(f["status"]=="confirmed" and "集計" in f["summary"] for f in self.findings()))
    def test_not_valid_column_reports_unvalidated_existing_rows(self):
        self.column["column_not_null"]=True
        self.snapshot["sections"]["constraints"]["rows"]=[nn(validated=False)]
        found=self.findings()
        self.assertTrue(any(f["status"]=="confirmed" and "未検証" in " ".join(f["evidence"]) for f in found))
    def test_not_valid_does_not_discredit_existing_null(self):
        self.column["column_not_null"]=True
        self.snapshot["sections"]["constraints"]["rows"]=[nn(validated=False)]
        self.evidence["profiles"]=[null_profile()]
        self.assertTrue(any(f["status"]=="confirmed" and "集計" in f["summary"] for f in self.findings()))
    def test_validated_column_disagrees_with_null_profile(self):
        self.column["column_not_null"]=True
        self.snapshot["sections"]["constraints"]["rows"]=[nn()]
        self.assertFalse(self.findings())
        self.evidence["profiles"]=[null_profile()]
        found=self.findings()
        self.assertTrue(any(f["status"]=="needs_evidence" for f in found))
    def test_missing_validation_is_unknown(self):
        self.column["column_not_null"]=True
        self.snapshot["sections"]["constraints"]["rows"]=[nn()]
        del self.snapshot["sections"]["constraints"]["rows"][0]["validated"]
        self.assertTrue(any(f["status"]=="needs_evidence" for f in self.findings()))
    def test_v1_true_never_becomes_column_guarantee(self):
        self.snapshot["schema_version"]=1
        self.column.clear()
        self.column.update(relation="public.required",name="value",type="integer",not_null=True)
        self.snapshot["sections"]["constraints"]["rows"]=[nn()]
        result=REVIEW.review(self.snapshot,self.contract,self.evidence)
        self.assertTrue(any(f["status"]=="needs_evidence" for f in result["findings"]))
        self.assertTrue(any("v1" in s and "再収集" in s for s in result["limitations"]))
    def test_v1_false_remains_usable(self):
        self.snapshot["schema_version"]=1
        self.column.clear()
        self.column.update(relation="public.required",name="value",type="integer",not_null=False)
        self.assertTrue(any(f["status"]=="confirmed" for f in self.findings()))
    def test_external_fk_exception_keeps_requiredness(self):
        self.contract["tables"][0].pop("required_columns")
        self.contract["tables"][0]["relationships"]=[{"columns":["value"],"enforce":False,"allow_null":False}]
        found=self.findings()
        self.assertTrue(any(f["rule"]=="schema.fk" and f["status"]=="exception" for f in found))
        self.assertTrue(any(f["rule"]=="schema.null" and f["status"]=="confirmed" for f in found))
        self.column["column_not_null"]=True
        self.snapshot["sections"]["constraints"]["rows"]=[nn()]
        self.assertFalse([f for f in self.findings() if f["rule"]=="schema.null"])
    def test_multicolumn_null_profile_is_not_discredited_by_one_protected_column(self):
        self.column["column_not_null"] = True
        self.snapshot["sections"]["constraints"]["rows"] = [nn()]
        self.snapshot["sections"]["columns"]["rows"].append({
            "relation": "public.required", "name": "other", "type": "integer",
            "column_not_null": False, "domain_not_null": False})
        self.contract["tables"][0]["required_columns"] = ["value", "other"]
        self.evidence["profiles"] = [{
            **null_profile(), "columns": ["value", "other"],
            "scope": "all current rows; WHERE value IS NULL OR other IS NULL"}]
        profile = [f for f in self.findings() if "集計" in f["summary"]]
        self.assertEqual([f["status"] for f in profile], ["confirmed"])

    def inherited_parent(self, kind="n"):
        self.snapshot["sections"]["tables"]["rows"][0]["has_inheritance_children"] = True
        self.column["column_not_null"] = True
        self.snapshot["sections"]["constraints"]["rows"] = [nn(type=kind, no_inherit=True)]

    def test_parent_local_not_null_does_not_cover_inherited_rows(self):
        for kind in ("n", "p"):
            with self.subTest(kind=kind):
                self.inherited_parent(kind)
                self.assertTrue(any(f["status"] == "needs_evidence" for f in self.findings()))

    def test_missing_inheritance_metadata_cannot_extend_local_guarantee(self):
        self.inherited_parent()
        del self.snapshot["sections"]["tables"]["rows"][0]["has_inheritance_children"]
        self.assertTrue(any(f["status"] == "needs_evidence" for f in self.findings()))

    def test_inherited_null_profile_preserves_observed_violation(self):
        self.inherited_parent()
        self.evidence["profiles"] = [{**null_profile(), "includes_children": True}]
        found = [f for f in self.findings() if "集計" in f["summary"]]
        self.assertEqual([f["status"] for f in found], ["confirmed"])

    def test_parent_null_profile_requires_explicit_row_scope(self):
        self.inherited_parent()
        self.evidence["profiles"] = [null_profile()]
        found = [f for f in self.findings() if "集計" in f["summary"]]
        self.assertEqual([f["status"] for f in found], ["needs_evidence"])
        self.assertIn("継承", " ".join(found[0]["missing"]))
        self.evidence["profiles"][0]["includes_children"] = False
        found = [f for f in self.findings() if "集計" in f["summary"]]
        self.assertEqual([f["status"] for f in found], ["needs_evidence"])
        self.assertIn("ONLY", " ".join(found[0]["missing"]))

    def test_parent_match_full_does_not_cover_inherited_rows(self):
        self.inherited_parent()
        self.snapshot["sections"]["columns"]["rows"].append({
            "relation": "public.required", "name": "other", "type": "integer",
            "column_not_null": False, "domain_not_null": False})
        self.snapshot["sections"]["constraints"]["rows"].append({
            "relation": "public.required", "name": "ref", "type": "f",
            "columns": ["value", "other"], "referenced_relation": "public.parent",
            "referenced_columns": ["a", "b"], "match_type": "f",
            "validated": True, "enforced": True})
        self.contract["tables"][0]["required_columns"] = ["other"]
        self.assertTrue(any(f["status"] == "needs_evidence" for f in self.findings()))

    def test_match_full_does_not_use_domain_or_unvalidated_seed(self):
        self.column["domain_not_null"]=True
        self.snapshot["sections"]["columns"]["rows"].append({"relation":"public.required","name":"other",
            "type":"integer","column_not_null":False,"domain_not_null":False})
        self.snapshot["sections"]["constraints"]["rows"]=[{"relation":"public.required","name":"ref","type":"f",
            "columns":["value","other"],"referenced_relation":"public.parent","referenced_columns":["a","b"],
            "match_type":"f","validated":True,"enforced":True}]
        self.contract["tables"][0]["required_columns"]=["other"]
        self.assertTrue(self.findings())
        self.column["domain_not_null"]=False
        self.column["column_not_null"]=True
        self.snapshot["sections"]["constraints"]["rows"].append(nn(validated=False))
        self.assertTrue(self.findings())
        self.snapshot["sections"]["constraints"]["rows"][-1]["validated"]=True
        self.snapshot["sections"]["constraints"]["rows"][-1]["enforced"]=False
        self.assertTrue(self.findings())
        self.snapshot["sections"]["constraints"]["rows"][-1]["enforced"]=True
        self.assertFalse(self.findings())

if __name__=="__main__":
    unittest.main()
