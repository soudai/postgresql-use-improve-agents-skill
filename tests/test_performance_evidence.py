"""Regression cases for evidence boundaries found by the cross-skill review."""
import contextlib
import io
import json
import tempfile
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/postgres-performance-diagnostics/scripts"


def module(name):
    spec = importlib.util.spec_from_file_location(name + "_evidence", SCRIPTS / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


SAMPLE, PLAN, ANALYZE = (module(name) for name in ("sample_activity", "analyze_plan", "analyze"))
SETTINGS = {"autovacuum": "on", "track_counts": "on", "autovacuum_vacuum_threshold": "50",
            "autovacuum_vacuum_scale_factor": "0.2", "autovacuum_vacuum_max_threshold": "100000000",
            "autovacuum_vacuum_insert_threshold": "1000", "autovacuum_vacuum_insert_scale_factor": "0.2",
            "autovacuum_analyze_threshold": "50", "autovacuum_analyze_scale_factor": "0.1"}


def table(**overrides):
    row = {"schemaname": "public", "relname": "frozen", "relkind": "r", "reltuples": 102000,
           "relpages": 452, "relallfrozen": 442, "n_ins_since_vacuum": 2000,
           "n_dead_tup": 0, "n_mod_since_analyze": 0}
    row.update(overrides)
    return row


def scan(expression):
    return {"Plan": {"Node Type": "Seq Scan", "Plan Rows": 33333, "Actual Rows": 0,
                     "Actual Loops": 1, "Filter": expression}}


class EvidenceTests(unittest.TestCase):
    def test_hidden_activity_is_unknown_not_background_or_zero(self):
        summary = SAMPLE.summarize([{"track_activities": "on", "sessions": [
            {"pid": 10, "backend_type": None, "state": None}]}])
        self.assertIsNone(summary["average_active_sessions"])
        self.assertIsNone(summary["peak_active_sessions"])
        self.assertEqual(summary["observed_average_active_sessions"], 0)
        self.assertEqual(summary["unknown_session_samples"], 1)
        self.assertEqual(summary["background"], [])
        self.assertTrue(summary["warnings"])

    def test_partial_visibility_preserves_observed_activity_as_lower_bound(self):
        summary = SAMPLE.summarize([{"track_activities": "on", "sessions": [
            {"pid": 10, "backend_type": None, "state": None},
            {"pid": 11, "backend_type": "client backend", "state": "active"}]}])
        self.assertIsNone(summary["average_active_sessions"])
        self.assertEqual(summary["observed_average_active_sessions"], 1)
        self.assertEqual(summary["activity_coverage"], "incomplete")
        self.assertEqual(summary["wait_profile"][0]["samples"], 1)
        self.assertEqual(summary["wait_profile_share_scope"], "observed_active_sessions")

    def test_disabled_and_missing_tracking_are_not_zero_load(self):
        for sample in ({"track_activities": "off", "sessions": []}, {"sessions": []},
                       {"track_activities": "on", "sessions": [
                           {"backend_type": "client backend", "state": "disabled"}]}):
            with self.subTest(sample=sample):
                self.assertIsNone(SAMPLE.summarize([sample])["average_active_sessions"])
        self.assertEqual(SAMPLE.summarize([{"track_activities": "on", "sessions": []}])
                         ["average_active_sessions"], 0)

    def test_quoted_parameter_text_is_not_plan_provenance(self):
        for expression in ("(i::text || '$1') > 'z'", '("$1" = 0)',
                           "(v = E'escaped\\\'$1')", "(v = $tag$literal $1$tag$)",
                           "(customer$1 = 0)", "(v = '$1''$2')", "(v = $値$literal $1$値$)",
                           "(v = 0) /* outer /* $1 */ $2 */ -- $3"):
            with self.subTest(expression=expression):
                report = PLAN.analyze(scan(expression))["plans"][0]
                self.assertFalse(report["uses_parameter_placeholders"])
                self.assertNotIn("generic_plan_misestimate", {c["kind"] for c in report["candidates"]})

    def test_lexer_keeps_real_tokens_beside_literals(self):
        self.assertEqual(PLAN.parameter_tokens("(v = '$2') AND i = $1 AND j = $10"), {"$1", "$10"})

    def test_cli_capture_metadata_and_relation_evidence(self):
        raw = {"Plan": {"Node Type": "Bitmap Heap Scan", "Schema": "public", "Relation Name": "items",
                        "Actual Loops": 1, "Lossy Heap Blocks": 128, "Plans": [
                            {"Node Type": "Bitmap Index Scan", "Index Name": "items_brin"}]}}
        with tempfile.TemporaryDirectory() as directory:
            plan_path, evidence_path = (Path(directory) / name for name in ("plan.json", "relation.json"))
            plan_path.write_text(json.dumps(raw))
            evidence_path.write_text(json.dumps({"relation": {"schema": "public", "name": "items"},
                                                 "indexes": [{"index_name": "items_brin", "access_method": "brin"}]}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = PLAN.main([str(plan_path), "--format", "json", "--relation-evidence", str(evidence_path)])
            self.assertEqual(code, 0)
            candidate = json.loads(output.getvalue())["plans"][0]["candidates"][0]
            self.assertEqual(candidate["evidence"]["index_access_methods"], ["brin"])
            plan_path.write_text(json.dumps({"plan": scan("(i = $1)"),
                                            "capture": {"plan_mode": "generic", "source": "fixture capture"}}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = PLAN.main([str(plan_path), "--format", "json"])
            self.assertEqual(code, 0)
            self.assertIn("generic_plan_misestimate", {c["kind"] for c in json.loads(output.getvalue())
                                                     ["plans"][0]["candidates"]})
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(PLAN.main([str(plan_path), "--plan-mode", "generic", "--plan-source", ""]), 2)

    def test_unquoted_parameter_is_unclassified_without_provenance(self):
        report = PLAN.analyze(scan("(i = $1)"))["plans"][0]
        self.assertTrue(report["uses_parameter_placeholders"])
        self.assertEqual(report["plan_capture"]["plan_mode"], "unknown")
        self.assertNotIn("generic_plan_misestimate", {c["kind"] for c in report["candidates"]})

    def test_initplan_parameter_is_not_prepared_statement_evidence(self):
        data = scan("(i = $0)")
        data["Plan"]["Plans"] = [{"Node Type": "Result", "Parent Relationship": "InitPlan",
                                  "Subplan Name": "InitPlan 1 (returns $0)"}]
        report = PLAN.analyze(data)["plans"][0]
        self.assertFalse(report["uses_parameter_placeholders"])
        self.assertNotIn("generic_plan_misestimate", {c["kind"] for c in report["candidates"]})

    def test_generic_detection_requires_capture_source(self):
        for mode in ("generic", "custom"):
            report = PLAN.analyze(scan("(i = $1)"), plan_mode=mode,
                                  plan_source="fixture: EXPLAIN EXECUTE with forced mode")["plans"][0]
            self.assertEqual("generic_plan_misestimate" in {c["kind"] for c in report["candidates"]},
                             mode == "generic")
        with self.assertRaises(ValueError):
            PLAN.analyze(scan("(i = $1)"), plan_mode="generic")

    def test_lossy_bitmap_does_not_prove_memory_exhaustion(self):
        raw = {"Plan": {"Node Type": "Bitmap Heap Scan", "Schema": "public", "Relation Name": "items",
                        "Plan Rows": 1, "Actual Rows": 1, "Actual Loops": 1,
                        "Lossy Heap Blocks": 128, "Plans": [
                            {"Node Type": "Bitmap Index Scan", "Index Name": "items_brin"}]}}
        plan = PLAN.analyze(raw)["plans"][0]
        lossy = next(c for c in plan["candidates"] if c["kind"] == "lossy_bitmap")
        self.assertNotIn("exceeded work_mem", lossy["next_step"])
        self.assertEqual(lossy["evidence"]["cause_status"], "unconfirmed")
        relation = {"relation": {"schema": "public", "name": "items"},
                    "indexes": [{"index_name": "items_brin", "access_method": "brin"}]}
        lossy = PLAN.analyze(raw, relation_evidence=relation)["plans"][0]["candidates"][0]
        self.assertEqual(lossy["evidence"]["index_access_methods"], ["brin"])
        self.assertIn("BRIN", lossy["next_step"])

    def test_relation_evidence_cannot_match_another_schema(self):
        raw = {"Plan": {"Node Type": "Bitmap Heap Scan", "Schema": "other", "Relation Name": "items",
                        "Actual Loops": 1, "Lossy Heap Blocks": 128, "Plans": [
                            {"Node Type": "Bitmap Index Scan", "Index Name": "items_brin"}]}}
        relation = {"relation": {"schema": "public", "name": "items"},
                    "indexes": [{"index_name": "items_brin", "access_method": "brin"}]}
        lossy = PLAN.analyze(raw, relation_evidence=relation)["plans"][0]["candidates"][0]
        self.assertEqual(lossy["evidence"]["index_access_methods"], [])

    def test_insert_trigger_uses_unfrozen_fraction(self):
        found = ANALYZE.autovacuum_backlog([table()], SETTINGS)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0]["insert_threshold"], 1000 + .2 * 102000 * (1 - 442 / 452))
        self.assertIn("insert", found[0]["exceeded_triggers"])

    def test_missing_freeze_evidence_is_unassessed(self):
        row = table()
        del row["relallfrozen"]
        self.assertEqual(ANALYZE.autovacuum_backlog([row], SETTINGS), [])
        result = ANALYZE.autovacuum_backlog([row], SETTINGS, include_unknown=True)[0]
        self.assertEqual(result["reason"], "insufficient_trigger_evidence")
        self.assertIn("insert", result["unknown_triggers"])

    def test_unknown_insert_evidence_does_not_hide_known_dead_backlog(self):
        result = ANALYZE.autovacuum_backlog([table(relallfrozen=None, n_dead_tup=50000)],
                                           SETTINGS, include_unknown=True)[0]
        self.assertEqual(result["reason"], "past_trigger_threshold")
        self.assertIn("dead", result["exceeded_triggers"])
        self.assertIn("insert", result["unknown_triggers"])

    def test_missing_or_disabled_track_counts_is_unassessed(self):
        for value in (None, "off"):
            settings = dict(SETTINGS, track_counts=value)
            found = ANALYZE.autovacuum_backlog([table()], settings, include_unknown=True)
            self.assertEqual(found[0]["reason"], "insufficient_trigger_evidence")
            self.assertEqual(set(found[0]["unknown_triggers"]), {"dead", "insert", "analyze"})

    def test_zero_threshold_still_detects_positive_work(self):
        settings = dict(SETTINGS, autovacuum_vacuum_insert_threshold="0",
                        autovacuum_vacuum_insert_scale_factor="0")
        found = ANALYZE.autovacuum_backlog([table(n_ins_since_vacuum=1)], settings)
        self.assertIn("insert", found[0]["exceeded_triggers"])
        self.assertEqual(found[0]["insert_threshold"], 0)
        json.dumps(found, allow_nan=False)

    def test_unfrozen_bounds_disabled_insert_and_missing_reltuples(self):
        for pages, frozen, expected in ((452, 1000, 1000), (0, 0, 21400), (452, 0, 21400)):
            found = ANALYZE.autovacuum_backlog([table(relpages=pages, relallfrozen=frozen,
                                                      n_ins_since_vacuum=30000)], SETTINGS)
            self.assertEqual(found[0]["insert_threshold"], expected)
        found = ANALYZE.autovacuum_backlog(
            [table(relallfrozen=None, reloptions=["autovacuum_vacuum_insert_threshold=-1"])],
            SETTINGS, include_unknown=True)
        self.assertEqual(found, [])
        found = ANALYZE.autovacuum_backlog([table(reltuples=None)], SETTINGS, include_unknown=True)
        self.assertEqual(set(found[0]["unknown_triggers"]), {"dead", "insert", "analyze"})


if __name__ == "__main__":
    unittest.main()
