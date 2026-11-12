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

    def test_entry_created_inside_interval_counts_from_zero(self):
        a, b = pair()
        a["sections"]["pgss"]["rows"] = []
        b["sections"]["pgss"]["rows"][0]["stats_since"] = "2026-10-26T00:00:30+00:00"
        report = MODULE.analyze(a, b)
        entry = report["deltas"]["pgss"][0]
        self.assertEqual(entry["delta"]["calls"], 12)
        self.assertEqual(entry["delta"]["total_exec_time"], 110)
        self.assertTrue(any("created during the interval" in w for w in report["warnings"]))

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

    def test_io_ranking_and_native_bytes_without_operation_size_assumption(self):
        a, b = pair()
        for snap, reads, hits in ((a, 0, 0), (b, 3, 9)):
            snap["sections"]["io"]["rows"] = [{"backend_type": "client backend", "object": "relation",
                "context": "normal", "reads": reads, "read_bytes": reads * 32768,
                "read_time": reads * 2.5, "stats_reset": None}]
            snap["sections"]["index_io"]["rows"] = [{"indexrelid": 1, "relname": "test_table",
                "indexrelname": "test_index", "idx_blks_read": reads, "idx_blks_hit": hits}]
        report = MODULE.analyze(a, b)
        native = report["deltas"]["io"][0]["delta"]
        self.assertEqual(native["read_bytes"], 98304)
        self.assertEqual(native["read_time"], 7.5)
        self.assertEqual(report["rankings"]["sql_shared_read_ms"][0]["ranking_value"], 1)
        index = report["rankings"]["index_reads"][0]
        self.assertEqual(index["shared_read_bytes"], 24576)
        self.assertEqual(index["idx_hit_ratio"], .75)

        del b["sections"]["io"]["rows"][0]["read_bytes"]
        for setting in b["sections"]["settings"]["rows"]:
            if setting["name"] == "track_io_timing":
                setting["setting"] = "off"
        report = MODULE.analyze(a, b)
        self.assertEqual(report["deltas"]["io"][0]["delta"], {"reads": 3})
        self.assertEqual(report["rankings"]["sql_shared_read_ms"], [])

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

    def test_exec_share_and_logical_reads_ranking(self):
        report = MODULE.analyze(*pair())
        entry = report["rankings"]["sql_shared_blks_accessed"][0]
        self.assertEqual(entry["shared_blks_accessed"], 10)
        self.assertEqual(entry["exec_time_share"], 1)

    def test_nested_statements_are_not_in_share_denominator(self):
        a, b = pair()
        for snap in (a, b):
            nested = copy.deepcopy(snap["sections"]["pgss"]["rows"][0])
            nested.update(toplevel=False, queryid=1)
            snap["sections"]["pgss"]["rows"].append(nested)
        shares = {x["toplevel"]: x["exec_time_share"] for x in MODULE.analyze(a, b)["deltas"]["pgss"]}
        self.assertEqual(shares, {True: 1, False: None})

    def test_database_average_active_sessions(self):
        a, b = pair()
        for snap, ms in ((a, 0), (b, 30000)):
            snap["sections"]["database"]["rows"][0].update(active_time=ms, session_time=ms * 4,
                                                           idle_in_transaction_time=ms)
        entry = MODULE.analyze(a, b)["deltas"]["database"][0]
        self.assertEqual(entry["average_active_sessions"], .5)
        self.assertEqual(entry["idle_in_transaction_share"], .25)

    def test_activity_oldest_transaction_first_and_xmin_holders(self):
        a, b = pair()
        b["sections"]["activity"] = {"status": "ok", "rows": [
            {"pid": 1, "xact_start": None, "backend_type": "checkpointer"},
            {"pid": 2, "xact_start": "2026-10-26T00:00:50+00:00", "backend_xmin_age": 10},
            {"pid": 3, "xact_start": "2026-10-25T00:00:00+00:00", "backend_xmin_age": 9000}]}
        b["sections"]["slots"] = {"status": "ok", "rows": [{"slot_name": "s", "catalog_xmin_age": 50000, "xmin_age": None}]}
        b["sections"]["prepared"] = {"status": "ok", "rows": [{"gid": "g", "transaction_age": 100}]}
        current = MODULE.analyze(a, b, top=2)["current"]
        self.assertEqual([x["pid"] for x in current["activity"]], [3, 2])
        self.assertEqual([x["source"] for x in current["xmin_horizon_holders"]],
                         ["replication_slot_catalog_xmin", "session"])

    def test_redundant_index_candidates_respect_constraints(self):
        def index(oid, name, keys, **extra):
            count = len(keys.split())
            row = {"relid": 1, "indexrelid": oid, "indexrelname": name, "access_method": "btree",
                   "indnkeyatts": count, "indnatts": count, "indkey": keys, "indclass": " ".join(["3124"] * count),
                   "indcollation": " ".join(["0"] * count), "indoption": " ".join(["0"] * count),
                   "has_expressions": False, "is_partial": False, "indisvalid": True, "constraint_names": [],
                   "index_bytes": oid * 100}
            row.update(extra)
            return row
        indexes = [index(10, "pk", "1", indisunique=True, indisprimary=True, constraint_names=["pk"]),
                   index(11, "pk_dup", "1"),
                   index(12, "a", "2"), index(13, "a_b", "2 3"), index(14, "a_b_copy", "2 3"),
                   index(15, "c_expr", "4", has_expressions=True), index(16, "c_d", "4 5"),
                   index(17, "e_incl", "6 7", indnkeyatts=1, indclass="3124", indcollation="0", indoption="0"),
                   index(18, "e_f", "6 8"),
                   index(19, "g_desc", "9", indoption="3"), index(20, "g_h", "9 10")]
        found = {(x["index"], x["covered_by"], x["reason"]) for x in MODULE.redundant_indexes(indexes)}
        self.assertEqual(found, {("pk_dup", "pk", "duplicate"), ("a", "a_b", "prefix"),
                                 ("a_b_copy", "a_b", "duplicate")})

    def test_autovacuum_backlog_uses_reloptions_and_flags_partition_parents(self):
        values = {"autovacuum": "on", "track_counts": "on", "autovacuum_vacuum_threshold": "50", "autovacuum_vacuum_scale_factor": "0.2",
                  "autovacuum_vacuum_max_threshold": "100000000", "autovacuum_analyze_threshold": "50",
                  "autovacuum_analyze_scale_factor": "0.1"}
        tables = [{"relname": "hot", "relkind": "r", "reltuples": 1000, "n_dead_tup": 500, "n_mod_since_analyze": 0},
                  {"relname": "tuned", "relkind": "r", "reltuples": 1000, "n_dead_tup": 500, "n_mod_since_analyze": 0,
                   "reloptions": ["autovacuum_vacuum_scale_factor=0.5", "fillfactor=90"]},
                  {"relname": "fresh", "relkind": "r", "reltuples": -1, "n_dead_tup": 10, "n_mod_since_analyze": 0},
                  {"relname": "parent", "relkind": "p", "last_analyze": None},
                  {"relname": "off", "relkind": "r", "reltuples": 0, "n_dead_tup": 100, "n_mod_since_analyze": 0,
                   "reloptions": ["autovacuum_enabled=false"]}]
        found = {x["relname"]: x for x in MODULE.autovacuum_backlog(tables, values)}
        self.assertEqual(set(found), {"hot", "parent", "off"})
        self.assertEqual(found["hot"]["dead_threshold"], 250)
        self.assertEqual(found["hot"]["dead_ratio"], 2)
        self.assertFalse(found["off"]["autovacuum_enabled"])
        self.assertEqual(found["parent"]["reason"], "partitioned_parent_never_analyzed")

    def test_never_scanned_indexes_exclude_constraints_and_invalid_ones(self):
        def index(name, **extra):
            row = {"relid": "1", "relname": "orders", "indexrelname": name, "relkind": "i", "idx_scan": 0,
                   "indisvalid": True, "indisunique": False, "indisprimary": False, "indisexclusion": False,
                   "constraint_names": [], "index_bytes": 8192, "last_idx_scan": None}
            row.update(extra)
            return row
        indexes = [index("never", index_bytes=900000), index("used", idx_scan=7),
                   index("unknown_usage", idx_scan=None), index("pk", indisprimary=True, indisunique=True),
                   index("uniq", indisunique=True), index("fk_or_check", constraint_names=["c"]),
                   index("broken", indisvalid=False), index("small")]
        tables = [{"relid": "1", "n_tup_ins": 10, "n_tup_upd": 20, "n_tup_del": 5}]
        found = MODULE.unused_indexes(indexes, tables, "2026-10-01T00:00:00Z")
        # Largest first; a missing counter is not a zero, and enforcing indexes are never candidates.
        self.assertEqual([x["index"] for x in found], ["never", "small"])
        self.assertEqual(found[0]["table_rows_written"], 35)
        self.assertEqual(found[0]["statistics_since"], "2026-10-01T00:00:00Z")
        # PostgreSQL 19 exposes a per-index reset time; it replaces the database-wide one.
        per_index = MODULE.unused_indexes([index("n", stats_reset="2026-10-20T00:00:00Z")], tables, "2026-10-01T00:00:00Z")
        self.assertEqual(per_index[0]["statistics_since"], "2026-10-20T00:00:00Z")

    def test_invalid_index_reports_a_running_build(self):
        indexes = [{"indexrelid": "10", "indexrelname": "leftover", "indisvalid": False, "indisready": True, "index_bytes": 5},
                   {"indexrelid": "11", "indexrelname": "building", "indisvalid": False, "indisready": False, "index_bytes": 9},
                   {"indexrelid": "12", "indexrelname": "fine", "indisvalid": True, "indisready": True}]
        found = {x["index"]: x for x in MODULE.invalid_indexes(indexes, [{"index_relid": "11"}])}
        self.assertEqual(set(found), {"leftover", "building"})
        self.assertTrue(found["building"]["build_in_progress"])
        self.assertFalse(found["leftover"]["build_in_progress"])

    def test_unindexed_foreign_key_carries_interval_evidence(self):
        keys = [{"relid": "20", "schemaname": "public", "relname": "items", "constraint_name": "items_order_fk",
                 "columns": ["order_id"], "referenced_relid": "10", "referenced_schemaname": "public",
                 "referenced_relname": "orders", "reltuples": 800000, "relpages": 5000, "supporting_index": None},
                {"relid": "21", "schemaname": "public", "relname": "notes", "constraint_name": "notes_order_fk",
                 "columns": ["order_id"], "referenced_relid": "10", "referenced_schemaname": "public",
                 "referenced_relname": "orders", "reltuples": 10, "relpages": 1, "supporting_index": "notes_order_id"}]
        deltas = [{"identity": {"relid": "10"}, "delta": {"n_tup_del": 40, "n_tup_upd": 0}},
                  {"identity": {"relid": "20"}, "delta": {"seq_scan": 40}}]
        found = MODULE.unindexed_foreign_keys(keys, deltas, 8192)
        self.assertEqual(len(found), 1)
        entry = found[0]
        self.assertEqual((entry["constraint"], entry["references"]), ("items_order_fk", "public.orders"))
        self.assertEqual(entry["child_estimated_bytes"], 5000 * 8192)
        self.assertEqual((entry["referenced_deletes_in_interval"], entry["child_seq_scans_in_interval"]), (40, 40))
        # Without table deltas the entry stays, with the interval evidence unknown rather than zero.
        latent = MODULE.unindexed_foreign_keys(keys)[0]
        self.assertIsNone(latent["referenced_deletes_in_interval"])
        self.assertIsNone(latent["child_estimated_bytes"])

    def test_freeze_age_uses_table_limits_and_the_oldest_database(self):
        values = {"autovacuum_freeze_max_age": "200000000", "autovacuum_multixact_freeze_max_age": "400000000",
                  "vacuum_failsafe_age": "1600000000"}
        databases = [{"datname": "app", "is_current": True, "xid_age": 50_000_000, "mxid_age": 10},
                     {"datname": "template0", "is_current": False, "xid_age": 210_000_000, "mxid_age": 0}]
        tables = [{"relname": "young", "xid_age": 1_000_000, "mxid_age": 0},
                  {"relname": "old", "xid_age": 150_000_000, "mxid_age": 0},
                  {"relname": "tuned", "xid_age": 60_000_000, "mxid_age": 0,
                   "reloptions": ["autovacuum_freeze_max_age=100000000", "fillfactor=90"]},
                  {"relname": "partitioned_parent", "xid_age": None}]
        result = MODULE.freeze_age(databases, tables, values)
        self.assertEqual(result["databases"][0]["datname"], "template0")
        self.assertAlmostEqual(result["databases"][0]["xid_age_ratio"], 1.05)
        self.assertEqual(result["databases"][0]["xids_until_wraparound"], 2 ** 31 - 210_000_000)
        self.assertFalse(result["databases"][0]["past_failsafe_age"])
        self.assertEqual([x["relname"] for x in result["tables_near_limit"]], ["old", "tuned"])
        self.assertAlmostEqual(result["tables_near_limit"][1]["xid_age_ratio"], 0.6)
        self.assertEqual(result["closest_table"]["relname"], "old")
        # Missing settings leave ratios unknown; they are not treated as "far from the limit".
        # A table's own reloption is still a known limit.
        unknown = MODULE.freeze_age(databases, tables, {})
        self.assertIsNone(unknown["databases"][0]["xid_age_ratio"])
        self.assertEqual([x["relname"] for x in unknown["tables_near_limit"]], ["tuned"])
        self.assertIsNone(next(x for x in [unknown["closest_table"]] if x)["mxid_age_ratio"])

    def test_new_current_candidates_and_warnings_reach_the_report(self):
        a, b = pair()
        for snap in (a, b):
            snap["sections"]["settings"]["rows"] += [{"name": "autovacuum_freeze_max_age", "setting": "200000000"}]
            snap["sections"]["tables"]["rows"] = [{"relid": "1", "relfilenode": 1, "relname": "orders", "seq_scan": 1,
                                                    "n_tup_del": 0, "xid_age": 250_000_000, "size_skipped_locked": True}]
            snap["sections"]["indexes"]["rows"] = [{"relid": "1", "indexrelid": "9", "relfilenode": 9, "relname": "orders",
                                                     "indexrelname": "idle", "relkind": "i", "idx_scan": 0,
                                                     "indisvalid": True, "index_bytes": 4096}]
        b["sections"]["foreign_keys"] = {"status": "ok", "rows": [
            {"relid": "2", "relname": "items", "constraint_name": "fk", "columns": ["order_id"], "referenced_relid": "1",
             "referenced_schemaname": "public", "referenced_relname": "orders", "relpages": 10, "supporting_index": None}]}
        b["sections"]["database_ages"] = {"status": "ok", "rows": [{"datname": "fixture", "xid_age": 250_000_000,
                                                                     "mxid_age": 1, "is_current": True}]}
        report = MODULE.analyze(a, b)
        current = report["current"]
        self.assertEqual(current["unused_indexes"][0]["index"], "idle")
        self.assertEqual(current["unused_indexes_total"], {"indexes": 1, "bytes": 4096})
        self.assertEqual(current["unindexed_foreign_keys"][0]["referenced_deletes_in_interval"], 0)
        self.assertEqual(current["freeze_age"]["tables_near_limit"][0]["relname"], "orders")
        self.assertTrue(any("past autovacuum_freeze_max_age" in w for w in report["warnings"]))
        self.assertTrue(any("sizes of 1 relation(s) were not read" in w for w in report["warnings"]))
        text = MODULE.render_markdown(report)
        for heading in ("Never-scanned index candidates", "Foreign keys without a supporting index", "Freeze age"):
            self.assertIn(heading, text)

    def test_postgresql_19_only_counters_are_differenced_when_present(self):
        a, b = pair()
        for snap, n in ((a, 0), (b, 1)):
            snap["sections"]["wal"]["rows"] = [{"wal_records": 1000 * n, "wal_fpi": 10 * n, "wal_bytes": 200000 * n,
                                                "wal_fpi_bytes": 60000 * n, "wal_buffers_full": 0, "stats_reset": None}]
            snap["sections"]["subscription_stats"]["rows"] = [{"subid": 7, "apply_error_count": n,
                "sync_table_error_count": 2 * n, "sync_seq_error_count": 0, "confl_update_deleted": 3 * n, "stats_reset": None}]
            snap["sections"]["replication_slots"]["rows"] = [{"slot_name": "s", "spill_bytes": 5 * n,
                                                              "mem_exceeded_count": 4 * n, "stats_reset": None}]
            snap["sections"]["lock_stats"]["rows"] = [{"locktype": "relation", "waits": 2 * n, "wait_time": 900.0 * n,
                                                       "fastpath_exceeded": n, "stats_reset": None}]
            snap["sections"]["checkpointer"]["rows"] = [{"num_timed": 3 * n, "num_requested": n, "stats_reset": None}]
        deltas = MODULE.analyze(a, b)["deltas"]
        self.assertEqual(deltas["wal"][0]["fpi_bytes_share"], 0.3)
        self.assertEqual(deltas["wal"][0]["fpi_bytes_share_upper_bound"], 10 * 8192 / 200000)
        sub = deltas["subscription_stats"][0]["delta"]
        self.assertEqual((sub["sync_table_error_count"], sub["confl_update_deleted"]), (2, 3))
        self.assertNotIn("sync_error_count", sub)  # The PostgreSQL 18 name is simply absent on 19.
        self.assertEqual(deltas["replication_slots"][0]["delta"]["mem_exceeded_count"], 4)
        self.assertEqual(deltas["lock_stats"][0]["mean_wait_ms"], 450)
        self.assertEqual(deltas["checkpointer"][0]["requested_ratio"], 0.25)

    def test_replication_gaps_standby_state_and_builds_are_current_evidence(self):
        a, b = pair()
        # Older snapshots have no recovery_state/create_index_progress sections: not a gap.
        warnings = MODULE.analyze(a, b)["warnings"]
        self.assertFalse(any(w.startswith(("current recovery_state", "current create_index_progress",
                                           "current vacuum_progress")) for w in warnings))
        b["sections"]["replication"] = {"status": "ok", "rows": []}
        b["sections"]["replication"]["rows"] = [{"pid": 41, "application_name": "standby1", "state": "streaming",
            "pending_send_gap_bytes": 0, "sent_write_gap_bytes": 0, "write_flush_gap_bytes": 0,
            "flush_replay_gap_bytes": 8192, "total_replay_gap_bytes": 8192, "client_addr": "10.0.0.9"}]
        b["sections"]["recovery_state"] = {"status": "ok", "rows": [{"in_recovery": False, "receive_lsn": None}]}
        b["sections"]["create_index_progress"] = {"status": "ok", "rows": [{"pid": 42, "relid": 1, "index_relid": 9,
            "command": "CREATE INDEX CONCURRENTLY", "phase": "waiting for old snapshots", "lockers_total": 3,
            "lockers_done": 1, "current_locker_pid": 40}]}
        report = MODULE.analyze(a, b)
        current = report["current"]
        sender = current["replication"][0]
        self.assertEqual((sender["application_name"], sender["flush_replay_gap_bytes"],
                          sender["total_replay_gap_bytes"]), ("standby1", 8192, 8192))
        self.assertNotIn("client_addr", sender)
        self.assertEqual(current["create_index_progress"][0]["current_locker_pid"], 40)
        self.assertFalse(any(w.startswith("current recovery_state") for w in report["warnings"]))
        text = MODULE.render_markdown(report)
        self.assertIn("Index builds running now", text)
        self.assertNotIn("Standby replay state", text)  # A primary has nothing to replay.
        b["sections"]["recovery_state"]["rows"] = [{"in_recovery": True, "receive_replay_gap_bytes": 0,
                                                    "replay_delay_seconds": 12.5, "replay_pause_state": "not paused"}]
        self.assertIn("Standby replay state", MODULE.render_markdown(MODULE.analyze(a, b)))
        # Collected but failed is a gap.
        b["sections"]["recovery_state"] = {"status": "error", "reason": "sqlstate_42501", "rows": []}
        self.assertIn("current recovery_state: missing or truncated evidence", MODULE.analyze(a, b)["warnings"])

    def test_scan_width_and_requested_checkpoint_share(self):
        a, b = pair()
        for snap, n in ((a, 0), (b, 1)):
            snap["sections"]["tables"]["rows"] = [{"relid": "1", "relfilenode": 1, "relname": "orders",
                                                   "seq_scan": 4 * n, "seq_tup_read": 400000 * n, "n_tup_upd": 0}]
            snap["sections"]["checkpointer"]["rows"] = [{"num_timed": 3 * n, "num_requested": n, "stats_reset": None}]
        deltas = MODULE.analyze(a, b)["deltas"]
        self.assertEqual(deltas["tables"][0]["seq_tup_per_scan"], 100000)
        self.assertEqual(deltas["checkpointer"][0]["requested_ratio"], 0.25)

    def test_views_missing_on_this_version_are_not_reported_as_gaps(self):
        a, b = pair()
        for snap in (a, b):
            snap["sections"]["lock_stats"] = {"status": "unavailable", "reason": "missing_relation", "rows": []}
            del snap["sections"]["recovery_prefetch"]
        b["sections"]["bgwriter"] = {"status": "error", "reason": "sqlstate_42501", "rows": []}
        warnings = MODULE.analyze(a, b)["warnings"]
        self.assertFalse(any(w.startswith(("lock_stats", "recovery_prefetch")) for w in warnings))
        self.assertIn("bgwriter: unavailable at one or both endpoints (sqlstate_42501)", warnings)

    def test_markdown_is_a_digest_not_a_dump(self):
        a, b = pair()
        for snap, n in ((a, 0), (b, 1)):
            # A real server has ~80 pg_stat_io rows; nearly all are idle in any interval.
            snap["sections"]["io"]["rows"] = [
                {"backend_type": f"type{i}", "object": "relation", "context": "normal", "reads": 0, "read_bytes": 0,
                 "writes": 0, "hits": 5 * n, "stats_reset": None} for i in range(80)
            ] + [{"backend_type": "client backend", "object": "relation", "context": "normal", "reads": 9 * n,
                  "read_bytes": 9 * 8192 * n, "read_time": 4.0 * n, "writes": 0, "hits": 0, "stats_reset": None}]
        report = MODULE.analyze(a, b)
        text = MODULE.render_markdown(report)
        self.assertNotIn("```json", text)
        self.assertEqual(text.count("| client backend |"), 1)
        self.assertNotIn("type17", text)
        self.assertIn("busiest 1 of 1 rows with I/O", text)
        self.assertLess(len(text.splitlines()), 200)
        self.assertEqual(len(report["deltas"]["io"]), 81)  # The JSON report still has every row.
        self.assertEqual(MODULE.compact({"a": None, "b": [], "c": 0, "d": 0.123456, "e": False}),
                         '{"c": 0, "d": 0.1235, "e": false}')


if __name__ == "__main__":
    unittest.main()
