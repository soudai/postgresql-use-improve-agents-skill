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
