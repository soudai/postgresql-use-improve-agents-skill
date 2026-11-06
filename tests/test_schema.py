import argparse
from contextlib import redirect_stderr
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/"skills/postgres-schema-review/scripts"
def module(name):
    spec=importlib.util.spec_from_file_location("schema_"+name,SCRIPTS/(name+".py"))
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
REVIEW=module("review")
COLLECT=module("collect")


def snapshot():
    return {"schema_version":1,"metadata":{"database":"test","server_version_num":180006,
            "collected_at":"2026-11-06T00:00:00Z","requested_schemas":["public"]},
            "sections":{name:{"status":"ok","truncated":False,"rows":[]} for name in REVIEW.SECTIONS}}


def sample():
    value=snapshot()
    value["sections"]["tables"]["rows"]=[{"relation":"public.orders","schema":"public","name":"orders","kind":"r"}]
    value["sections"]["columns"]["rows"]=[{"relation":"public.orders","name":"id","type":"integer","not_null":False}]
    return value


class ReviewTests(unittest.TestCase):
    def test_empty_is_not_global_health(self):
        report=REVIEW.review(snapshot())
        self.assertFalse(report["findings"])
        self.assertTrue(report["limitations"])
        self.assertEqual(report["observations"],{"writes":0,"profiles":0,"contention":0})

    def test_missing_and_truncated_coverage(self):
        value=sample()
        del value["sections"]["constraints"]
        value["sections"]["tables"]["truncated"]=True
        report=REVIEW.review(value)
        self.assertEqual(report["coverage"]["constraints"]["status"],"missing")
        self.assertTrue(report["coverage"]["tables"]["truncated"])
        self.assertTrue(any("業務上の設計契約" in s for s in report["limitations"]))
        self.assertEqual(report["coverage"]["statistics"]["missing_columns"],1)

    def test_omitted_truncation_is_unknown_coverage(self):
        value=sample()
        del value["sections"]["constraints"]["truncated"]
        report=REVIEW.review(value)
        self.assertTrue(report["coverage"]["constraints"]["truncated"])
        self.assertTrue(any("constraints: incomplete" in x for x in report["limitations"]))

    def test_failed_inventory_rows_do_not_establish_scope(self):
        value=sample()
        value["sections"]["tables"]["status"]="error"
        report=REVIEW.review(value,{"schema_version":1,"tables":[{"relation":"public.orders"}]})
        self.assertTrue(any(f["rule"]=="schema.coverage" for f in report["findings"]))

    def test_orphan_observations_are_visible(self):
        for section, observation in (
            ("profiles", {"relation":"public.typo","kind":"null","columns":["amount"],
                          "violations":2,"rows_checked":3,"scope":"all"}),
            ("writes", {"relation":"public.typo","operation":"UPDATE"}),
            ("contention", {"relation":"public.typo","wait_ms":10}),
        ):
            with self.subTest(section=section):
                result=REVIEW.review(sample(),evidence={"schema_version":1,section:[observation]})
                self.assertTrue(any(f["rule"]=="schema.coverage" and f["relation"]=="public.typo"
                                    and f["status"]=="needs_evidence" for f in result["findings"]))

    def test_external_reference_does_not_require_invented_remote_names(self):
        contract={"schema_version":1,"tables":[{"relation":"public.orders","source":"ADR external CRM",
                   "relationships":[{"columns":["id"],"enforce":False}]}]}
        report=REVIEW.review(sample(),contract)
        self.assertTrue(any(f["rule"]=="schema.fk" and f["status"]=="exception" for f in report["findings"]))

    def test_unknown_contract_table_is_unconfirmed(self):
        report=REVIEW.review(snapshot(),{"schema_version":1,"tables":[{"relation":"public.unknown","source":"ADR"}]})
        self.assertTrue(any(f["status"]=="needs_evidence" and f["rule"]=="schema.coverage" for f in report["findings"]))

    def test_exception_retains_underlying_violation(self):
        contract={"schema_version":1,"tables":[{"relation":"public.orders","source":"ADR","required_columns":["id"],
                    "exceptions":[{"rule":"schema.null","source":"migration","reason":"migration in progress"}]}]}
        findings=REVIEW.review(sample(),contract)["findings"]
        result=next(f for f in findings if f["rule"]=="schema.null")
        self.assertEqual(result["status"],"exception")
        self.assertEqual(result["underlying_status"],"confirmed")
        self.assertTrue(result["evidence"])

    def test_malformed_inputs_are_clear_errors(self):
        good={"schema_version":1,"tables":[]}
        for contract in ({"schema_version":True}, {"schema_version":2}, {"schema_version":1,"tables":{}},
                         {"schema_version":1,"tables":[{"relation":"public.orders","required_columns":"id"}]},
                         {"schema_version":1,"tables":[{"relation":"public.orders","relationships":[
                             {"columns":["id"],"referenced_columns":["tenant","id"],"referenced_relation":"public.p"}]}]},
                         {"schema_version":1,"tables":[{"relation":"public.orders","requried_columns":["id"]}]},
                         {"schema_version":1,"tables":[{"relation":"public.orders","exceptions":[{"rule":"schema.null","reason":"x"}]}]}):
            with self.subTest(contract=contract),self.assertRaises(REVIEW.InputError):
                REVIEW.review(sample(),contract)
        for evidence in ({"schema_version":1,"contention":[{"relation":"public.orders","wait_ms":True}]},
                         {"schema_version":1,"contention":[{"relation":"public.orders","wait_ms":float("nan")}]},
                         {"schema_version":1,"writes":[{"relation":"public.orders","operation":"UPDATE","after_finalization":"true"}]},
                         {"schema_version":1,"writes":[{"relation":"public.orders","operation":"UPDATE","observed_at":"yesterday"}]},
                         {"schema_version":1,"profiles":[{"relation":"public.orders","kind":"null","columns":["id"],
                                                        "violations":2,"rows_checked":1,"scope":"all"}]}):
            with self.subTest(evidence=evidence),self.assertRaises(REVIEW.InputError):
                REVIEW.review(sample(),good,evidence)

    def test_duplicate_relation_rejected(self):
        value=sample()
        value["sections"]["tables"]["rows"]*=2
        with self.assertRaises(REVIEW.InputError):
            REVIEW.review(value)

    def test_no_raw_extra_metadata_or_sql_in_output(self):
        value=sample()
        value["metadata"]["password"]="SECRET_CANARY"
        value["sections"]["columns"]["rows"][0]["default_expression"]="'SECRET_CANARY'"
        text=json.dumps(REVIEW.review(value))
        self.assertNotIn("SECRET_CANARY",text)

    def test_markdown_does_not_render_supplied_html_or_links(self):
        value=sample()
        value["metadata"]["database"]="<img src=x>[payload](https://bad)"
        text=REVIEW.markdown(REVIEW.review(value))
        self.assertNotIn("<img",text)
        self.assertNotIn("[payload](",text)

    def test_cli_roundtrip_and_private_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/"snapshot.json"; output=root/"report.json"
            source.write_text(json.dumps(sample()))
            self.assertEqual(REVIEW.main([str(source),"--format","json","--output",str(output)]),0)
            self.assertIn("coverage",json.loads(output.read_text()))
            source.write_text('{"schema_version":NaN,"password":"SECRET_CANARY"}')
            log=io.StringIO()
            with redirect_stderr(log):
                self.assertEqual(REVIEW.main([str(source)]),2)
            self.assertNotIn("SECRET_CANARY",log.getvalue())
            self.assertIn("invalid JSON",log.getvalue())


class CollectionTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(schema=["public"],row_limit=1,statement_timeout_ms=50,
                    lock_timeout_ms=20,psql="psql",docker_container=None)

    def test_all_sections_bounded_and_stats_visibility(self):
        raw={"metadata":{"server_version_num":180006},"rows":{n:[] for n in COLLECT.SECTIONS}}
        raw["rows"]["tables"]=[{"relation":"a"},{"relation":"b"}]
        raw["rows"]["columns"]=[{"relation":"a","name":"id"},{"relation":"b","name":"id"}]
        with patch.object(COLLECT,"run_psql",return_value=raw):
            result=COLLECT.collect(self.args())
        self.assertTrue(result["sections"]["tables"]["truncated"])
        self.assertEqual(len(result["sections"]["tables"]["rows"]),1)
        self.assertTrue(result["sections"]["columns"]["truncated"])
        self.assertEqual(result["sections"]["statistics"]["missing_columns"],1)

    def test_schema_is_literal_and_catalog_expressions_are_not_exported(self):
        query=COLLECT.build_query(["Odd'; SELECT 'CANARY"],3)
        self.assertIn("'Odd''; SELECT ''CANARY'",query)
        self.assertNotIn("pg_get_expr",query)
        self.assertNotIn("most_common_vals",query)
        self.assertNotIn("histogram_bounds",query)
        self.assertEqual(query.count("LIMIT 4"),6)

    def test_read_only_one_transaction_and_no_psqlrc(self):
        captured={}
        def run(command,**kwargs):
            captured.update(command=command,**kwargs)
            return subprocess.CompletedProcess(command,0,stdout="{}",stderr="")
        with patch.object(COLLECT.subprocess,"run",side_effect=run):
            self.assertEqual(COLLECT.run_psql(self.args(),"SELECT 1"),{})
        self.assertIn("-X",captured["command"])
        self.assertIn("--no-password",captured["command"])
        self.assertIn("REPEATABLE READ READ ONLY",captured["input"])
        self.assertIn("search_path=pg_catalog",captured["input"])
        self.assertEqual(captured["input"].count("BEGIN"),1)

    def test_server_errors_do_not_leak_sql_or_password(self):
        outcome=subprocess.CompletedProcess([],1,stdout="",stderr="ERROR: 42501 SECRET_CANARY\n")
        with patch.object(COLLECT.subprocess,"run",return_value=outcome):
            with self.assertRaisesRegex(COLLECT.CollectionError,"sqlstate_42501") as caught:
                COLLECT.run_psql(self.args(),"SELECT 1")
        self.assertNotIn("SECRET_CANARY",str(caught.exception))

    def test_failed_collection_leaves_explicit_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"failed.json"
            with patch.object(COLLECT,"collect",side_effect=COLLECT.CollectionError("psql_failed")),redirect_stderr(io.StringIO()):
                self.assertEqual(COLLECT.main(["--output",str(target)]),1)
            report=REVIEW.review(json.loads(target.read_text()))
            self.assertTrue(all(c["status"]=="error" for c in report["coverage"].values()))
            self.assertFalse(report["findings"])


if __name__=="__main__":
    unittest.main()
