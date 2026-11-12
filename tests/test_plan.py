import importlib.util
import io
import json
from pathlib import Path
import contextlib
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("analyze_plan", ROOT / "skills/postgres-performance-diagnostics/scripts/analyze_plan.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture(name):
    return json.loads((ROOT / "tests/fixtures/plans" / name).read_text(encoding="utf-8"))


class PlanTests(unittest.TestCase):
    def test_spill_evidence_and_parallel_shortfall(self):
        plan = MODULE.analyze(fixture("spill.json"))["plans"][0]
        kinds = [candidate["kind"] for candidate in plan["candidates"]]
        self.assertEqual(kinds.count("sort_spill"), 2)
        self.assertIn("hash_batches", kinds)
        self.assertIn("heavy_filtering", kinds)
        self.assertIn("parallel_worker_shortfall", kinds)
        self.assertIn("cardinality_mismatch", kinds)

    def test_root_buffers_are_not_sum_of_children(self):
        plan = MODULE.analyze(fixture("spill.json"))["plans"][0]
        self.assertEqual(plan["root_buffers"]["Shared Read Blocks"], 100)
        self.assertEqual(plan["root_buffers"]["Shared Hit Blocks"], 200)

    def test_per_loop_cardinality_and_heap_fetches(self):
        nodes = MODULE.analyze(fixture("spill.json"))["plans"][0]["nodes"]
        scan = next(node for node in nodes if node["node_type"] == "Seq Scan")
        self.assertEqual(scan["cardinality"]["actual_over_estimate"], 1)
        self.assertEqual(scan["cardinality"]["approximate_total_output_rows"], 10)
        index_scan = next(node for node in nodes if node["node_type"] == "Index Only Scan")
        self.assertEqual(index_scan["metrics"]["Heap Fetches"], 400)

    def test_limit_is_not_statistics_failure(self):
        plan = MODULE.analyze(fixture("limit.json"))["plans"][0]
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["nodes"][1]["cardinality"]["status"], "partial_under_limit")

    def test_unexecuted_node_does_not_emit_mismatch(self):
        plan = MODULE.analyze({"Plan": {"Node Type": "Seq Scan", "Plan Rows": 500000,
                                       "Actual Rows": 0, "Actual Loops": 0}})["plans"][0]
        self.assertEqual(plan["nodes"][0]["cardinality"]["status"], "not_executed")
        self.assertEqual(plan["candidates"], [])

    def test_zero_observed_rows_are_finite(self):
        plan = MODULE.analyze({"Plan": {"Node Type": "Seq Scan", "Plan Rows": 50000,
                                       "Actual Rows": 0, "Actual Loops": 1}})["plans"][0]
        self.assertEqual(plan["nodes"][0]["cardinality"]["actual_over_estimate"], 0)
        self.assertEqual(plan["candidates"][0]["kind"], "cardinality_mismatch")
        json.dumps(plan, allow_nan=False)

    def test_plain_explain_has_no_invented_actuals(self):
        plan = MODULE.analyze({"Plan": {"Node Type": "Sort", "Plan Rows": 50000}})["plans"][0]
        self.assertEqual(plan["nodes"][0]["cardinality"]["status"], "not_measured")
        self.assertNotIn("execution_time_ms", plan)
        self.assertEqual(plan["candidates"], [])

    def test_normal_scans_sorts_and_hashes_are_not_bad_by_type(self):
        for kind in ("Seq Scan", "Sort", "Hash Join"):
            plan = MODULE.analyze({"Plan": {"Node Type": kind, "Plan Rows": 50,
                                           "Actual Rows": 50, "Actual Loops": 1}})["plans"][0]
            self.assertEqual(plan["candidates"], [])

    def test_sensitive_details_opt_in(self):
        data = fixture("spill.json")
        public = MODULE.analyze(data)
        self.assertNotIn("TOP_SECRET_PARAMETER", json.dumps(public))
        self.assertNotIn("private.customer_token", MODULE.markdown(public))
        self.assertIn("TOP_SECRET_PARAMETER", json.dumps(MODULE.analyze(data, include_details=True)))

    def test_jit_and_auto_explain_wrapper(self):
        plan = MODULE.analyze({"plan": fixture("spill.json")[0]})["plans"][0]
        self.assertEqual(plan["jit"]["Timing"]["Total"], 7.2)
        self.assertEqual(plan["execution_time_ms"], 935.2)

    def test_pg18_io_names_and_incremental_sort_disk(self):
        raw = {"Plan": {"Node Type": "Incremental Sort", "Actual Rows": 100,
                        "Actual Loops": 1, "Plan Rows": 100, "Shared I/O Read Time": 12.4,
                        "Local I/O Write Time": 2.1,
                        "Full-sort Groups": {"Sort Space Disk": {"Peak Sort Space Used": 128}},
                        "Pre-sorted Groups": {"Sort Space Disk": {"Peak Sort Space Used": 0}}}}
        report = MODULE.analyze(raw)["plans"][0]
        self.assertEqual(report["nodes"][0]["metrics"]["Shared I/O Read Time"], 12.4)
        self.assertEqual(report["nodes"][0]["metrics"]["Local I/O Write Time"], 2.1)
        self.assertEqual([item["kind"] for item in report["candidates"]], ["incremental_sort_spill"])

    def test_nested_loop_filtering_counts_all_loops(self):
        raw = {"Plan": {"Node Type": "Seq Scan", "Plan Rows": 1, "Actual Rows": 1, "Actual Loops": 5000,
                        "Rows Removed by Filter": 100}}
        kinds = [c["kind"] for c in MODULE.analyze(raw)["plans"][0]["candidates"]]
        self.assertIn("heavy_filtering", kinds)
        self.assertIn("repeated_seq_scan", kinds)

    def test_self_time_hotspots(self):
        raw = {"Plan": {"Node Type": "Hash Join", "Actual Total Time": 100, "Actual Loops": 1, "Plans": [
                   {"Node Type": "Seq Scan", "Relation Name": "big", "Actual Total Time": 70, "Actual Loops": 1},
                   {"Node Type": "Hash", "Actual Total Time": 20, "Actual Loops": 1, "Plans": [
                       {"Node Type": "Index Scan", "Actual Total Time": 0.05, "Actual Loops": 200}]}]},
               "Execution Time": 100}
        spots = MODULE.analyze(raw)["plans"][0]["hotspots"]
        self.assertEqual((spots[0]["relation"], spots[0]["self_ms_approx"]), ("big", 70))
        self.assertAlmostEqual(spots[0]["share_of_execution"], .7)
        hash_node = next(x for x in spots if x["node_type"] == "Hash")
        self.assertAlmostEqual(hash_node["self_ms_approx"], 10)
        self.assertIn("Self-time hotspots", MODULE.markdown(MODULE.analyze(raw)))

    def test_lossy_bitmap_planning_and_jit_candidates(self):
        raw = {"Plan": {"Node Type": "Bitmap Heap Scan", "Plan Rows": 10, "Actual Rows": 10, "Actual Loops": 1,
                        "Exact Heap Blocks": 5, "Lossy Heap Blocks": 900, "Rows Removed by Index Recheck": 5000},
               "Planning Time": 80, "Execution Time": 40,
               "JIT": {"Functions": 4, "Timing": {"Total": 30}}}
        kinds = {c["kind"] for c in MODULE.analyze(raw)["plans"][0]["candidates"]}
        self.assertEqual(kinds, {"lossy_bitmap", "planning_dominates", "jit_overhead"})

    def test_semi_join_inner_side_is_not_a_statistics_failure(self):
        # Shape observed on PostgreSQL 18.6: EXISTS becomes a semi nested loop that stops at the first match.
        def plan(join_type):
            return {"Plan": {"Node Type": "Nested Loop", "Join Type": join_type, "Plan Rows": 667, "Actual Rows": 2000,
                             "Actual Loops": 1, "Plans": [
                {"Node Type": "Function Scan", "Parent Relationship": "Outer", "Plan Rows": 2000, "Actual Rows": 2000,
                 "Actual Loops": 1},
                {"Node Type": "Index Only Scan", "Parent Relationship": "Inner", "Plan Rows": 66667, "Actual Rows": 1,
                 "Actual Loops": 2000, "Index Searches": 2000}]}}
        semi = MODULE.analyze(plan("Semi"))["plans"][0]
        inner = semi["nodes"][2]
        self.assertEqual(inner["cardinality"]["status"], "partial_early_exit")
        self.assertEqual(inner["metrics"]["Index Searches"], 2000)
        self.assertEqual(semi["candidates"], [])
        # The same numbers under an ordinary join are a real 66667x overestimate.
        inner_join = MODULE.analyze(plan("Inner"))["plans"][0]
        self.assertEqual([c["path"] for c in inner_join["candidates"] if c["kind"] == "cardinality_mismatch"],
                         ["Plan/Plans/1"])

    def test_mismatch_is_weighed_over_all_loops_from_one_row_up(self):
        def kinds(plan_rows, actual, loops):
            raw = {"Plan": {"Node Type": "Index Scan", "Plan Rows": plan_rows, "Actual Rows": actual, "Actual Loops": loops}}
            return [c["kind"] for c in MODULE.analyze(raw)["plans"][0]["candidates"]]
        # 50 rows per loop against an estimate of 1 is small once, and 500000 unexpected rows over 10000 loops.
        self.assertEqual(kinds(1, 50, 1), [])
        self.assertEqual(kinds(1, 50, 10000), ["cardinality_mismatch"])
        # An estimate of one row with "0" actual (a rounded average) is the planner's floor, not a miss.
        self.assertEqual(kinds(1, 0, 10000), [])
        self.assertEqual(kinds(1, 0.2, 10000), [])
        self.assertEqual(kinds(50000, 0, 1), ["cardinality_mismatch"])

    def test_tuplestore_spill_join_filter_and_capture_context(self):
        raw = {"Plan": {"Node Type": "Nested Loop", "Join Type": "Inner", "Plan Rows": 10, "Actual Rows": 10,
                        "Actual Loops": 1, "Actual Total Time": 80.0, "Rows Removed by Join Filter": 990000,
                        "Join Filter": "(a.code = b.code)", "Plans": [
                   {"Node Type": "Seq Scan", "Relation Name": "a", "Plan Rows": 1000, "Actual Rows": 1000,
                    "Actual Loops": 1, "Actual Total Time": 1.0},
                   {"Node Type": "Materialize", "Plan Rows": 1000, "Actual Rows": 1000, "Actual Loops": 1000,
                    "Actual Total Time": 0.05, "Storage": "Disk", "Maximum Storage": 9343}]},
               "Settings": {"work_mem": "64kB", "enable_hashjoin": "off"}, "Query Identifier": -6004425419096503781,
               "Query Text": "SELECT 'TOP_SECRET_PARAMETER'"}
        plan = MODULE.analyze({"plan": raw, "capture": {"source": "auto_explain jsonlog line 7",
                                                         "logged_duration_ms": 100.0}})["plans"][0]
        kinds = {c["kind"]: c for c in plan["candidates"]}
        self.assertEqual(kinds["join_filter_discards"]["evidence"]["approximate_total_removed"], 990000)
        self.assertEqual(kinds["tuplestore_disk"]["evidence"]["maximum_storage_kb"], 9343)
        self.assertEqual(plan["settings"], {"work_mem": "64kB", "enable_hashjoin": "off"})
        self.assertEqual(plan["query_id"], "-6004425419096503781")
        # No Execution Time in auto_explain output: shares use the duration its log line reported.
        self.assertEqual((plan["time_basis"], plan["logged_duration_ms"]), ("logged_duration", 100.0))
        self.assertAlmostEqual(plan["hotspots"][0]["share_of_execution"], 0.5)
        text = MODULE.markdown({"plans": [plan], "limitations": []})
        self.assertIn("enable_hashjoin", text)
        self.assertIn("of the logged duration", text)
        self.assertNotIn("TOP_SECRET_PARAMETER", json.dumps(plan) + text)
        bare = MODULE.analyze({"Plan": {"Node Type": "Seq Scan", "Actual Total Time": 4.0, "Actual Loops": 1}})["plans"][0]
        self.assertEqual((bare["time_basis"], bare["hotspots"][0]["share_of_execution"]), ("root_node", 1))

    def test_anti_join_small_output_is_not_missing_partners(self):
        def join(join_type):
            return {"Plan": {"Node Type": "Hash Join", "Join Type": join_type, "Plan Rows": 40, "Actual Rows": 40,
                             "Actual Loops": 1, "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "items", "Plan Rows": 800000, "Actual Rows": 800000, "Actual Loops": 1},
                {"Node Type": "Hash", "Plan Rows": 800000, "Actual Rows": 800000, "Actual Loops": 1, "Plans": [
                    {"Node Type": "Seq Scan", "Relation Name": "orders", "Plan Rows": 800000, "Actual Rows": 800000,
                     "Actual Loops": 1}]}]}}
        found = lambda t: [c["path"] for c in MODULE.analyze(join(t))["plans"][0]["candidates"]
                           if c["kind"] == "join_discards_scanned_rows"]
        self.assertEqual(found("Inner"), ["Plan/Plans/0", "Plan/Plans/1/Plans/0"])
        self.assertEqual(found("Semi"), ["Plan/Plans/0"])  # Only outer rows are returned by a semi join.
        self.assertEqual(found("Anti"), [])                # Few rows out means most rows DID match.

    def test_markdown_lists_one_line_per_node_up_to_the_limit(self):
        children = [{"Node Type": "Seq Scan", "Relation Name": f"part_{i}", "Plan Rows": 5, "Actual Rows": 5,
                     "Actual Loops": 1, "Shared Hit Blocks": 3, "Startup Cost": 0.0, "Total Cost": 9.5}
                    for i in range(120)]
        report = MODULE.analyze({"Plan": {"Node Type": "Append", "Plan Rows": 600, "Actual Rows": 600,
                                          "Actual Loops": 1, "Plans": children}})
        text = MODULE.markdown(report, max_nodes=30)
        node_lines = [line for line in text.splitlines() if line.startswith("- `Plan")]
        self.assertEqual(len(node_lines), 30)
        self.assertIn("91 more node(s) not shown", text)
        self.assertIn("rows est 5 / act 5 x1", node_lines[1])
        self.assertIn('"Shared Hit Blocks": 3', node_lines[1])
        self.assertNotIn("Total Cost", text)  # Costs stay in the JSON report.
        self.assertEqual(len(report["plans"][0]["nodes"]), 121)

    def test_invalid_structure_rejected(self):
        for data in ([], {}, {"Plan": []}, [{"wrong": True}], {"Plan": {"Node Type": "Scan", "Plans": {}}}):
            with self.assertRaises(ValueError):
                MODULE.analyze(data)

    def test_cli_json_output_and_errors_do_not_echo_source(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reports" / "report.json"
            code = MODULE.main([str(ROOT / "tests/fixtures/plans/spill.json"), "--output", str(output), "--format", "json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["kind"], "explain_analysis")
            bad = Path(directory) / "bad.json"
            bad.write_text("TOP_SECRET_PARAMETER", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(MODULE.main([str(bad)]), 2)
            self.assertNotIn("TOP_SECRET_PARAMETER", err.getvalue())


if __name__ == "__main__":
    unittest.main()
