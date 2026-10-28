#!/usr/bin/env python3
"""Reproduce the problem, normal and insufficient-information cases of issues #3-#12 on real PostgreSQL.

Each run creates NEW disposable, network-isolated containers from a locally available official
postgres:18/19 image, loads synthetic data, drives the skill's own tools and checks their verdicts.
Nothing here is meant for a shared or production server.

Example: python tests/scenarios.py --image postgres:18-bookworm --report scenario-report.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "postgres-performance-diagnostics" / "scripts"
CONFIG_SCRIPT = ROOT / "skills" / "postgres-config-tuning" / "scripts" / "review_config.py"
TASK_LABEL = "postgres-performance-diagnostics"
# Only settings no scenario changes: command-line values override ALTER SYSTEM.
SERVER_ARGS = ["-c", "shared_preload_libraries=pg_stat_statements", "-c", "compute_query_id=on",
               "-c", "logging_collector=on", "-c", "log_destination=jsonlog", "-c", "log_directory=log",
               "-c", "log_lock_waits=on", "-c", "deadlock_timeout=200ms", "-c", "log_temp_files=0"]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ANALYZE = load("analyze", SCRIPTS / "analyze.py")
COMPARE = load("compare_intervals", SCRIPTS / "compare_intervals.py")
PLAN = load("analyze_plan", SCRIPTS / "analyze_plan.py")
LOGS = load("analyze_logs", SCRIPTS / "analyze_logs.py")
TRACE = load("analyze_trace", SCRIPTS / "analyze_trace.py")
CONFIG = load("review_config", CONFIG_SCRIPT)


def run(command, *, sql=None, timeout=300, check=True):
    result = subprocess.run(command, input=sql, text=True, encoding="utf-8", capture_output=True,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command[:6])}: "
                           f"{(result.stderr or result.stdout).strip()[-800:]}")
    return result


class Cluster:
    def __init__(self, image, directory, *, docker_options=(), extra_args=(), tmpfs=True):
        self.image, self.directory, self.tmpfs = image, directory, tmpfs
        self.name = "pgskills-scenario-" + uuid.uuid4().hex[:12]
        self.docker_options, self.extra_args = list(docker_options), list(extra_args)
        self.container_id = None
        self.counter = 0

    def start(self):
        # tmpfs pages count against a container memory limit, so the OOM scenario uses the anonymous volume instead.
        storage = ["--tmpfs", "/var/lib/postgresql"] if self.tmpfs else []
        command = ["docker", "run", "--pull=never", "--rm", "-d", "--name", self.name, "--network", "none",
                   *storage, "--shm-size", "512m", "--label", "codex.task=" + TASK_LABEL,
                   "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=scen", "-e", "PGUSER=postgres",
                   "-e", "PGDATABASE=scen", *self.docker_options, self.image, *SERVER_ARGS, *self.extra_args]
        self.container_id = run(command, timeout=60).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", self.container_id):
            raise RuntimeError("Docker did not return a full container ID")
        deadline = time.monotonic() + 90
        while run(["docker", "exec", self.name, "psql", "-h", "127.0.0.1", "-X", "-qAt", "-c", "SELECT 1"],
                  check=False, timeout=15).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError("container did not become ready")
            time.sleep(0.25)
        return self

    def stop(self):
        if not self.container_id:
            return
        details = json.loads(run(["docker", "inspect", self.container_id], check=False).stdout or "[{}]")[0]
        if details.get("Id") == self.container_id and details.get("Config", {}).get("Labels", {}).get("codex.task") == TASK_LABEL:
            run(["docker", "rm", "-f", self.container_id], check=False, timeout=60)

    def psql(self, sql, *, check=True, timeout=300, user=None, extra=()):
        command = ["docker", "exec", "-i"] + (["-u", user] if user else []) + [self.name, "psql", *extra, "-X", "-qAt",
                                                                            "--no-password", "-v", "ON_ERROR_STOP=1"]
        return run(command, sql=sql, check=check, timeout=timeout).stdout

    def value(self, sql):
        return self.psql(sql).strip()

    def shell(self, script, *, user="postgres", check=True, timeout=300):
        return run(["docker", "exec", "-i", "-u", user, self.name, "bash", "-s"], sql=script, check=check, timeout=timeout)

    def session(self, app):
        return Session(self, app)

    def qid(self, tag):
        value = self.value(f"SELECT queryid FROM pg_stat_statements WHERE query LIKE '%/* {tag} */%' "
                           "AND toplevel ORDER BY calls DESC LIMIT 1")
        return value or None

    def collect(self, psql="psql"):
        self.counter += 1
        path = self.directory / f"{self.name}-{self.counter}.json"
        run([sys.executable, str(SCRIPTS / "collect.py"), "--docker-container", self.name, "--instance-id",
             self.name + ("" if psql == "psql" else "-" + psql), "--psql", psql, "--output", str(path)])
        return json.loads(path.read_text(encoding="utf-8"))

    def interval(self, workload):
        before = self.collect()
        workload()
        time.sleep(0.6)  # pgstat flushes pending counters of finished sessions
        return ANALYZE.analyze(before, self.collect(), top=50)

    def explain(self, sql, settings=""):
        text = self.psql(settings + "\nEXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
        return PLAN.analyze(json.loads(text))["plans"][0]

    def logs(self):
        out = self.shell('cat "$(psql -XAtc "SHOW data_directory")"/log/*.json 2>/dev/null || true').stdout
        return LOGS.analyze(out.splitlines(), max_events=100000)

    def sample(self, samples=4, interval=0.3, psql="psql"):
        self.counter += 1
        path = self.directory / f"{self.name}-samples-{self.counter}.json"
        run([sys.executable, str(SCRIPTS / "sample_activity.py"), "--docker-container", self.name, "--instance-id",
             self.name, "--samples", str(samples), "--interval", str(interval), "--psql", psql, "--output", str(path)])
        return json.loads(path.read_text(encoding="utf-8"))["summary"]


class Session:
    """A long-lived psql session for concurrency scenarios; statements are fed one at a time."""

    def __init__(self, cluster, app):
        self.cluster, self.app = cluster, app
        self.proc = subprocess.Popen(["docker", "exec", "-i", "-e", f"PGAPPNAME={app}", cluster.name, "psql", "-X",
                                      "-qAt", "--no-password"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, text=True)

    def send(self, sql, pause=0.3):
        self.proc.stdin.write(sql.rstrip() + "\n")
        self.proc.stdin.flush()
        time.sleep(pause)

    def flush(self):
        # Open sessions flush counters (e.g. deadlocks) lazily; force it before the next snapshot.
        self.send("SELECT pg_stat_force_next_flush();", 0.1)
        self.send("SELECT 1;", 0.3)

    def pid(self):
        return int(self.cluster.value(f"SELECT pid FROM pg_stat_activity WHERE application_name = '{self.app}' LIMIT 1"))

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


def wait_until(predicate, timeout=30, step=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def blocks(output):
    """Split psql output that uses '\\echo @@name' markers into {name: text}."""
    result, name, lines = {}, None, []
    for line in output.splitlines():
        if line.startswith("@@"):
            if name:
                result[name] = "\n".join(lines)
            name, lines = line[2:].strip(), []
        else:
            lines.append(line)
    if name:
        result[name] = "\n".join(lines)
    return result


def entry(report, qid):
    return next((e for e in report["deltas"]["pgss"] if str(e["identity"]["queryid"]) == str(qid)), None)


def kinds(plan):
    return {c["kind"] for c in plan["candidates"]}


class Scenarios:
    def __init__(self, cluster):
        self.c = cluster
        self.results = {}
        self.issue = None

    def check(self, name, condition, detail=None):
        self.results[self.issue]["checks"].append({"check": name, "ok": bool(condition), "detail": detail})

    def measure(self, **values):
        self.results[self.issue]["measurements"].update(values)

    def begin(self, issue, title):
        self.issue = issue
        self.results[issue] = {"title": title, "checks": [], "measurements": {}}
        print(f"[{issue}] {title}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------------------------------------
    def setup(self):
        self.c.psql("ALTER SYSTEM SET track_io_timing = on; ALTER SYSTEM SET track_wal_io_timing = on; SELECT pg_reload_conf();")
        self.c.psql("""
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pgstattuple;
CREATE EXTENSION IF NOT EXISTS pg_buffercache;
CREATE TABLE customers AS SELECT i AS id, i % 50 AS region, 'c' || i AS name FROM generate_series(1, 20000) i;
ALTER TABLE customers ADD PRIMARY KEY (id);
CREATE TABLE orders AS
SELECT i::bigint AS id, (i % 20000) + 1 AS customer_id,
       CASE WHEN i > 396000 THEN 'failed' WHEN i % 100 = 0 THEN 'pending' ELSE 'done' END AS status,
       timestamptz '2026-10-01 00:00:00+00' - make_interval(secs => i) AS created_at,
       i % 1000 AS amount, md5(i::text) AS note
FROM generate_series(1, 400000) i;
ALTER TABLE orders ADD PRIMARY KEY (id);
CREATE INDEX orders_created_at ON orders (created_at);
CREATE TABLE order_items AS
SELECT o.id AS order_id, ((o.id * 7 + k) % 5000)::int AS sku, k + 1 AS qty FROM orders o, generate_series(0, 1) k;
CREATE TABLE small_codes AS SELECT i AS code, 'label' || i AS label FROM generate_series(1, 20) i;
ALTER TABLE small_codes ADD PRIMARY KEY (code);
VACUUM ANALYZE;
""", timeout=600)

    # ------------------------------------------------------------------------------------------
    def issue3(self):
        self.begin("#3", "slow queries and inefficient scans")
        ids = [i * 613 % 20000 + 1 for i in range(30)]
        lookup = "".join(f"SELECT /* s3_lookup */ id, amount FROM orders WHERE customer_id = {c};\n" for c in ids)
        normal = "SELECT /* s3_codes */ count(*) FROM small_codes;\n" * 60 + \
                 "SELECT /* s3_report */ status, count(*) FROM orders GROUP BY status;\n" * 3
        outputs = []

        def workload():
            outputs.append(self.c.psql(lookup * 2 + normal))

        before = self.c.interval(workload)
        q_lookup, q_codes = self.c.qid("s3_lookup"), self.c.qid("s3_codes")
        top = before["rankings"]["sql_total_exec_ms"][0]
        self.check("slow lookup ranks first by interval total time", str(top["identity"]["queryid"]) == q_lookup,
                   {"top_share": top.get("exec_time_share")})
        self.check("orders ranks first by sequentially read tuples",
                   before["rankings"]["table_seq_tuples"][0]["identity"].get("relname") == "orders")
        parallel = self.c.explain(f"SELECT id, amount FROM orders WHERE customer_id = {ids[0]}")
        filtering = next((c for c in parallel["candidates"] if c["kind"] == "heavy_filtering"), None)
        removed = filtering["evidence"]["approximate_total_removed"] if filtering else 0
        self.check("few rows from a large scan: heavy_filtering with loop-aware total", filtering and abs(removed - 399980) < 20000,
                   {"approximate_total_removed": removed, "loops": filtering and filtering["evidence"]["loops"]})
        codes = self.c.explain("SELECT count(*) FROM small_codes")
        report = self.c.explain("SELECT status, count(*) FROM orders GROUP BY status", "SET max_parallel_workers_per_gather = 0;")
        index_kinds = {"heavy_filtering", "repeated_seq_scan", "join_discards_scanned_rows", "cardinality_mismatch"}
        self.check("small table seq scan: no candidate", not kinds(codes), sorted(kinds(codes)))
        self.check("full-table aggregate: no index candidate", not (kinds(report) & index_kinds), sorted(kinds(report)))
        naive = sum(n["metrics"].get("Actual Total Time", 0) for n in report["nodes"])
        self_sum = sum(h["self_ms_approx"] for h in report["hotspots"])
        self.check("inclusive node times are not summed (self time ~ execution)",
                   naive > report["execution_time_ms"] * 1.1 and self_sum <= report["execution_time_ms"] * 1.05,
                   {"naive_sum": naive, "self_sum": self_sum, "execution": report["execution_time_ms"]})
        self.c.psql("CREATE INDEX orders_customer_id ON orders (customer_id); ANALYZE orders;")
        after = self.c.interval(workload)
        comparison = COMPARE.compare(before, after, queryids=[q_lookup])["statements"][0]
        ratio = comparison["after_over_before"]["total_exec_time_per_call"]
        self.check("same parameters and load after CREATE INDEX: mean time improves", ratio is not None and ratio < 0.5,
                   {"ratio": ratio, "warnings": comparison["warnings"]})
        self.check("comparison has no load/cache/row warnings", not comparison["warnings"], comparison["warnings"])
        digest = [hashlib.sha256("\n".join(sorted(o.splitlines())).encode()).hexdigest() for o in outputs]
        self.check("results identical before/after", digest[0] == digest[1])
        self.measure(lookup_ms_per_call_before=comparison["before"]["total_exec_time_per_call"],
                     lookup_ms_per_call_after=comparison["after"]["total_exec_time_per_call"],
                     unchanged_normal_query=q_codes is not None)

    # ------------------------------------------------------------------------------------------
    def issue4(self):
        self.begin("#4", "hit ratio, cache and I/O")
        ranges = "".join(f"SELECT /* s4_range */ sum(amount) FROM orders WHERE id BETWEEN {s} AND {s + 2000};\n"
                         for s in range(1000, 381000, 19000))
        batch = "SELECT /* s4_batch */ sku, sum(qty) FROM order_items GROUP BY sku;\n"
        const = "SELECT /* s4_const */ now() > timestamptz '2000-01-01 00:00:00+00';\n" * 10
        evict = self.c.value("SELECT count(*) FROM pg_proc WHERE proname = 'pg_buffercache_evict_relation'") == "1"
        self.measure(evict_function_available=evict)
        if evict:
            self.c.psql("SELECT pg_buffercache_evict_relation('orders'); SELECT pg_buffercache_evict_relation('order_items');")
        cold = self.c.interval(lambda: self.c.psql(ranges + batch + const))
        warm = self.c.interval(lambda: self.c.psql(ranges + batch + const))
        q_range, q_batch, q_const = self.c.qid("s4_range"), self.c.qid("s4_batch"), self.c.qid("s4_const")
        comparison = COMPARE.compare(cold, warm, queryids=[q_range])["statements"][0]
        self.check("cold vs warm cache difference is flagged", any("cache condition" in w for w in comparison["warnings"]) or not evict,
                   comparison["warnings"])
        b = entry(cold, q_batch)
        self.check("low hit ratio batch is not read-time bound (read_time_share small)",
                   b and b["shared_hit_ratio"] < 0.5 and b["read_time_share"] is not None and b["read_time_share"] < 0.5,
                   {"hit_ratio": b and b["shared_hit_ratio"], "read_time_share": b and b["read_time_share"]})
        self.check("zero denominator gives no ratio", entry(warm, q_const)["shared_hit_ratio"] is None)
        self.check("PG18+ byte columns detected in pg_stat_io", any("read_bytes" in r for r in self.c.collect()["sections"]["io"]["rows"]))
        self.c.psql("ALTER SYSTEM SET track_io_timing = off; SELECT pg_reload_conf();")
        time.sleep(1)
        off = self.c.interval(lambda: self.c.psql(ranges))
        self.c.psql("ALTER SYSTEM SET track_io_timing = on; SELECT pg_reload_conf();")
        time.sleep(1)
        e = entry(off, q_range)
        self.check("timing off: read time absent, not zero", e and "shared_blk_read_time" not in e["delta"] and e["read_time_share"] is None
                   and any("track_io_timing" in w for w in off["warnings"]))
        reset = self.c.interval(lambda: self.c.psql(
            "SELECT pg_stat_statements_reset(); SELECT pg_stat_reset_single_table_counters('orders'::regclass);" + ranges))
        self.check("statement reset suppresses SQL interval", not reset["deltas"]["pgss"]
                   and any("pgss: reset marker" in w for w in reset["warnings"]))
        self.check("table counter reset excludes the table",
                   all(d["identity"].get("relname") != "orders" for d in reset["deltas"]["tables"]),
                   [w for w in reset["warnings"] if w.startswith("tables")])
        self.check("limitations state OS metrics are needed for physical I/O",
                   any("physical disk read" in x for x in cold["limitations"]))
        self.measure(range_hit_cold=comparison["before"]["shared_hit_ratio"], range_hit_warm=comparison["after"]["shared_hit_ratio"],
                     batch_read_time_share=b and b["read_time_share"],
                     note="tmpfs storage: real I/O latency cannot be reproduced; read_time_share is the discriminating metric")

    # ------------------------------------------------------------------------------------------
    def issue5(self):
        self.begin("#5", "temp spill of sorts and hashes")
        sort = "SELECT /* s5_sort */ note FROM orders ORDER BY note LIMIT 1 OFFSET 399000"
        hashj = "SELECT /* s5_hash */ count(*) FROM orders o JOIN order_items i ON i.order_id = o.id"
        agg = "SELECT /* s5_agg */ count(*) FROM (SELECT note, count(*) FROM orders GROUP BY note) s"
        par = "SELECT /* s5_par */ note FROM orders ORDER BY note, id LIMIT 1 OFFSET 399001"
        small = "SET work_mem = '256kB'; SET max_parallel_workers_per_gather = 0; SET enable_mergejoin = off; SET enable_sort = on;"
        parallel = ("SET work_mem = '256kB'; SET max_parallel_workers_per_gather = 2; SET parallel_setup_cost = 0; "
                    "SET parallel_tuple_cost = 0; SET min_parallel_table_scan_size = 0;")
        nolog = "SET log_temp_files = -1; SET work_mem = '256kB'; SELECT /* s5_nolog */ note FROM orders ORDER BY note DESC LIMIT 1 OFFSET 399002;"
        spill = self.c.interval(lambda: self.c.psql(f"{small}\n{sort};\n{hashj};\nSET enable_sort = off;\n{agg};\n{parallel}\n{par};\n{nolog}"))
        q = {t: self.c.qid(t) for t in ("s5_sort", "s5_hash", "s5_agg", "s5_par", "s5_nolog")}
        ranked = {str(e["identity"]["queryid"]) for e in spill["rankings"]["sql_temp_written_bytes"]}
        self.check("spilling SQL ranked by temp bytes", {q["s5_sort"], q["s5_hash"], q["s5_agg"]} <= ranked)
        groups = {g["query_id"]: g for g in self.c.logs()["query_groups"]}
        self.check("log temp files attributed to the same query IDs",
                   all(groups.get(q[t], {}).get("event_counts", {}).get("temp_file") for t in ("s5_sort", "s5_hash")))
        self.check("log coverage gap: spill visible in statistics but not in log",
                   entry(spill, q["s5_nolog"])["temp_written_bytes"] and q["s5_nolog"] not in groups)
        plans = {"sort": self.c.explain(sort, small),
                 "hash": self.c.explain(hashj, small), "agg": self.c.explain(agg, small + "SET enable_sort = off;"),
                 "parallel": self.c.explain(par, parallel)}
        self.check("sort spill identified in plan", "sort_spill" in kinds(plans["sort"]))
        self.check("hash join spill identified (batches > 1)", "hash_batches" in kinds(plans["hash"]))
        self.check("hash aggregate spill identified", kinds(plans["agg"]) & {"hash_batches", "aggregate_disk_usage"})
        self.check("parallel worker spill located under Workers",
                   any("/Workers/" in c["path"] for c in plans["parallel"]["candidates"] if c["kind"] == "sort_spill"),
                   [c["path"] for c in plans["parallel"]["candidates"]])
        db = spill["deltas"]["database"][0]
        self.check("database-wide temp is visible but not attributable alone", db["delta"].get("temp_bytes", 0) > 0)
        fixed = self.c.interval(lambda: self.c.psql(small.replace("256kB", "64MB") + f"\n{hashj};"))
        comparison = COMPARE.compare(spill, fixed, queryids=[q["s5_hash"]])["statements"][0]
        self.check("scoped work_mem removes the hash spill", comparison["after"]["temp_blks_written_per_call"] == 0,
                   comparison["after_over_before"])
        snapshot = self.c.collect()
        review = CONFIG.review(CONFIG.load_settings(snapshot), argparse.Namespace(
            ram_gb=8, cpus=2, storage="ssd", workload="mixed", max_active_connections=20, managed=False), spill)
        wm = next((f for f in review["findings"] if f["parameter"] == "work_mem"), None)
        self.check("memory budget is part of the recommendation", wm and wm["scope"] == "role/session" and wm["evidence"].get("estimated_safe_cap"),
                   wm)
        self.measure(hash_ms_per_call_256kB=comparison["before"]["total_exec_time_per_call"],
                     hash_ms_per_call_64MB=comparison["after"]["total_exec_time_per_call"],
                     hash_temp_blocks_per_call_before=comparison["before"]["temp_blks_written_per_call"])

    # ------------------------------------------------------------------------------------------
    def issue6(self):
        self.begin("#6", "index fit for JOIN, ORDER BY LIMIT and redundant indexes")
        latest = "SELECT /* s6_latest */ id FROM orders WHERE status = '{}' ORDER BY created_at DESC LIMIT 10"
        values = ("done", "pending", "failed")
        before = {v: self.c.explain(latest.format(v), "SET max_parallel_workers_per_gather = 0;") for v in values}
        self.check("ORDER BY LIMIT walking the wrong index is detected for the skewed value",
                   "heavy_filtering" in kinds(before["failed"]), sorted(kinds(before["failed"])))
        self.check("common value with the same plan is not flagged", "heavy_filtering" not in kinds(before["done"]))
        join = "SELECT /* s6_join */ sum(i.qty) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.customer_id = 77"
        join_before = self.c.explain(join)
        self.check("JOIN scanning the whole inner table is detected", "join_discards_scanned_rows" in kinds(join_before),
                   sorted(kinds(join_before)))
        pk = self.c.explain("SELECT * FROM orders WHERE id = 12345")
        export = self.c.explain("SELECT customer_id, sum(amount) FROM orders GROUP BY customer_id ORDER BY 2 DESC")
        index_kinds = {"heavy_filtering", "join_discards_scanned_rows", "repeated_seq_scan"}
        self.check("existing index is sufficient: no candidate", not kinds(pk))
        self.check("bulk retrieval with sort is not an index problem", not (kinds(export) & index_kinds), sorted(kinds(export)))
        insert = "INSERT /* s6_insert */ INTO order_items SELECT g, 1, 1 FROM generate_series(1, 200) g;\n" * 30
        writes_before = self.c.interval(lambda: self.c.psql(insert))
        self.c.psql("CREATE INDEX orders_status_created ON orders (status, created_at); "
                    "CREATE INDEX order_items_order_id ON order_items (order_id); ANALYZE orders; ANALYZE order_items;")
        writes_after = self.c.interval(lambda: self.c.psql(insert))
        q_insert = self.c.qid("s6_insert")
        wal = COMPARE.compare(writes_before, writes_after, queryids=[q_insert])["statements"][0]["after_over_before"]["wal_bytes_per_call"]
        self.check("added index raises write cost (WAL per INSERT)", wal and wal > 1.2, wal)
        after = {v: self.c.explain(latest.format(v), "SET max_parallel_workers_per_gather = 0;") for v in values}
        join_after = self.c.explain(join)
        times = {v: [before[v]["execution_time_ms"], after[v]["execution_time_ms"]] for v in values}
        self.check("composite index fixes the skewed value", times["failed"][1] < times["failed"][0] / 5, times)
        self.check("other representative values do not regress", all(t[1] <= t[0] * 3 + 1 for t in times.values()), times)
        self.check("join key index removes the full scan", "join_discards_scanned_rows" not in kinds(join_after)
                   and join_after["execution_time_ms"] < join_before["execution_time_ms"],
                   [join_before["execution_time_ms"], join_after["execution_time_ms"]])
        self.c.psql("CREATE INDEX orders_status ON orders (status);")
        snapshot = self.c.collect()
        redundant = ANALYZE.redundant_indexes(snapshot["sections"]["indexes"]["rows"])
        names = {(r["index"], r["covered_by"]) for r in redundant}
        self.check("prefix-redundant index found; constraint index not flagged",
                   ("orders_status", "orders_status_created") in names and all(r["index"] != "orders_pkey" for r in redundant), sorted(names))
        sizes = {r["indexrelname"]: r.get("index_bytes") for r in snapshot["sections"]["indexes"]["rows"]}
        self.c.psql("DROP INDEX orders_status;")
        self.measure(latest_ms_by_value=times, join_ms=[join_before["execution_time_ms"], join_after["execution_time_ms"]],
                     insert_wal_ratio=wal, index_bytes={k: sizes.get(k) for k in ("orders_status_created", "order_items_order_id")})

    # ------------------------------------------------------------------------------------------
    def issue7(self):
        self.begin("#7", "large results, high-frequency SQL and N+1 with application traces")
        customers = [i * 37 % 20000 + 1 for i in range(20)]
        parents = {c: [int(x.split("|")[0]) for x in self.c.psql(f"SELECT id, customer_id FROM orders WHERE customer_id = {c} ORDER BY id").split()]
                   for c in customers}
        steps = []  # (request_id, endpoint, statement tag, sql, rows_used, code_location)
        for r in range(150):
            steps.append((f"cust{r}", "GET /customer", "s7_customer",
                          f"SELECT /* s7_customer */ name FROM customers WHERE id = {r * 97 % 20000 + 1}", None, "customer.py:12"))
        steps.append(("export", "GET /export", "s7_export",
                      "SELECT /* s7_export */ id, amount FROM orders WHERE created_at > timestamptz '2026-09-30 12:00:00+00'", "all", "export.py:30"))
        for r in range(10):
            steps.append((f"recent{r}", "GET /recent", "s7_recent",
                          "SELECT /* s7_recent */ * FROM orders ORDER BY created_at DESC LIMIT 1000", 20, "recent.py:8"))

        def n_plus_one(batch):
            items = []
            for c in customers:
                rid = f"orders{c}"
                items.append((rid, "GET /orders", "s7_orders", f"SELECT /* s7_orders */ id FROM orders WHERE customer_id = {c} ORDER BY id", None, "orders.py:20"))
                if batch:
                    ids = ",".join(map(str, parents[c]))
                    items.append((rid, "GET /orders", "s7_items_batch", "SELECT /* s7_items_batch */ order_id, sku, qty FROM order_items "
                                  f"WHERE order_id = ANY('{{{ids}}}'::bigint[]) ORDER BY order_id, sku, qty", None, "orders.py:24"))
                else:
                    items += [(rid, "GET /orders", "s7_items", f"SELECT /* s7_items */ {o} AS order_id, sku, qty FROM order_items "
                               f"WHERE order_id = {o} ORDER BY sku, qty", None, "orders.py:42") for o in parents[c]]
            return items

        def execute(plan):
            script = "\\timing on\n" + "".join(f"\\echo @@{i}\n{s[3]};\n" for i, s in enumerate(plan))
            spans, requests = [], {}
            for key, text in blocks(self.c.psql(script, timeout=600)).items():
                step = plan[int(key)]
                lines = text.splitlines()
                timing = next((float(m.group(1)) for m in map(re.compile(r"^Time: ([0-9.]+) ms").match, lines) if m), 0.0)
                rows = [l for l in lines if l and not l.startswith("Time: ")]
                used = len(rows) if step[4] == "all" else step[4]
                spans.append({"type": "db", "request_id": step[0], "statement": step[2], "rows": len(rows),
                              "rows_used": used, "duration_ms": timing, "code_location": step[5],
                              "bytes": len("\n".join(rows).encode()), "_rows": rows})
                requests[step[0]] = requests.get(step[0], {"type": "request", "request_id": step[0], "endpoint": step[1], "duration_ms": 1.0})
                requests[step[0]]["duration_ms"] += timing
            return spans, list(requests.values())

        holder = {}
        before = self.c.interval(lambda: holder.update(base=execute(steps + n_plus_one(False))))
        spans, requests = holder["base"]
        trace = TRACE.analyze(json.dumps({k: v for k, v in r.items() if k != "_rows"}) for r in requests + spans)
        labels = {s["statement"]: s["classification"] for s in trace["statements"]}
        self.check("four patterns distinguished from the trace", labels.get("s7_customer") == "high_frequency_normal"
                   and labels.get("s7_export") == "large_result" and labels.get("s7_recent") == "over_fetching"
                   and labels.get("s7_items") == "n_plus_one", labels)
        n1 = next(c for c in trace["candidates"] if c["kind"] == "n_plus_one")
        self.check("call site and parent statement reported", n1["code_location"] == "orders.py:42" and n1["parent_statement"] == "s7_orders")
        calls = {str(e["identity"]["queryid"]): e for e in before["rankings"]["sql_calls"]}
        self.check("statistics alone: high calls for both normal and N+1, with the N+1 caveat",
                   self.c.qid("s7_customer") in calls and self.c.qid("s7_items") in calls
                   and any("Calls alone do not prove N+1" in x for x in before["limitations"]))
        after = self.c.interval(lambda: holder.update(fixed=execute(n_plus_one(True))))
        fixed_spans, fixed_requests = holder["fixed"]
        fixed_trace = TRACE.analyze(json.dumps({k: v for k, v in r.items() if k != "_rows"}) for r in fixed_requests + fixed_spans)
        endpoint = lambda t: next(e for e in t["endpoints"] if e["endpoint"] == "GET /orders")
        a, b = endpoint(trace), endpoint(fixed_trace)
        self.check("SQL per request drops after batching", a["sql_per_request_mean"] == 21 and b["sql_per_request_mean"] == 2,
                   [a["sql_per_request_mean"], b["sql_per_request_mean"]])
        old_rows = sorted(r for s in spans if s["statement"] == "s7_items" for r in s["_rows"])
        new_rows = sorted(r for s in fixed_spans if s["statement"] == "s7_items_batch" for r in s["_rows"])
        self.check("batched result equals the N+1 results", old_rows == new_rows and len(old_rows) >= 800, len(old_rows))
        self.check("transfer volume per request does not grow", b["bytes_per_request_mean"] <= a["bytes_per_request_mean"] * 1.2,
                   [a["bytes_per_request_mean"], b["bytes_per_request_mean"]])
        db_ms = lambda report, tags: sum(entry(report, self.c.qid(t))["delta"]["total_exec_time"] for t in tags)
        self.measure(orders_endpoint_p95_ms=[a["p95_ms"], b["p95_ms"]],
                     bytes_per_request=[a["bytes_per_request_mean"], b["bytes_per_request_mean"]],
                     orders_db_exec_ms=[db_ms(before, ["s7_orders", "s7_items"]), db_ms(after, ["s7_orders", "s7_items_batch"])],
                     sql_per_request=[a["sql_per_request_mean"], b["sql_per_request_mean"]])

    # ------------------------------------------------------------------------------------------
    def issue8(self):
        self.begin("#8", "long transactions, lock chains and deadlocks")
        names = ["s8_a", "s8_b", "s8_c", "s8_d", "s8_e"]
        sessions = {n: self.c.session(n) for n in names}
        try:
            baseline = self.c.collect()
            sessions["s8_a"].send("BEGIN; UPDATE orders SET amount = amount WHERE id = 1;")
            sessions["s8_b"].send("BEGIN; UPDATE orders SET amount = amount WHERE id = 2; UPDATE orders SET amount = amount WHERE id = 1;")
            sessions["s8_c"].send("BEGIN; UPDATE orders SET amount = amount WHERE id = 2;")
            sessions["s8_d"].send("ALTER TABLE orders ADD COLUMN s8_tmp int;")
            sessions["s8_e"].send("SELECT count(*) FROM orders WHERE id = 3;")
            waiting = wait_until(lambda: self.c.value("SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 's8_%' "
                                                      "AND wait_event_type = 'Lock'") == "4", 20)
            pids = {n: s.pid() for n, s in sessions.items()}
            during = self.c.collect()
            report = ANALYZE.analyze(baseline, during)
            chain = report["current"]["lock_chains"][0] if report["current"]["lock_chains"] else {}
            self.check("four sessions waiting", waiting)
            self.check("root blocker is the idle-in-transaction session", chain.get("root_pid") == pids["s8_a"]
                       and chain.get("state") == "idle in transaction", chain)
            self.check("chain depth >= 2 and all waiters counted", chain.get("max_depth", 0) >= 2 and chain.get("blocked_sessions", 0) >= 4, chain)
            e_row = next(r for r in during["sections"]["activity"]["rows"] if r["pid"] == pids["s8_e"])
            self.check("query queued behind waiting DDL is attributed to the DDL", pids["s8_d"] in (e_row.get("blocking_pids") or []),
                       e_row.get("blocking_pids"))
            samples = self.c.sample(3, 0.3)
            self.check("sampling names the root blocker", samples["lock_blockers"] and
                       any(b["pid"] == pids["s8_a"] for b in samples["lock_blockers"]), samples["lock_blockers"])
            self.check("idle in transaction is visible in samples", samples["idle_in_transaction"]["session_samples"] > 0)
        finally:
            self.c.psql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name LIKE 's8_%';")
            for s in sessions.values():
                s.close()
        after_cleanup = self.c.collect()
        logs = self.c.logs()
        self.check("after resolution: no current chain, waits only in the log",
                   not ANALYZE.lock_chains(after_cleanup) and logs["summary"]["event_counts"].get("lock_wait", 0) > 0)

        d1, d2 = self.c.session("s8_d1"), self.c.session("s8_d2")

        def opposite():
            for _ in range(3):
                d1.send("BEGIN; UPDATE orders SET amount = amount WHERE id = 10;", 0.2)
                d2.send("BEGIN; UPDATE orders SET amount = amount WHERE id = 11;", 0.2)
                d1.send("UPDATE orders SET amount = amount WHERE id = 11;", 0.2)
                d2.send("UPDATE orders SET amount = amount WHERE id = 10;", 1.0)
                d1.send("ROLLBACK;", 0.1)
                d2.send("ROLLBACK;", 0.3)
            d1.flush()
            d2.flush()

        def ordered():
            for _ in range(3):
                d1.send("BEGIN; UPDATE orders SET amount = amount WHERE id = 10;", 0.2)
                d2.send("BEGIN; UPDATE orders SET amount = amount WHERE id = 10;", 0.2)
                d1.send("UPDATE orders SET amount = amount WHERE id = 11; COMMIT;", 0.3)
                d2.send("UPDATE orders SET amount = amount WHERE id = 11; COMMIT;", 0.3)
            d1.flush()
            d2.flush()

        def phase(workload):
            """Interval report plus log-derived waits and wall time per transaction round for one workload."""
            seen = max((e["line"] for e in self.c.logs()["events"]), default=0)
            started = time.monotonic()
            report = self.c.interval(workload)
            events = [e for e in self.c.logs()["events"] if e["line"] > seen]
            waits = [e["wait_ms"] for e in events if e["kind"] == "lock_acquired"]
            return report, {"lock_waits_logged": len(waits), "lock_wait_ms_total": sum(waits),
                            "deadlocks_logged": sum(e["kind"] == "deadlock" for e in events),
                            "round_seconds_incl_collection": (time.monotonic() - started) / 3}

        try:
            bad, bad_waits = phase(opposite)
            good, good_waits = phase(ordered)
        finally:
            d1.close()
            d2.close()
        self.check("consistent order turns deadlocks into short logged waits",
                   good_waits["deadlocks_logged"] == 0 and good_waits["lock_waits_logged"] >= 1, [bad_waits, good_waits])
        self.measure(waits=[bad_waits, good_waits],
                     rollbacks=[bad["deltas"]["database"][0]["delta"].get("xact_rollback"),
                                good["deltas"]["database"][0]["delta"].get("xact_rollback")])
        deadlocks = lambda r: r["deltas"]["database"][0]["delta"].get("deadlocks")
        self.check("deadlock reproduced 3 times; consistent order removes it", deadlocks(bad) == 3 and deadlocks(good) == 0,
                   [deadlocks(bad), deadlocks(good)])
        logged = [e for e in self.c.logs()["events"] if e["kind"] == "deadlock"]
        self.check("deadlocks reconstructed from the log with SQLSTATE", len(logged) >= 3 and all(e.get("state_code") == "40P01" for e in logged))
        self.measure(deadlocks_per_round=[deadlocks(bad) / 3, deadlocks(good) / 3])

    # ------------------------------------------------------------------------------------------
    def issue9(self):
        self.begin("#9", "estimation errors, stale statistics and generic plan regression")
        serial = "SET max_parallel_workers_per_gather = 0;"
        self.c.psql("CREATE TABLE s9_addr AS SELECT i AS id, (i % 200) / 4 AS city, i % 200 AS zip FROM generate_series(1, 400000) i;"
                    "ANALYZE s9_addr;")
        corr = "SELECT count(*) FROM s9_addr WHERE city = 5 AND zip = 21"
        before = self.c.explain(corr, serial)
        self.c.psql("CREATE STATISTICS s9_dep (dependencies) ON city, zip FROM s9_addr; ANALYZE s9_addr;")
        after = self.c.explain(corr, serial)
        self.check("correlated columns: mismatch fixed by extended statistics",
                   "cardinality_mismatch" in kinds(before) and "cardinality_mismatch" not in kinds(after),
                   [sorted(kinds(before)), sorted(kinds(after))])
        self.c.psql("CREATE TABLE s9_events (kind int, payload int) WITH (autovacuum_enabled = false);"
                    "INSERT INTO s9_events SELECT i % 10, i FROM generate_series(1, 100000) i; ANALYZE s9_events;"
                    "INSERT INTO s9_events SELECT 99, i FROM generate_series(1, 100000) i;")
        stale = self.c.explain("SELECT count(*) FROM s9_events WHERE kind = 99", serial)
        backlog = {b["relname"]: b for b in ANALYZE.autovacuum_backlog(self.c.collect()["sections"]["tables"]["rows"],
                                                                       {r["name"]: r["setting"] for r in self.c.collect()["sections"]["settings"]["rows"]})}
        self.c.psql("ANALYZE s9_events;")
        fresh = self.c.explain("SELECT count(*) FROM s9_events WHERE kind = 99", serial)
        self.check("stale statistics: mismatch and analyze backlog, fixed by ANALYZE",
                   "cardinality_mismatch" in kinds(stale) and backlog.get("s9_events", {}).get("analyze_ratio", 0) > 1
                   and "cardinality_mismatch" not in kinds(fresh), backlog.get("s9_events"))
        # A job queue: 99% done, four rare states that the application polls far more often than 'done'.
        self.c.psql("CREATE TABLE s9_jobs AS SELECT i AS id, CASE i % 400 WHEN 0 THEN 'pending' WHEN 1 THEN 'running' "
                    "WHEN 2 THEN 'failed' WHEN 3 THEN 'queued' ELSE 'done' END AS status, md5(i::text) AS payload "
                    "FROM generate_series(1, 300000) i; CREATE INDEX s9_jobs_status ON s9_jobs (status); ANALYZE s9_jobs;")
        values = {"done": 0.1, "pending": 0.3, "running": 0.2, "failed": 0.1, "queued": 0.3}
        script = [serial, "PREPARE s9q(text) AS SELECT count(*), max(payload) FROM s9_jobs WHERE status = $1;"]
        for mode in ("force_generic_plan", "force_custom_plan"):
            script.append(f"SET plan_cache_mode = {mode};")
            script += [f"\\echo @@{mode}:{v}\nEXPLAIN (ANALYZE, FORMAT JSON) EXECUTE s9q('{v}');" for v in values]
        script.append("SET plan_cache_mode = auto;")
        script += ["\\echo @@warmup"] + ["EXECUTE s9q('pending');"] * 6 + ["\\echo @@auto:pending\nEXPLAIN (ANALYZE, FORMAT JSON) EXECUTE s9q('pending');"]
        plans = {k: PLAN.analyze(json.loads(v))["plans"][0] for k, v in blocks(self.c.psql("\n".join(script))).items() if ":" in k}
        generic_rare = plans["force_generic_plan:pending"]
        self.check("generic plan regression detected for a rare value",
                   "generic_plan_misestimate" in kinds(generic_rare) and generic_rare["uses_parameter_placeholders"], sorted(kinds(generic_rare)))
        self.check("custom plans for rare and frequent values have no misestimate",
                   not any("cardinality_mismatch" in kinds(plans[f"force_custom_plan:{v}"]) for v in values))
        weighted = {m: sum(w * plans[f"{m}:{v}"]["execution_time_ms"] for v, w in values.items())
                    for m in ("force_generic_plan", "force_custom_plan")}
        # Recorded, not asserted: in this dataset the time impact is small and can be within noise on PG18.
        generic_calls = self.c.value("SELECT count(*) FROM information_schema.columns WHERE table_name = 'pg_stat_statements' "
                                     "AND column_name = 'generic_plan_calls'") == "1"
        self.measure(weighted_ms=weighted, auto_mode_used_generic=plans["auto:pending"]["uses_parameter_placeholders"],
                     pgss_has_generic_plan_calls=generic_calls,
                     per_value_ms={k: v["execution_time_ms"] for k, v in plans.items()})

    # ------------------------------------------------------------------------------------------
    def issue10(self):
        self.begin("#10", "autovacuum backlog, retention, reusable space, HOT and heap fetches")
        table = "(id int PRIMARY KEY, counter int, pad text) WITH (autovacuum_enabled = false)"
        fill = "SELECT i, 0, repeat('x', 80) FROM generate_series(1, 50000) i"
        self.c.psql(f"CREATE TABLE s10_busy {table}; INSERT INTO s10_busy {fill}; VACUUM ANALYZE s10_busy;"
                    "UPDATE s10_busy SET counter = counter + 1; UPDATE s10_busy SET counter = counter + 1; UPDATE s10_busy SET counter = counter + 1;")
        snap = self.c.collect()
        settings = {r["name"]: r["setting"] for r in snap["sections"]["settings"]["rows"]}
        busy = next((b for b in ANALYZE.autovacuum_backlog(snap["sections"]["tables"]["rows"], settings) if b["relname"] == "s10_busy"), None)
        old = [h for h in ANALYZE.xmin_horizon_holders(snap) if h["age"] > 1000]
        size_before = int(self.c.value("SELECT pg_relation_size('s10_busy')"))
        started = time.monotonic()
        self.c.psql("VACUUM s10_busy;")
        self.measure(busy_vacuum_seconds=time.monotonic() - started)
        time.sleep(0.6)
        self.measure(busy_bytes_before_after_vacuum=[size_before, int(self.c.value("SELECT pg_relation_size('s10_busy')"))])
        dead = lambda name: int(self.c.value(f"SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname = '{name}'"))
        self.check("high update without vacuum: backlog, no old holder, cleared by VACUUM",
                   busy and busy["dead_ratio"] > 1 and not old and dead("s10_busy") < 1000, {"backlog": busy, "old_holders": old})
        holder = self.c.session("s10_holder")
        try:
            holder.send("BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM small_codes;", 0.5)
            self.c.psql(f"CREATE TABLE s10_held {table}; INSERT INTO s10_held {fill};"
                        "UPDATE s10_held SET counter = counter + 1; UPDATE s10_held SET counter = counter + 1; VACUUM s10_held;")
            time.sleep(0.6)
            held = self.c.collect()
            holders = ANALYZE.xmin_horizon_holders(held)
            remaining = dead("s10_held")
            self.check("old snapshot: VACUUM cannot remove, holder identified",
                       remaining > 50000 and holders and holders[0]["pid"] == holder.pid(), {"dead_after_vacuum": remaining, "top": holders[:1]})
        finally:
            holder.close()
        self.c.psql("VACUUM s10_held;")
        time.sleep(0.6)
        self.check("after the holder ends VACUUM removes the rows", dead("s10_held") < 1000)
        self.c.psql("CREATE TABLE s10_space (id int, pad text) WITH (autovacuum_enabled = false);"
                    "INSERT INTO s10_space SELECT i, repeat('y', 100) FROM generate_series(1, 100000) i;"
                    "DELETE FROM s10_space WHERE id % 2 = 0; VACUUM s10_space;")
        size1 = int(self.c.value("SELECT pg_relation_size('s10_space')"))
        free = float(self.c.value("SELECT free_percent FROM pgstattuple('s10_space')"))
        self.c.psql("INSERT INTO s10_space SELECT i, repeat('y', 100) FROM generate_series(100001, 150000) i;")
        size2 = int(self.c.value("SELECT pg_relation_size('s10_space')"))
        self.check("reusable free space: file not shrunk but reused by inserts", free > 30 and size2 <= size1 * 1.05,
                   {"free_percent": free, "size_before_insert": size1, "size_after_insert": size2})
        self.c.psql("CREATE TABLE s10_ff100 (id int PRIMARY KEY, counter int, pad text) WITH (autovacuum_enabled = false, fillfactor = 100);"
                    "CREATE TABLE s10_ff70 (id int PRIMARY KEY, counter int, pad text) WITH (autovacuum_enabled = false, fillfactor = 70);"
                    f"INSERT INTO s10_ff100 {fill}; INSERT INTO s10_ff70 {fill}; VACUUM ANALYZE s10_ff100; VACUUM ANALYZE s10_ff70;")
        hot = self.c.interval(lambda: self.c.psql("UPDATE s10_ff100 SET counter = counter + 1; UPDATE s10_ff70 SET counter = counter + 1;"))
        ratios = {d["identity"]["relname"]: d["hot_update_ratio"] for d in hot["deltas"]["tables"] if d["identity"].get("relname", "").startswith("s10_ff")}
        ff70, ff100 = ratios.get("s10_ff70"), ratios.get("s10_ff100")
        self.check("fillfactor headroom raises the HOT ratio", ff70 is not None and ff100 is not None and ff70 > ff100 + 0.3, ratios)
        self.c.psql("CREATE TABLE s10_ios (id int, v int) WITH (autovacuum_enabled = false);"
                    "INSERT INTO s10_ios SELECT i, i FROM generate_series(1, 100000) i; CREATE INDEX s10_ios_v ON s10_ios (v, id); ANALYZE s10_ios;")
        force = "SET enable_seqscan = off; SET enable_bitmapscan = off; SET max_parallel_workers_per_gather = 0;"
        ios = "SELECT count(id) FROM s10_ios WHERE v < 50000"
        before = self.c.explain(ios, force)
        again = self.c.explain(ios, force)
        self.c.psql("VACUUM s10_ios;")
        after = self.c.explain(ios, force)
        fetches = [next(n["metrics"].get("Heap Fetches") for n in p["nodes"] if n["node_type"] == "Index Only Scan")
                   for p in (before, again, after)]
        if int(self.c.value("SHOW server_version_num")) >= 190000:
            # Observed on 19rc1: index-only scans set visibility-map bits on access, so fetches vanish without VACUUM.
            self.check("PG19: heap fetches only on the first scan, cleared on access", fetches[0] > 0 and fetches[1] == 0
                       and fetches[2] == 0, fetches)
        else:
            self.check("heap fetches flagged before VACUUM and gone after", "index_only_heap_fetches" in kinds(again)
                       and "index_only_heap_fetches" not in kinds(after), {"heap_fetches": fetches})
        self.measure(hot_ratio=ratios, ios_heap_fetches=fetches, ios_ms=[before["execution_time_ms"], after["execution_time_ms"]],
                     space={"free_percent": free, "bytes": [size1, size2]})

    # ------------------------------------------------------------------------------------------
    def issue11(self):
        self.begin("#11", "WAL, checkpoints, replication delay and retention")
        self.c.shell("pg_basebackup -h /var/run/postgresql -D /var/lib/postgresql/standby -R -C -S s11_slot -X stream -c fast && "
                     "pg_ctl -D /var/lib/postgresql/standby -o '-p 5433 -k /tmp -c logging_collector=off -c cluster_name=s11standby' "
                     "-l /tmp/standby.log -w start", timeout=300)
        run(["docker", "exec", "-i", "-u", "root", self.c.name, "sh", "-c",
             "cat > /usr/local/bin/psql-standby && chmod 755 /usr/local/bin/psql-standby"], sql='#!/bin/sh\nexec psql -h /tmp -p 5433 "$@"\n')
        streaming = wait_until(lambda: self.c.value("SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming'") == "1", 60)
        self.check("standby streaming", streaming)
        p0 = self.c.collect()
        standby = lambda sql: run(["docker", "exec", "-i", self.c.name, "psql-standby", "-X", "-qAt"], sql=sql).stdout.strip()
        standby("SELECT pg_wal_replay_pause();")
        self.c.psql("ALTER SYSTEM SET synchronous_standby_names = 's11standby'; SELECT pg_reload_conf();")
        time.sleep(1)
        sync = self.c.session("s11_sync")
        sync.send("SET synchronous_commit = remote_apply; UPDATE orders SET amount = amount WHERE id = 5;", 0.5)
        samples = self.c.sample(4, 0.3)
        self.check("synchronous commit wait visible as IPC:SyncRep", any(w["wait"] == "IPC:SyncRep" for w in samples["wait_profile"]),
                   samples["wait_profile"])
        self.c.psql("ALTER SYSTEM RESET synchronous_standby_names; SELECT pg_reload_conf();")
        time.sleep(1)
        sync.close()
        self.c.psql("UPDATE orders SET amount = amount + 1 WHERE id <= 50000;")
        time.sleep(1)
        p1, s1 = self.c.collect(), self.c.collect("psql-standby")
        repl = p1["sections"]["replication"]["rows"][0]
        receiver = s1["sections"]["receiver"]["rows"][0] if s1["sections"]["receiver"]["rows"] else {}
        self.check("replay delay located after flush (flush->replay gap)", repl["flush_replay_gap_bytes"] > 1_000_000
                   and repl["sent_write_gap_bytes"] < 1_000_000, {k: repl[k] for k in ("sent_write_gap_bytes", "write_flush_gap_bytes", "flush_replay_gap_bytes")})
        self.check("standby snapshot: role, receiver LSN and timestamps", s1["metadata"]["in_recovery"] and receiver.get("flushed_lsn")
                   and s1["started_at"] and p1["started_at"])
        try:
            ANALYZE.analyze(p0, s1)
            rejected = False
        except ValueError:
            rejected = True
        self.check("primary and standby snapshots are never differenced", rejected)
        self.check("version/column capabilities recorded on both", p1["metadata"]["server_version_num"] == s1["metadata"]["server_version_num"]
                   and s1["capabilities"])
        standby("SELECT pg_wal_replay_resume();")
        wait_until(lambda: self.c.value("SELECT coalesce(pg_wal_lsn_diff(flush_lsn, replay_lsn), 1) FROM pg_stat_replication") == "0", 60)
        active_retained = next(r for r in self.c.collect()["sections"]["slots"]["rows"] if r["slot_name"] == "s11_slot")["retained_wal_bytes"]

        def commit_ms(mode):
            script = f"SET synchronous_commit = {mode};\n\\timing on\n" + "UPDATE orders SET amount = amount WHERE id = 6;\n" * 20
            times = [float(m.group(1)) for m in re.finditer(r"Time: ([0-9.]+) ms", self.c.psql(script))]
            return sorted(times)[len(times) // 2]

        self.c.psql("ALTER SYSTEM SET synchronous_standby_names = 's11standby'; SELECT pg_reload_conf();")
        time.sleep(1)
        try:
            commits = {mode: commit_ms(mode) for mode in ("local", "on", "remote_apply")}
        finally:
            self.c.psql("ALTER SYSTEM RESET synchronous_standby_names; SELECT pg_reload_conf();")
            time.sleep(1)
        self.check("commit response grows with the synchronous level (median of 20)",
                   commits["remote_apply"] >= commits["local"], commits)
        self.measure(commit_median_ms=commits)
        self.c.shell("pg_ctl -D /var/lib/postgresql/standby -m fast -w stop")
        self.c.psql("UPDATE orders SET amount = amount + 1 WHERE id <= 100000; CHECKPOINT;")
        slot = next(r for r in self.c.collect()["sections"]["slots"]["rows"] if r["slot_name"] == "s11_slot")
        self.check("stopped standby: inactive slot retains WAL", slot["active"] is False and slot["retained_wal_bytes"] > 10_000_000,
                   {"active_retained": active_retained, "inactive_retained": slot["retained_wal_bytes"]})
        self.c.psql("SELECT pg_drop_replication_slot('s11_slot');")
        bulk = "UPDATE /* s11_bulk */ orders SET amount = amount + 1;"
        self.c.psql("ALTER SYSTEM SET max_wal_size = '32MB'; SELECT pg_reload_conf();")
        time.sleep(1)
        small = self.c.interval(lambda: self.c.psql(bulk))
        self.c.psql("ALTER SYSTEM RESET max_wal_size; SELECT pg_reload_conf();")
        time.sleep(1)
        self.c.psql("CHECKPOINT;")
        large = self.c.interval(lambda: self.c.psql(bulk))
        cp = lambda r: r["deltas"]["checkpointer"][0]["delta"]
        wal = lambda r: r["deltas"]["wal"][0]["delta"]["wal_bytes"]
        self.check("checkpoint excess (requested) vs WAL volume with timed checkpoints",
                   cp(small)["num_requested"] >= 2 and cp(large)["num_requested"] == 0, [cp(small), cp(large)])
        self.check("the WAL-heavy SQL is identified", str(large["rankings"]["sql_wal_bytes"][0]["identity"]["queryid"]) == self.c.qid("s11_bulk"))
        self.measure(wal_bytes=[wal(small), wal(large)], fpi=[small["deltas"]["wal"][0]["delta"]["wal_fpi"], large["deltas"]["wal"][0]["delta"]["wal_fpi"]],
                     slot_retained=[active_retained, slot["retained_wal_bytes"]], not_reproduced="archive failure, logical decoding spill")

    # ------------------------------------------------------------------------------------------
    def issue12(self):
        self.begin("#12", "CPU saturation, pool waits, parallel workers and memory")
        job = ("SET max_parallel_workers_per_gather = 0; SELECT /* s12_cpu */ count(*) FROM orders o CROSS JOIN generate_series(1, 6) k "
               "WHERE md5(o.note || k) LIKE '00%';")

        def jobs(concurrency, count=6):
            spans, lock, started = [], threading.Lock(), time.monotonic()
            queue = list(range(count))

            def worker():
                while True:
                    with lock:
                        if not queue:
                            return
                        n = queue.pop(0)
                    begin = time.monotonic()
                    self.c.psql(job)
                    with lock:
                        spans.append({"type": "request", "request_id": f"job{n}", "endpoint": "POST /report",
                                      "pool_wait_ms": (begin - started) * 1000, "duration_ms": (time.monotonic() - started) * 1000})
            threads = [threading.Thread(target=worker) for _ in range(concurrency)]
            for t in threads:
                t.start()
            return threads, spans, started

        holder = {}

        def saturated():
            threads, spans, started = jobs(6)
            time.sleep(1.0)
            # OS-side CPU must be read while the load runs; docker stats samples for about a second.
            stats = threading.Thread(target=lambda: holder.update(cpu=run(
                ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", self.c.name]).stdout.strip()))
            stats.start()
            holder["samples"] = self.c.sample(6, 0.4)
            stats.join()
            for t in threads:
                t.join()
            holder["unlimited"] = (spans, time.monotonic() - started)

        def pooled():
            threads, spans, started = jobs(2)
            for t in threads:
                t.join()
            holder["pooled"] = (spans, time.monotonic() - started)

        unlimited = self.c.interval(saturated)
        limited = self.c.interval(pooled)
        samples = holder["samples"]
        cpu_share = next((w["share"] for w in samples["wait_profile"] if w["wait"] == "CPU (no wait event)"), 0)
        match = re.match(r"[0-9.]+", holder.get("cpu", ""))
        cpu_percent = float(match.group()) if match else 0.0
        self.check("CPU saturation: active sessions exceed CPUs and run without waits",
                   samples["average_active_sessions"] >= 4 and cpu_share >= 0.7, {"aas": samples["average_active_sessions"],
                                                                                    "cpu_share": cpu_share})
        self.check("OS confirms saturation of the 2-CPU limit", cpu_percent >= 150, holder["cpu"])
        q = self.c.qid("s12_cpu")
        comparison = COMPARE.compare(unlimited, limited, queryids=[q])["statements"][0]
        ratio = comparison["after_over_before"]["total_exec_time_per_call"]
        throughput = [6 / holder["unlimited"][1], 6 / holder["pooled"][1]]
        self.check("pool of 2 lowers DB time per query at similar throughput", ratio < 0.8 and throughput[1] > throughput[0] * 0.7,
                   {"exec_ratio": ratio, "jobs_per_s": throughput})
        pooled_trace = TRACE.analyze(json.dumps(s) for s in holder["pooled"][0])
        self.check("pool wait is visible only in application timing", pooled_trace["endpoints"][0]["pool_wait_share"] > 0.2,
                   pooled_trace["endpoints"][0])
        self.c.psql("ALTER SYSTEM SET max_parallel_workers = 2; SELECT pg_reload_conf();")
        time.sleep(1)
        par = ("SET parallel_setup_cost = 0; SET parallel_tuple_cost = 0; SET max_parallel_workers_per_gather = 2; "
               "SELECT /* s12_par */ count(*) FROM orders o CROSS JOIN generate_series(1, 3) k WHERE md5(o.note || k) LIKE '00%';")
        holder2 = {}

        def contended():
            background = [threading.Thread(target=self.c.psql, args=(par,)) for _ in range(2)]
            for t in background:
                t.start()
            time.sleep(0.3)
            holder2["plan"] = self.c.explain("SELECT count(*) FROM orders o CROSS JOIN generate_series(1, 3) k WHERE md5(o.note || k) LIKE '00%'",
                                             "SET parallel_setup_cost = 0; SET parallel_tuple_cost = 0; SET max_parallel_workers_per_gather = 2;")
            for t in background:
                t.join()

        contention = self.c.interval(contended)
        self.c.psql("ALTER SYSTEM RESET max_parallel_workers; SELECT pg_reload_conf();")
        e = entry(contention, self.c.qid("s12_par")) or {"delta": {}}
        planned, launched = e["delta"].get("parallel_workers_to_launch"), e["delta"].get("parallel_workers_launched")
        self.check("parallel worker shortfall detected", "parallel_worker_shortfall" in kinds(holder2["plan"])
                   or (planned is not None and launched is not None and launched < planned),
                   {"plan": sorted(kinds(holder2["plan"])), "pgss_to_launch": planned, "pgss_launched": launched})
        self.measure(aas=samples["average_active_sessions"], docker_cpu=holder["cpu"], exec_ratio=ratio, jobs_per_second=throughput,
                     not_reproduced="storage I/O latency (tmpfs)")


def oom_scenario(image, directory, results):
    """Separate small container: an oversized work_mem gets a backend OOM-killed; a budgeted value spills instead."""
    print("[#12] memory pressure (OOM)", file=sys.stderr, flush=True)
    cluster = Cluster(image, directory, docker_options=["--memory", "640m", "--memory-swap", "640m"], tmpfs=False)
    checks = results["#12"]["checks"]
    try:
        cluster.start()
        query = ("SET max_parallel_workers_per_gather = 0; SET hash_mem_multiplier = 1; SET work_mem = '{}'; "
                 "SELECT count(*) FROM (SELECT g, count(*) FROM generate_series(1, 25000000) g GROUP BY g) s;")
        failed = cluster.psql(query.format("4GB"), check=False, timeout=600)
        time.sleep(3)
        events = cluster.shell("cat /sys/fs/cgroup/memory.events 2>/dev/null || true", user="root").stdout
        oom_kills = int(re.search(r"oom_kill (\d+)", events).group(1)) if "oom_kill" in events else None
        wait_until(lambda: run(["docker", "exec", cluster.name, "psql", "-h", "127.0.0.1", "-X", "-qAt", "-c", "SELECT 1"],
                               check=False).returncode == 0, 60)
        logs = cluster.logs()
        counts = logs["summary"]["event_counts"]
        checks.append({"check": "oversized work_mem: backend OOM-killed, classified from the log",
                       "ok": bool(counts.get("backend_killed") or counts.get("out_of_memory")),
                       "detail": {"log_events": counts, "cgroup_oom_kill": oom_kills}})
        fine = cluster.psql(query.format("64MB"), check=False, timeout=600)
        checks.append({"check": "budgeted work_mem completes (spilling to temp)", "ok": fine.strip().endswith("25000000"),
                       "detail": fine.strip()[-40:]})
        results["#12"]["measurements"]["oom"] = {"cgroup_oom_kill": oom_kills, "log_events": counts}
    finally:
        cluster.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="postgres:18-bookworm")
    parser.add_argument("--report", type=Path, help="Write the JSON evidence report here")
    parser.add_argument("--only", nargs="*", help="Issue numbers to run, e.g. 3 5 8 (setup always runs)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"(?:docker\.io/library/)?postgres:(?:18|19)[A-Za-z0-9_.-]*", args.image):
        parser.error("--image must be an official postgres:18... or postgres:19... tag")
    selected = set(args.only or [str(n) for n in range(3, 13)])
    with tempfile.TemporaryDirectory(prefix="pgskills-scenarios-") as scratch:
        directory = Path(scratch)
        cluster = Cluster(args.image, directory, docker_options=["--cpus", "2"])
        scenarios = Scenarios(cluster)
        failure = None
        try:
            cluster.start()
            version = cluster.value("SELECT version()")
            scenarios.setup()
            for number in range(3, 13):
                if str(number) in selected:
                    try:
                        getattr(scenarios, f"issue{number}")()
                    except Exception as exc:  # Record and continue so one scenario does not hide the others.
                        scenarios.check("scenario completed", False, traceback.format_exc(limit=-2))
        except Exception as exc:
            failure = exc
        finally:
            cluster.stop()
        if failure:
            print(f"scenarios aborted: {failure}", file=sys.stderr)
            return 1
        if "12" in selected:
            try:
                oom_scenario(args.image, directory, scenarios.results)
            except Exception as exc:
                scenarios.results["#12"]["checks"].append({"check": "OOM scenario completed", "ok": False, "detail": str(exc)})
    report = {"image": args.image, "server_version": version, "results": scenarios.results}
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    failed = 0
    for issue, result in scenarios.results.items():
        bad = [c for c in result["checks"] if not c["ok"]]
        failed += len(bad)
        print(f"{issue} {result['title']}: {len(result['checks']) - len(bad)}/{len(result['checks'])} passed")
        for c in bad:
            print(f"  FAIL {c['check']}: {json.dumps(c['detail'], ensure_ascii=False, default=str)[:600]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
