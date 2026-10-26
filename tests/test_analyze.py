import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("interval_analysis", ROOT / "skills/postgres-performance-diagnostics/scripts/analyze.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def pair():
    def snap(minute):
        time = f"2026-10-26T00:{minute:02d}:00+00:00"
        sections = {name: {"status": "ok", "collected_at": time, "rows": [], "truncated": False}
                    for name in MODULE.SPECS}
        sections["settings"] = {"status": "ok", "rows": [
            {"name": name, "setting": "on"} for name in ("track_counts", "track_activities",
                "track_io_timing", "track_wal_io_timing", "pg_stat_statements.track_planning")]}
        sections["settings"]["rows"].append({"name": "pg_stat_statements.track", "setting": "top"})
        sections["database"]["rows"] = [{"datid": 5, "stats_reset": None, "deadlocks": minute}]
        sections["pgss_info"] = {"status": "ok", "rows": [{"stats_reset": "2026-10-01T00:00:00Z", "dealloc": 0}]}
        sections["pgss"]["rows"] = [{"dbid": 5, "userid": 10, "queryid": 9223372036854775807,
            "toplevel": True, "stats_since": "2026-10-25T00:00:00Z", "calls": 10 + minute * 2,
            "total_exec_time": 100 + minute * 10, "mean_exec_time": 1000 - minute * 900,
            "rows": 100 + minute * 6, "temp_blks_written": 2 + minute,
            "shared_blks_read": 2 + minute, "shared_blks_hit": 10 + minute * 9,
            "shared_blk_read_time": 1 + minute, "query": "SELECT 'SENSITIVE_TOKEN'"}]
        return {"schema_version": 1, "instance_id": "fixture", "started_at": time, "finished_at": time,
            "metadata": {"database_oid": 5, "database": "fixture", "server_version_num": 180006,
                "postmaster_start": "2026-10-01T00:00:00Z", "in_recovery": False, "block_size": 8192,
                "extensions": [{"name": "pg_stat_statements", "version": "1.12"}]},
            "sections": sections, "warnings": []}
    return snap(0), snap(1)


class IntervalTests(unittest.TestCase):
    def test_malformed_or_null_identity_is_rejected(self):
        a, b = pair()
        for invalid in ([], "text", {"metadata": None}, {"metadata": {}, "sections": []}):
            self.assertRaises(ValueError, MODULE.analyze, invalid, b)
        a["metadata"]["postmaster_start"] = b["metadata"]["postmaster_start"] = None
        self.assertRaises(ValueError, MODULE.analyze, a, b)

    def test_interval_average_and_rates_not_lifetime_mean(self):
        report = MODULE.analyze(*pair())
        entry = report["rankings"]["sql_total_exec_ms"][0]
        self.assertEqual(entry["mean_exec_ms"], 5)
        self.assertAlmostEqual(entry["per_second"]["calls"], 2 / 60)
        self.assertEqual(entry["rows_per_call"], 3)
        self.assertEqual(entry["temp_written_bytes"], 8192)
        self.assertEqual(entry["shared_hit_ratio"], .9)
        self.assertEqual(entry["identity"]["queryid"], 9223372036854775807)
        self.assertNotIn("mean_exec_time", entry["delta"])
        self.assertNotIn("SENSITIVE_TOKEN", json.dumps(report))

    def test_identity_and_restart_changes_rejected(self):
        for field, value in (("postmaster_start", "2026-10-26T00:00:30Z"),
            ("database_oid", 6), ("database", "other"), ("server_version_num", 190000),
            ("in_recovery", True), ("block_size", 16384)):
            with self.subTest(field=field):
                a, b = pair()
                b["metadata"][field] = value
                self.assertRaises(ValueError, MODULE.analyze, a, b)

    def test_instance_id_and_overlap_rejected(self):
        a, b = pair()
        b["instance_id"] = "other"
        self.assertRaises(ValueError, MODULE.analyze, a, b)
        a, b = pair()
        b["started_at"] = a["finished_at"]
        self.assertRaises(ValueError, MODULE.analyze, a, b)

    def test_global_pgss_reset_suppresses_query_interval(self):
        a, b = pair()
        b["sections"]["pgss_info"]["rows"][0]["stats_reset"] = "2026-10-26T00:00:30Z"
        report = MODULE.analyze(a, b)
        self.assertEqual(report["deltas"]["pgss"], [])
        self.assertTrue(any("reset marker changed" in warning for warning in report["warnings"]))

    def test_entry_recreated_even_if_counters_have_grown(self):
        a, b = pair()
        b["sections"]["pgss"]["rows"][0]["stats_since"] = "2026-10-26T00:00:30Z"
        self.assertEqual(MODULE.analyze(a, b)["deltas"]["pgss"], [])

    def test_deallocation_does_not_discard_surviving_marked_entry(self):
        a, b = pair()
        b["sections"]["pgss_info"]["rows"][0]["dealloc"] = 5
        result = MODULE.analyze(a, b)
        self.assertEqual(len(result["deltas"]["pgss"]), 1)
        self.assertTrue(any("eviction" in x for x in result["warnings"]))

    def test_missing_entry_marker_is_not_assumed_continuous(self):
        a, b = pair()
        for snapshot in (a, b):
            del snapshot["sections"]["pgss"]["rows"][0]["stats_since"]
        self.assertEqual(MODULE.analyze(a, b)["deltas"]["pgss"], [])

    def test_negative_counter_discards_whole_entry(self):
        a, b = pair()
        b["sections"]["pgss"]["rows"][0]["calls"] = 2
        self.assertEqual(MODULE.analyze(a, b)["deltas"]["pgss"], [])

    def test_new_entry_without_baseline_is_not_a_full_interval(self):
        a, b = pair()
        a["sections"]["pgss"]["rows"] = []
        self.assertEqual(MODULE.analyze(a, b)["deltas"]["pgss"], [])

    def test_null_and_duplicate_queryids_do_not_merge(self):
        for target in ("null", "old_duplicate", "new_duplicate"):
            a, b = pair()
            if target == "null":
                for snap in (a, b):
                    snap["sections"]["pgss"]["rows"][0]["queryid"] = None
            else:
                snap = a if target == "old_duplicate" else b
                snap["sections"]["pgss"]["rows"].append(copy.deepcopy(snap["sections"]["pgss"]["rows"][0]))
            self.assertEqual(MODULE.analyze(a, b)["deltas"]["pgss"], [])

    def test_timing_off_is_absent_not_zero(self):
        a, b = pair()
        for snap in (a, b):
            next(x for x in snap["sections"]["settings"]["rows"] if x["name"] == "track_io_timing")["setting"] = "off"
        report = MODULE.analyze(a, b)
        self.assertNotIn("shared_blk_read_time", report["deltas"]["pgss"][0]["delta"])
        self.assertTrue(any("track_io_timing" in x for x in report["warnings"]))

    def test_zero_calls_and_zero_hit_denominator(self):
        a, b = pair()
        for field in ("calls", "shared_blks_read", "shared_blks_hit"):
            b["sections"]["pgss"]["rows"][0][field] = a["sections"]["pgss"]["rows"][0][field]
        entry = MODULE.analyze(a, b)["deltas"]["pgss"][0]
        self.assertIsNone(entry["mean_exec_ms"])
        self.assertIsNone(entry["shared_hit_ratio"])

    def test_missing_counters_do_not_become_zero_bytes(self):
        a, b = pair()
        for snap in (a, b):
            for field in ("temp_blks_written", "shared_blks_read"):
                del snap["sections"]["pgss"]["rows"][0][field]
        entry = MODULE.analyze(a, b)["deltas"]["pgss"][0]
        self.assertIsNone(entry["temp_written_bytes"])
        self.assertIsNone(entry["shared_read_bytes"])
        self.assertIsNone(entry["shared_hit_ratio"])

    def test_database_reset_and_relation_rewrite(self):
        for scenario in ("reset", "rewrite", "row_reset"):
            a, b = pair()
            a["sections"]["tables"]["rows"] = [{"relid": 10, "relfilenode": 10, "seq_scan": 1, "stats_reset": None}]
            b["sections"]["tables"]["rows"] = [{"relid": 10, "relfilenode": 10, "seq_scan": 5, "stats_reset": None}]
            if scenario == "reset":
                b["sections"]["database"]["rows"][0]["stats_reset"] = "2026-10-26T00:00:30Z"
            elif scenario == "rewrite":
                b["sections"]["tables"]["rows"][0]["relfilenode"] = 11
            else:
                b["sections"]["tables"]["rows"][0]["stats_reset"] = "2026-10-26T00:00:30Z"
            self.assertEqual(MODULE.analyze(a, b)["deltas"]["tables"], [])

    def test_truncated_and_unavailable_sections_are_explicit(self):
        a, b = pair()
        a["sections"]["pgss"]["truncated"] = True
        b["sections"]["io"] = {"status": "error", "reason": "sqlstate_42501", "rows": []}
        report = MODULE.analyze(a, b)
        self.assertTrue(any("truncated" in x for x in report["warnings"]))
        self.assertTrue(any("io: unavailable" in x for x in report["warnings"]))

    def test_gauges_are_not_differenced(self):
        a, b = pair()
        for snap, n in ((a, 100), (b, 10)):
            snap["sections"]["tables"]["rows"] = [{"relid": 1, "relfilenode": 1, "n_dead_tup": n, "seq_scan": 1}]
        report = MODULE.analyze(a, b)
        self.assertNotIn("n_dead_tup", report["deltas"]["tables"][0]["delta"])
        self.assertEqual(report["current"]["tables"][0]["n_dead_tup"], 10)

    def test_json_report_is_finite_and_markdown_has_evidence(self):
        report = MODULE.analyze(*pair())
        json.dumps(report, allow_nan=False)
        markdown = MODULE.render_markdown(report)
        self.assertIn("Ranking value", markdown)
        self.assertNotIn("SENSITIVE_TOKEN", markdown)

    def test_disabled_or_unknown_tracking_suppresses_sql(self):
        for value in ("none", None):
            a, b = pair()
            for snap in (a, b):
                next(x for x in snap["sections"]["settings"]["rows"] if x["name"] == "pg_stat_statements.track")["setting"] = value
            report = MODULE.analyze(a, b)
            self.assertEqual(report["deltas"]["pgss"], [])
            self.assertTrue(any("SQL interval suppressed" in x for x in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
