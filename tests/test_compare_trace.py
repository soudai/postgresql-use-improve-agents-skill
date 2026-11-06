import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/postgres-performance-diagnostics/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMPARE, TRACE, ANALYZE, PLAN, LOGS = (load(n) for n in ("compare_intervals", "analyze_trace", "analyze", "analyze_plan", "analyze_logs"))


def report(calls, exec_ms, rows, hit, read, settings=None, seconds=60):
    delta = {"calls": calls, "total_exec_time": exec_ms, "rows": rows, "shared_blks_hit": hit, "shared_blks_read": read}
    entry = {"identity": {"queryid": 42}, "toplevel": True, "delta": delta,
             "per_second": {k: v / seconds for k, v in delta.items()},
             "shared_hit_ratio": hit / (hit + read) if hit + read else None}
    return {"instance_id": "i", "database": "d", "deltas": {"pgss": [entry]}, "warnings": [],
            "settings": {"after": settings or {"work_mem": "4096"}}}


class CompareTests(unittest.TestCase):
    def test_improvement_with_comparable_load(self):
        result = COMPARE.compare(report(100, 1000, 2000, 900, 100), report(100, 100, 2000, 950, 50))
        item = result["statements"][0]
        self.assertAlmostEqual(item["after_over_before"]["total_exec_time_per_call"], .1)
        self.assertEqual(item["warnings"], [])

    def test_detects_load_cache_rows_and_settings_differences(self):
        result = COMPARE.compare(report(100, 1000, 2000, 100, 900),
                                 report(300, 900, 3000, 990, 10, settings={"work_mem": "65536"}))
        warnings = " ".join(result["statements"][0]["warnings"])
        self.assertIn("load differs", warnings)
        self.assertIn("cache condition may differ", warnings)
        self.assertIn("verify result correctness", warnings)
        self.assertEqual(result["settings_changed"], {"work_mem": ["4096", "65536"]})

    def test_same_query_for_two_roles_is_order_independent(self):
        slow = report(1, 100, 1, 1, 0)
        fast = report(1, 1, 1, 1, 0)
        entries = []
        for userid, source in ((10, slow), (20, fast)):
            row = source["deltas"]["pgss"][0]
            row["identity"].update(dbid=5, userid=userid, toplevel=True)
            entries.append(row)
        before = dict(slow, deltas={"pgss": entries})
        after = dict(slow, deltas={"pgss": list(reversed(entries))})
        compared = COMPARE.compare(before, after)
        self.assertEqual([r["after_over_before"]["total_exec_time_per_call"] for r in compared["statements"]], [1, 1])
        self.assertEqual({r["baseline_identity"] for r in compared["statements"]}, {"5:10:42", "5:20:42"})
        ambiguous = COMPARE.compare(before, after, queryids=["42"])
        self.assertFalse(ambiguous["statements"])
        self.assertTrue(any("ambiguous" in w for w in ambiguous["warnings"]))

    def test_cross_major_requires_mapping(self):
        before = report(1, 100, 1, 1, 0)
        after = report(1, 10, 1, 1, 0)
        before["metadata"] = {"server_version_num": 180006}
        after["metadata"] = {"server_version_num": 190000}
        self.assertEqual(COMPARE.compare(before, after)["statements"], [])
        self.assertEqual(len(COMPARE.compare(before, after, {"42": "42"})["statements"]), 1)

    def test_rewritten_sql_needs_explicit_mapping(self):
        after = report(10, 10, 2000, 1, 0)
        after["deltas"]["pgss"][0]["identity"]["queryid"] = 77
        result = COMPARE.compare(report(100, 1000, 2000, 1, 0), after)
        self.assertEqual(result["statements"], [])
        self.assertEqual(result["only_in_candidate"], ["77"])
        mapped = COMPARE.compare(report(100, 1000, 2000, 1, 0), after, {"42": "77"})
        self.assertEqual(len(mapped["statements"]), 1)
        self.assertIn("Before/after", COMPARE.markdown(mapped))


def lines(records):
    return [json.dumps(r) for r in records]


class TraceTests(unittest.TestCase):
    def test_four_patterns_are_distinguished(self):
        records = []
        for r in range(150):  # Normal high-QPS lookups: one per request.
            records += [{"type": "request", "request_id": f"q{r}", "endpoint": "GET /customer", "duration_ms": 5},
                        {"type": "db", "request_id": f"q{r}", "statement": "customer_by_id", "rows": 1, "rows_used": 1, "duration_ms": 1}]
        records += [{"type": "request", "request_id": "e", "endpoint": "GET /export", "duration_ms": 900},
                    {"type": "db", "request_id": "e", "statement": "export", "rows": 50000, "rows_used": 50000, "duration_ms": 700}]
        records += [{"type": "request", "request_id": "o", "endpoint": "GET /recent", "duration_ms": 80},
                    {"type": "db", "request_id": "o", "statement": "recent_all", "rows": 1000, "rows_used": 20,
                     "duration_ms": 50, "code_location": "recent.py:10"}]
        records += [{"type": "request", "request_id": "n", "endpoint": "GET /orders", "duration_ms": 120, "pool_wait_ms": 30},
                    {"type": "db", "request_id": "n", "statement": "orders_of_customer", "rows": 20, "duration_ms": 2}]
        records[-1]["bytes"] = 200
        records += [{"type": "db", "request_id": "n", "statement": "items_of_order", "rows": 2, "duration_ms": 1,
                     "code_location": "orders.py:42", "bytes": 40} for _ in range(20)]
        result = TRACE.analyze(lines(records))
        labels = {s["statement"]: s["classification"] for s in result["statements"]}
        self.assertEqual(labels["customer_by_id"], "high_frequency_normal")
        self.assertEqual(labels["export"], "large_result")
        self.assertEqual(labels["recent_all"], "over_fetching")
        self.assertEqual(labels["items_of_order"], "n_plus_one")
        n1 = next(c for c in result["candidates"] if c["kind"] == "n_plus_one")
        self.assertEqual((n1["parent_statement"], n1["code_location"]), ("orders_of_customer", "orders.py:42"))
        self.assertTrue(next(c for c in result["candidates"] if c["kind"] == "large_result")["intended"])
        orders = next(e for e in result["endpoints"] if e["endpoint"] == "GET /orders")
        self.assertEqual(orders["sql_per_request_max"], 21)
        self.assertEqual(orders["pool_wait_share"], .25)
        self.assertEqual(orders["bytes_per_request_mean"], 1000)
        self.assertIsNone(next(e for e in result["endpoints"] if e["endpoint"] == "GET /export")["bytes_per_request_mean"])
        self.assertIn("n_plus_one", TRACE.markdown(result))

    def test_pool_wait_share_uses_only_requests_with_pool_measurements(self):
        records = [
            {"type": "request", "request_id": "missing", "endpoint": "A", "duration_ms": 100},
            {"type": "request", "request_id": "unmeasured", "endpoint": "B", "duration_ms": 100},
            {"type": "request", "request_id": "measured", "endpoint": "B", "duration_ms": 20, "pool_wait_ms": 10},
            {"type": "request", "request_id": "zero", "endpoint": "C", "duration_ms": 20, "pool_wait_ms": 0},
        ]
        absent, partial, measured_zero = TRACE.analyze(lines(records))["endpoints"]
        self.assertIsNone(absent["pool_wait_share"])
        self.assertEqual(absent["pool_wait_observed_requests"], 0)
        self.assertEqual(partial["pool_wait_share"], .5)
        self.assertEqual(partial["p95_pool_wait_ms"], 10)
        self.assertEqual(partial["pool_wait_observed_requests"], 1)
        self.assertEqual(measured_zero["pool_wait_share"], 0)

    def test_repetition_without_parent_is_not_called_n_plus_one(self):
        records = [{"type": "db", "request_id": "x", "statement": "poll", "rows": 1, "duration_ms": 1} for _ in range(6)]
        self.assertEqual(TRACE.analyze(lines(records))["candidates"][0]["kind"], "repeated_statement")


class ChainAndPlanTests(unittest.TestCase):
    def test_lock_chain_root_depth_and_cycle(self):
        snap = {"sections": {"activity": {"status": "ok", "rows": [
            {"pid": 1, "state": "idle in transaction", "blocking_pids": []},
            {"pid": 2, "blocking_pids": [1]}, {"pid": 3, "blocking_pids": [2]},
            {"pid": 4, "blocking_pids": [1]}, {"pid": 5, "blocking_pids": [4]},
            {"pid": 8, "blocking_pids": [9]}, {"pid": 9, "blocking_pids": [8]}]}}}
        chains = ANALYZE.lock_chains(snap)
        self.assertEqual((chains[0]["root_pid"], chains[0]["blocked_sessions"], chains[0]["max_depth"]), (1, 4, 2))
        self.assertEqual(chains[0]["state"], "idle in transaction")
        self.assertEqual(chains[-1]["cycle_pids"], [8, 9])

    def test_join_discarding_scanned_rows_and_generic_plan(self):
        raw = {"Plan": {"Node Type": "Hash Join", "Plan Rows": 40, "Actual Rows": 40, "Actual Loops": 1,
                        "Hash Cond": "(i.order_id = o.id)", "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "items", "Plan Rows": 800000, "Actual Rows": 800000, "Actual Loops": 1},
            {"Node Type": "Hash", "Plan Rows": 20, "Actual Rows": 20, "Actual Loops": 1, "Plans": [
                {"Node Type": "Index Scan", "Plan Rows": 20, "Actual Rows": 20, "Actual Loops": 1}]}]}}
        plan = PLAN.analyze(raw)["plans"][0]
        found = [c for c in plan["candidates"] if c["kind"] == "join_discards_scanned_rows"]
        self.assertEqual(found[0]["path"], "Plan/Plans/0")
        self.assertFalse(plan["uses_parameter_placeholders"])
        generic = {"Plan": {"Node Type": "Index Scan", "Index Cond": "(status = $1)", "Plan Rows": 30,
                            "Actual Rows": 270000, "Actual Loops": 1}}
        plan = PLAN.analyze(generic)["plans"][0]
        self.assertTrue(plan["uses_parameter_placeholders"])
        self.assertIn("generic_plan_misestimate", [c["kind"] for c in plan["candidates"]])
        self.assertNotIn("status = $1", json.dumps(plan))

    def test_oom_kill_is_classified(self):
        records = [{"message": "client backend (PID 77) was terminated by signal 9: Killed", "error_severity": "LOG"},
                   {"message": "terminating any other active server processes", "error_severity": "LOG"},
                   {"message": "out of memory", "state_code": "53200", "error_severity": "ERROR"}]
        report = LOGS.analyze([json.dumps(r) for r in records])
        self.assertEqual(report["summary"]["event_counts"], {"backend_killed": 1, "crash_restart": 1, "out_of_memory": 1})
        self.assertEqual((report["events"][0]["process"], report["events"][0]["signal"]), ("client backend", 9))
        self.assertIn("backend_killed", [c["kind"] for c in report["candidates"]])


if __name__ == "__main__":
    unittest.main()
