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

    def test_checkpoint_causes_and_timings_in_18_and_19_formats(self):
        # Messages as logged by PostgreSQL 18.6 and 19rc1; 19 repeats the flags after "complete:".
        v18 = ("checkpoint complete: wrote 4614 buffers (28.2%), wrote 1 SLRU buffers; 0 WAL file(s) added, 0 removed, "
               "4 recycled; write=78.730 s, sync=0.001 s, total=78.731 s; sync files=31, longest=0.001 s, "
               "average=0.001 s; distance=58057 kB, estimate=58057 kB; lsn=0/79B7E18, redo lsn=0/79B7DC0")
        v19 = ("checkpoint complete: fast force wait: wrote 3637 buffers (22.2%), wrote 1 SLRU buffers; 0 WAL file(s) "
               "added, 1 removed, 4 recycled; write=0.013 s, sync=0.001 s, total=0.014 s; sync files=31, "
               "longest=0.002 s, average=0.001 s; distance=66864 kB, estimate=66864 kB; lsn=0/079B7E18, redo lsn=0/079B7DC0")
        messages = ["checkpoint starting: time", v18, "checkpoint starting: wal", "checkpoint starting: wal time",
                    "checkpoint starting: immediate force wait", "checkpoint starting: fast force wait", v19,
                    "checkpoint starting: shutdown immediate", "restartpoint starting: time",
                    "checkpoints are occurring too frequently (9 seconds apart)"]
        report = MODULE.analyze([json.dumps({"timestamp": f"2026-11-12 04:0{i}:00.000 UTC", "message": m})
                                 for i, m in enumerate(messages)])
        stats = report["checkpoints"]
        self.assertEqual(stats["by_cause"], {"forced": 2, "shutdown": 1, "time": 1, "wal": 2})
        self.assertEqual((stats["started"], stats["completed"]), (6, 2))
        self.assertAlmostEqual(stats["write_s_total"], 78.743)
        self.assertEqual((stats["total_s_max"], stats["longest_sync_s_max"], stats["distance_kb_max"]), (78.731, 0.002, 66864))
        self.assertEqual((stats["buffers_written_total"], stats["wal_files_removed"], stats["wal_files_recycled"]), (8251, 1, 8))
        self.assertEqual(report["restartpoints"]["by_cause"], {"time": 1})
        complete = [e for e in report["events"] if e["kind"] == "checkpoint_complete"]
        self.assertNotIn("cause", complete[0])               # 18 does not repeat why it started.
        self.assertEqual(complete[1]["cause"], "forced")
        kinds = {c["kind"]: c["observed_events"] for c in report["candidates"]}
        # Only "wal" points at max_wal_size; a CHECKPOINT command or a timeout does not.
        self.assertEqual((kinds["checkpoint_by_wal_volume"], kinds["checkpoint_too_frequent"]), (2, 1))
        self.assertEqual((report["first_timestamp"], report["last_timestamp"]),
                         ("2026-11-12 04:00:00.000 UTC", "2026-11-12 04:09:00.000 UTC"))

    def test_autovacuum_runs_are_summarized_per_table(self):
        def vacuum(table, removed, dead, elapsed, prefix="automatic vacuum of table"):
            return (f'{prefix} "{table}": index scans: 1\n'
                    "pages: 0 removed, 2778 remain, 2778 scanned (100.00% of total), 0 eagerly scanned\n"
                    f"tuples: {removed} removed, 200000 remain, {dead} are dead but not yet removable\n"
                    "removable cutoff: 860, which was 12345 XIDs old when operation ended\n"
                    "WAL usage: 8877 records, 4 full page images, 1301290 bytes, 0 buffers full\n"
                    f"system usage: CPU: user: 0.02 s, system: 0.00 s, elapsed: {elapsed} s")
        messages = [vacuum("shop.public.orders", 66667, 0, 0.12),
                    vacuum("shop.public.orders", 10, 50000, 2.5),
                    vacuum("shop.public.items", 5, 0, 9.0, "automatic aggressive vacuum to prevent wraparound of table"),
                    'automatic analyze of table "shop.public.orders"\nI/O timings: read: 1.087 ms, write: 0.012 ms\n'
                    "system usage: CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.40 s"]
        report = MODULE.analyze([json.dumps({"message": m, "backend_type": "autovacuum worker"}) for m in messages])
        tables = {t["table"]: t for t in report["autovacuum"]["tables"]}
        orders = tables["shop.public.orders"]
        self.assertEqual((orders["vacuum_runs"], orders["analyze_runs"]), (2, 1))
        self.assertEqual((orders["tuples_removed_total"], orders["dead_not_removable_max"]), (66677, 50000))
        self.assertEqual((orders["runs_with_dead_not_removable"], orders["cutoff_xids_old_max"]), (1, 12345))
        self.assertAlmostEqual(orders["elapsed_s_total"], 3.02)
        self.assertEqual((tables["shop.public.items"]["aggressive_runs"], tables["shop.public.items"]["wraparound_runs"]), (1, 1))
        self.assertEqual(report["autovacuum"]["tables"][0]["table"], "shop.public.items")  # Longest first.
        event = report["events"][1]
        self.assertEqual((event["dead_not_removable"], event["elapsed_s"], event["wal_bytes"]), (50000, 2.5, 1301290))
        self.assertIn("autovacuum_dead_not_removable", [c["kind"] for c in report["candidates"]])
        self.assertIn("| shop.public.orders | 2 | 1 |", MODULE.markdown(report))

    def test_timeouts_and_limits_are_classified_by_sqlstate(self):
        records = [
            {"state_code": "57014", "error_severity": "ERROR", "message": "canceling statement due to statement timeout"},
            {"state_code": "57014", "error_severity": "ERROR", "message": "ステートメントのタイムアウトのためステートメントをキャンセルしています"},
            {"state_code": "57014", "error_severity": "ERROR", "message": "canceling autovacuum task",
             "backend_type": "autovacuum worker"},
            {"state_code": "55P03", "error_severity": "ERROR", "message": "canceling statement due to lock timeout"},
            {"state_code": "25P03", "error_severity": "FATAL", "message": "terminating connection due to idle-in-transaction timeout"},
            {"state_code": "25P04", "error_severity": "FATAL", "message": "terminating connection due to transaction timeout"},
            {"state_code": "53300", "error_severity": "FATAL", "message": "sorry, too many clients already"},
            {"state_code": "53100", "error_severity": "ERROR", "message": "could not extend file: No space left on device"},
            {"state_code": "40001", "error_severity": "ERROR", "message": "canceling statement due to conflict with recovery"},
            {"state_code": "40001", "error_severity": "ERROR", "message": "could not serialize access due to concurrent update"},
            {"state_code": "23505", "error_severity": "ERROR", "message": "duplicate key value violates unique constraint \"k\"",
             "detail": "Key (id)=(TOP_SECRET_PARAMETER) already exists."},
            {"state_code": "00000", "error_severity": "LOG", "message": "connection authorized: user=app"},
        ]
        report = MODULE.analyze([json.dumps(r, ensure_ascii=False) for r in records])
        self.assertEqual(report["summary"]["event_counts"], {
            "autovacuum_canceled": 1, "disk_full": 1, "idle_in_transaction_timeout": 1, "lock_not_available": 1,
            "recovery_conflict": 1, "serialization_failure": 1, "statement_canceled": 2, "too_many_connections": 1,
            "transaction_timeout": 1})
        causes = [e.get("cause") for e in report["events"] if e["kind"] == "statement_canceled"]
        self.assertEqual(causes, ["statement_timeout", "unknown"])  # The localized text still counts as a cancel.
        self.assertEqual(report["error_states"]["57014"], 3)
        self.assertEqual(report["error_states"]["23505"], 1)       # Counted, although not a performance event.
        self.assertNotIn("00000", report["error_states"])
        self.assertEqual(report["unclassified_records"], 2)
        self.assertNotIn("TOP_SECRET_PARAMETER", json.dumps(report))

    def test_lock_wait_names_mode_target_and_holders(self):
        record = {"message": "process 1016 still waiting for ShareLock on transaction 686 after 200.089 ms",
                  "detail": "Process holding the lock: 1009. Wait queue: 1016, 1020.", "pid": 1016}
        relation = {"message": "process 1306 acquired AccessShareLock on relation 16463 of database 5 after 1200.5 ms"}
        events = MODULE.analyze([json.dumps(record), json.dumps(relation)])["events"]
        self.assertEqual((events[0]["lock_mode"], events[0]["lock_target"]), ("ShareLock", "transaction"))
        self.assertEqual((events[0]["holder_pids"], events[0]["wait_queue_pids"]), ([1009], [1016, 1020]))
        self.assertEqual((events[1]["kind"], events[1]["lock_target"], events[1]["wait_ms"]), ("lock_acquired", "relation", 1200.5))
        self.assertNotIn("holder_pids", events[1])

    def test_auto_explain_plans_are_extracted_only_on_request(self):
        def plan(ms, text="SELECT 'TOP_SECRET_PARAMETER'"):
            body = json.dumps({"Query Text": text, "Plan": {"Node Type": "Seq Scan", "Relation Name": "orders",
                                                            "Actual Total Time": ms, "Actual Loops": 1}}, indent=2)
            return {"message": f"duration: {ms} ms  plan:\n{body}", "query_id": 77, "timestamp": "t"}
        records = [plan(5.0), plan(900.0), plan(40.0),
                   {"message": "duration: 12.5 ms  plan:\nQuery Text: SELECT 1\nSeq Scan on orders  (cost=0.00..1.01 rows=1)"},
                   {"message": "duration: 1.0 ms  plan:\n{broken"}]
        lines = [json.dumps(r) for r in records]
        quiet = MODULE.analyze(lines)
        self.assertEqual(quiet["auto_explain_plans"], {"json": 4, "text": 1})
        self.assertNotIn("plans", quiet)
        self.assertNotIn("TOP_SECRET_PARAMETER", json.dumps(quiet))
        kept = MODULE.analyze(lines, keep_plans=2)["plans"]
        self.assertEqual([(p["line"], p["duration_ms"]) for p in kept], [(2, 900.0), (3, 40.0)])  # Slowest first.
        self.assertEqual(kept[0]["plan"]["Plan"]["Relation Name"], "orders")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "postgresql.json"
            source.write_text("\n".join(lines) + "\n", encoding="utf-8")
            plans = Path(directory) / "plans"
            output = Path(directory) / "report.json"
            arguments = [str(source), "--format", "json", "--output", str(output), "--extract-plans", str(plans),
                         "--max-plans", "1"]
            self.assertEqual(MODULE.main(arguments), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertNotIn("TOP_SECRET_PARAMETER", json.dumps(report))
            self.assertEqual([p["line"] for p in report["extracted_plans"]], [2])
            saved = json.loads((plans / "plan-line2.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["capture"]["logged_duration_ms"], 900.0)
            # The saved file is what analyze_plan.py reads, including the logged duration.
            spec = importlib.util.spec_from_file_location("analyze_plan_for_logs", ROOT / "skills/postgres-performance-diagnostics/scripts/analyze_plan.py")
            plan_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plan_module)
            analyzed = plan_module.analyze(saved)["plans"][0]
            self.assertEqual((analyzed["time_basis"], analyzed["logged_duration_ms"]), ("logged_duration", 900.0))
            # An existing plan file is never overwritten.
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(MODULE.main(arguments), 2)

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
