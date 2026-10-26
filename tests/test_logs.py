import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("analyze_logs", ROOT / "skills/postgres-performance-diagnostics/scripts/analyze_logs.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def records():
    return (ROOT / "tests/fixtures/logs/events.jsonl").read_text(encoding="utf-8").splitlines()


class LogTests(unittest.TestCase):
    def test_event_types_and_malformed_record(self):
        report = MODULE.analyze(records())
        counts = report["summary"]["event_counts"]
        self.assertEqual(counts["deadlock"], 1)
        self.assertEqual(counts["temp_file"], 2)
        self.assertEqual(counts["lock_wait"], 1)
        self.assertEqual(counts["lock_acquired"], 1)
        self.assertEqual(counts["checkpoint_start"], 1)
        self.assertEqual(counts["checkpoint_complete"], 1)
        self.assertEqual(counts["autovacuum"], 1)
        self.assertEqual(report["summary"]["temp_bytes"], 12288)
        self.assertEqual(report["malformed_lines"], 1)

    def test_temp_attribution_never_reuses_previous_pid_query(self):
        report = MODULE.analyze(records())
        temp = [event for event in report["events"] if event["kind"] == "temp_file"]
        self.assertEqual(temp[0]["query_id"], "9223372036854775806")
        self.assertNotIn("query_id", temp[1])
        group = next(group for group in report["query_groups"] if group["query_id"] == "9223372036854775806")
        self.assertEqual(group["temp_bytes"], 8192)

    def test_extended_protocol_phases_are_separate(self):
        phases = MODULE.analyze(records())["summary"]["durations_by_phase"]
        self.assertEqual(set(phases), {"execute", "parse", "bind", "statement", "plan"})
        self.assertEqual(phases["execute"]["observed_count"], 1)
        self.assertEqual(phases["parse"]["total_ms"], 2)
        self.assertNotIn("p95", phases["statement"])

    def test_large_query_id_preserved_exactly(self):
        report = MODULE.analyze(records())
        self.assertIn("9223372036854775806", [group["query_id"] for group in report["query_groups"]])
        self.assertIsNone(MODULE.query_id(0))
        self.assertIsNone(MODULE.query_id(True))
        self.assertEqual(MODULE.query_id(-921), "-921")

    def test_sensitive_output_requires_explicit_option(self):
        public = MODULE.analyze(records())
        self.assertNotIn("TOP_SECRET_PARAMETER", json.dumps(public))
        self.assertNotIn("TOP_SECRET_PARAMETER", MODULE.markdown(public))
        self.assertIn("TOP_SECRET_PARAMETER", json.dumps(MODULE.analyze(records(), include_details=True)))

    def test_event_limit_does_not_truncate_aggregates(self):
        full = MODULE.analyze(records())
        limited = MODULE.analyze(records(), max_events=1)
        self.assertEqual(len(limited["events"]), 1)
        self.assertEqual(limited["summary"], full["summary"])
        self.assertEqual(limited["events_omitted"], len(full["events"]) - 1)

    def test_state_code_works_with_localized_message(self):
        report = MODULE.analyze([json.dumps({"state_code": "40P01", "message": "デッドロックを検出しました"})])
        self.assertEqual(report["summary"]["event_counts"]["deadlock"], 1)

    def test_sql_state_code_compatibility_alias(self):
        report = MODULE.analyze([json.dumps({"sql_state_code": "40P01", "message": "localized"})])
        self.assertEqual(report["summary"]["event_counts"]["deadlock"], 1)

    def test_grouping_keeps_database_and_user_boundaries(self):
        lines = [json.dumps({"query_id": 123, "dbname": db, "user": user, "message": "duration: 1 ms  statement: SELECT 1"})
                 for db, user in [("a", "x"), ("b", "x"), ("a", "y")]]
        self.assertEqual(len(MODULE.analyze(lines)["query_groups"]), 3)

    def test_unknown_json_and_empty_lines_do_not_crash(self):
        report = MODULE.analyze(["", "[]", "null", '{"message":"connection authorized"}', '{"value":NaN}'])
        self.assertEqual(report["blank_lines"], 1)
        self.assertEqual(report["malformed_lines"], 3)
        self.assertEqual(report["unclassified_records"], 1)
        self.assertEqual(report["events"], [])

    def test_missing_duration_phase_is_unknown(self):
        report = MODULE.analyze(['{"message":"duration: 25.1 ms"}'])
        self.assertEqual(report["events"][0]["phase"], "unspecified")
        self.assertEqual(report["events"][0]["selection"], "sampling_or_threshold_unknown")

    def test_positive_event_limit_required(self):
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                MODULE.analyze([], max_events=value)

    def test_cli_json_and_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reports" / "report.json"
            self.assertEqual(MODULE.main([str(ROOT / "tests/fixtures/logs/events.jsonl"), "--output", str(output), "--format", "json"]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["kind"], "jsonlog_analysis")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(MODULE.main([str(Path(directory) / "missing")]), 2)
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
