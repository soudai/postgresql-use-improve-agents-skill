#!/usr/bin/env python3
"""Review PostgreSQL settings against host resources and observed evidence; never applies changes."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

UNITS = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
         "8kB": 8192, "16kB": 16384, "32kB": 32768, "64kB": 65536, "16MB": 16 * 1024 ** 2,
         "us": 1e-6, "ms": 1e-3, "s": 1, "min": 60, "h": 3600, "d": 86400}
GB = 1024 ** 3
MB = 1024 ** 2
SEVERITY = ("critical", "high", "medium", "low", "info")
LIMITATIONS = [
    "Recommendations are starting points derived from rules of thumb and the supplied evidence, not measured optima.",
    "Memory rules use --ram-gb as memory available to PostgreSQL; other processes, page cache and containers change the budget.",
    "Managed services may hide, rename or fix parameters and use their own formulas; follow the provider's parameter group.",
    "Planner cost settings change many plans at once; compare representative plans before and after.",
    "Evidence is only as good as the interval report: short or idle windows make checkpoint and temp findings unreliable.",
]


def load_settings(data: dict) -> dict:
    if data.get("kind") == "pg_settings_export":
        rows = data.get("settings") or []
    elif isinstance(data.get("sections"), dict):  # collect.py snapshot from postgres-performance-diagnostics
        rows = data["sections"].get("settings", {}).get("rows", [])
    else:
        raise ValueError("expected a pg_settings export (sql/settings.sql) or a diagnostics snapshot")
    result = {row["name"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)}
    if not result:
        raise ValueError("no settings found")
    return result


def raw(settings: dict, name: str):
    row = settings.get(name)
    return row.get("setting") if row else None


def number(settings: dict, name: str):
    """Numeric value in base units (bytes or seconds); None when absent or not numeric."""
    row = settings.get(name)
    if not row:
        return None
    try:
        value = float(row["setting"])
    except (KeyError, TypeError, ValueError):
        return None
    if value < 0:
        return value  # -1 style sentinels keep their meaning
    return value * UNITS.get(row.get("unit") or "", 1)


def size(value: float) -> str:
    if value >= GB and value % GB == 0:
        return f"{int(value // GB)}GB"
    if value >= MB:
        return f"{int(value // MB)}MB"
    return f"{max(int(value // 1024), 1)}kB"


def rounded_bytes(value: float) -> int:
    """A conf-friendly size: whole GB from 4GB up, whole MB below."""
    unit = GB if value >= 4 * GB else MB
    return max(round(value / unit), 1) * unit


def rounded(value: float) -> str:
    return size(rounded_bytes(value))


def human(settings: dict, name: str) -> str | None:
    value, row = number(settings, name), settings.get(name)
    if not row:
        return None
    unit = row.get("unit") or ""
    if value is None or value < 0 or unit not in UNITS:
        return raw(settings, name)
    if unit in ("us", "ms", "s", "min", "h", "d"):
        return f"{raw(settings, name)}{unit}"
    return size(value)


def apply_context(settings: dict, name: str) -> str:
    context = (settings.get(name) or {}).get("context")
    return {"postmaster": "restart", "sighup": "reload", "superuser": "reload/role/session",
            "user": "reload/role/session", "internal": "not changeable"}.get(context, context or "unknown")


class Review:
    def __init__(self, settings, args, evidence):
        self.s, self.args, self.ev = settings, args, evidence
        self.findings, self.not_evaluated = [], []
        limits = [value for value in (args.ram_gb, getattr(args, "memory_limit_gb", None)) if value is not None]
        self.ram = min(limits) * GB if limits else None
        self.memory_budget = None

    def add(self, name, severity, recommended, rationale, *, caveat=None, evidence=None, scope="global"):
        if name not in self.s:
            return
        self.findings.append({"parameter": name, "severity": severity, "current": human(self.s, name),
                              "recommended": recommended, "apply": apply_context(self.s, name), "scope": scope,
                              "rationale": rationale, "evidence": evidence, "caveat": caveat,
                              "source": self.s[name].get("source")})

    def skip(self, rule, reason):
        self.not_evaluated.append({"rule": rule, "reason": reason})

    def on(self, name):
        return raw(self.s, name) == "on"

    # --- data safety -------------------------------------------------------------------------
    def safety(self):
        for name in ("fsync", "full_page_writes"):
            if raw(self.s, name) == "off":
                self.add(name, "critical", "on", "Turning this off risks unrecoverable corruption after a crash or power loss.",
                         caveat="Acceptable only for disposable data that can be rebuilt from elsewhere.")
        if raw(self.s, "synchronous_commit") == "off":
            self.add("synchronous_commit", "medium", "on (globally); off only per transaction/role where loss is acceptable",
                     "A crash can lose recently acknowledged commits (no corruption).", scope="role/transaction")
        for name in ("autovacuum", "track_counts"):
            if raw(self.s, name) == "off":
                self.add(name, "critical", "on", "Without it dead rows accumulate and XID wraparound protection depends on manual VACUUM.")

    # --- memory ------------------------------------------------------------------------------
    def memory(self):
        if not self.ram:
            self.skip("memory", "--ram-gb not given; memory sizing is not guessed")
            return
        sb = number(self.s, "shared_buffers")
        if sb and not self.args.managed:
            share = sb / self.ram
            if share < 0.15 or share > 0.40:
                self.add("shared_buffers", "medium", rounded(self.ram * 0.25),
                         f"shared_buffers is {share:.1%} of RAM; ~25% is the usual start, leaving the rest to the OS cache and work memory.",
                         caveat="Above ~40% gains are usually small and double caching grows; validate with hit ratio and I/O waits.")
            if sb >= 8 * GB and raw(self.s, "huge_pages") != "on":
                status = raw(self.s, "huge_pages_status")
                if status in (None, "off", "unknown"):
                    self.add("huge_pages", "low", "on (after reserving vm.nr_hugepages)",
                             "Large shared memory without huge pages costs page-table memory and TLB misses.",
                             caveat="With 'on', the server will not start unless enough huge pages are reserved.",
                             evidence={"huge_pages_status": status})
        ecs = number(self.s, "effective_cache_size")
        if ecs:
            share = ecs / self.ram
            if share < 0.4 or share > 0.85:
                self.add("effective_cache_size", "low", rounded(self.ram * 0.7),
                         f"Planner hint for cache available to one query is {share:.0%} of RAM; it allocates nothing.")
        self.work_mem(sb)
        finding = next((item for item in self.findings if item["parameter"] == "shared_buffers"), None)
        if self.memory_budget is not None and finding is not None:
            proposed = rounded_bytes(self.ram * 0.25)
            available = self.memory_budget["query_budget_bytes"]
            projected = max(available + sb - proposed, 0)
            self.memory_budget["proposed_shared_buffers_bytes"] = proposed
            self.memory_budget["projected_query_budget_bytes"] = int(projected)
            current_cap = self.memory_budget["estimated_work_mem_cap_bytes"]
            projected_cap = current_cap * projected / available if current_cap and available else 0
            if proposed > sb and (projected == 0 or
                                  (current_cap is not None and number(self.s, "work_mem") > projected_cap)):
                finding["recommended"] = None
                finding["caveat"] = "The projected workload budget requires coordinated memory changes."

    def work_mem(self, shared_buffers):
        wm = number(self.s, "work_mem")
        active = self.args.max_active_connections or number(self.s, "max_connections")
        maintenance = number(self.s, "maintenance_work_mem")
        vacuum = number(self.s, "autovacuum_work_mem")
        vacuum_workers = number(self.s, "autovacuum_max_workers")
        if shared_buffers is None or not wm or not active or maintenance is None or vacuum_workers is None:
            self.skip("work_mem", "Need shared_buffers, work_mem, concurrency and maintenance settings")
            return
        configured_workers = number(self.s, "max_parallel_workers_per_gather") or 0
        workers = getattr(self.args, "parallel_workers_per_query", None)
        if workers is None:
            workers = configured_workers
        pool = number(self.s, "max_parallel_workers")
        parallel = active * workers
        if pool is not None:
            parallel = min(parallel, pool)
        sort_nodes = getattr(self.args, "sort_nodes", 0)
        hash_nodes = getattr(self.args, "hash_nodes", 2)
        maintenance_jobs = getattr(self.args, "maintenance_jobs", 1)
        if vacuum is None or vacuum < 0:
            vacuum = maintenance
        if raw(self.s, "autovacuum") == "off":
            # Wraparound protection can still start workers.
            self.skip("autovacuum memory", "Reserve configured workers even with autovacuum off")
        overhead = self.ram * 0.2
        reserved = overhead + shared_buffers + vacuum_workers * vacuum + maintenance_jobs * maintenance
        weight = (active + parallel) * (sort_nodes + hash_nodes * (number(self.s, "hash_mem_multiplier") or 2))
        available = max(self.ram - reserved, 0)
        cap = int(available / weight) if weight else None
        self.memory_budget = {
            "ram_bytes": int(self.ram), "os_and_overhead_bytes": int(overhead),
            "shared_buffers_bytes": int(shared_buffers), "maintenance_jobs": maintenance_jobs,
            "maintenance_bytes": int(maintenance_jobs * maintenance), "autovacuum_workers": int(vacuum_workers),
            "autovacuum_bytes": int(vacuum_workers * vacuum), "active_queries": int(active),
            "parallel_workers_per_query_assumed": workers, "parallel_workers_budgeted": int(parallel),
            "sort_nodes_per_process": sort_nodes, "hash_nodes_per_process": hash_nodes,
            "query_budget_bytes": int(available), "estimated_work_mem_cap_bytes": cap,
        }
        if not self.args.max_active_connections:
            self.skip("work_mem sizing", "Concurrency estimate uses max_connections; supply peak active queries")
        explicit_shape = getattr(self.args, "plan_shape_known", False)
        temp = self.ev.get("temp_bytes_per_s")
        minimum = int(float(self.s["work_mem"].get("min_val") or 64)) * 1024
        if available <= 0 or (cap is not None and cap < minimum):
            self.add("work_mem", "high", None,
                     "The estimated budget cannot fit the minimum work_mem. Review reservations and concurrency.",
                     evidence=self.memory_budget)
        elif cap is not None and wm > cap:
            self.add("work_mem", "high", size(cap) if explicit_shape else None,
                     "The supplied concurrency and per-process operators can exceed the estimated budget.",
                     evidence=self.memory_budget)
        elif temp and cap is not None and wm < cap / 4:
            self.add("work_mem", "medium", size(min(cap, wm * 4)) if explicit_shape else None,
                     "Temp writes warrant checking which SQL and operator spilled before sizing work_mem.",
                     evidence=self.memory_budget, scope="role/session")
        if not explicit_shape:
            self.skip("work_mem numeric recommendation", "Supply measured plan shape and peak active queries")

    # --- connections -------------------------------------------------------------------------
    def connections(self):
        mc = number(self.s, "max_connections")
        if not self.args.cpus:
            self.skip("connections", "--cpus not given")
            return
        if mc and mc > max(self.args.cpus * 10, 200):
            self.add("max_connections", "medium", "size to real concurrency and put a connection pooler in front",
                     f"{int(mc)} connections for {self.args.cpus} CPUs: active sessions beyond a few per core add contention, not throughput.",
                     evidence={"average_active_sessions": self.ev.get("average_active_sessions")})

    # --- WAL and checkpoints -----------------------------------------------------------------
    def wal(self):
        cct = number(self.s, "checkpoint_completion_target")
        if cct is not None and cct < 0.9:
            self.add("checkpoint_completion_target", "low", "0.9", "Spreads checkpoint writes over the interval (default since 14).")
        timeout = number(self.s, "checkpoint_timeout") or 300
        timed, requested = self.ev.get("checkpoints_timed"), self.ev.get("checkpoints_requested")
        if timed is None or requested is None:
            self.skip("max_wal_size", "no checkpointer interval evidence (--interval-report)")
        elif (self.ev.get("interval_seconds") or 0) < 3 * timeout:
            self.skip("max_wal_size", "interval shorter than 3 x checkpoint_timeout")
        elif requested + timed > 0 and requested / (requested + timed) > 0.1:
            current = number(self.s, "max_wal_size") or GB
            rate = self.ev.get("wal_bytes_per_s")
            target = max(current * 2, math.ceil(rate * timeout * 2 / GB) * GB if rate else 0)
            self.add("max_wal_size", "high", size(target),
                     "Checkpoints are triggered by WAL volume rather than checkpoint_timeout, multiplying full-page writes and I/O bursts.",
                     caveat="pg_wal can grow to about this size; check disk space and accept longer crash recovery.",
                     evidence={"requested": requested, "timed": timed, "wal_bytes_per_s": rate})
        fpi = self.ev.get("fpi_share")
        if raw(self.s, "wal_compression") == "off" and (fpi is None or fpi > 0.2):
            self.add("wal_compression", "low", "lz4 (or zstd)",
                     "Compresses full-page images, reducing WAL volume, archive and replication traffic for some CPU.",
                     caveat="Requires a build with lz4/zstd; check the accepted values on the server.",
                     evidence={"fpi_share": fpi})
        if fpi is not None and fpi > 0.3 and timeout <= 300:
            self.add("checkpoint_timeout", "low", "15min",
                     "A high share of full-page images after each checkpoint; fewer checkpoints write fewer of them.",
                     caveat="Raise max_wal_size together and accept longer crash recovery.", evidence={"fpi_share": fpi})
        full = self.ev.get("wal_buffers_full_per_s")
        if full and full > 0.1:
            self.add("wal_buffers", "medium", "64MB", "Backends had to write WAL themselves because WAL buffers were full.",
                     evidence={"wal_buffers_full_per_s": full})

    # --- planner and I/O ---------------------------------------------------------------------
    def io(self):
        storage = self.args.storage
        if not storage:
            self.skip("planner_io", "--storage not given")
            return
        rpc = number(self.s, "random_page_cost")
        if storage in ("ssd", "network") and rpc is not None and rpc >= 3:
            self.add("random_page_cost", "medium", "1.1" if storage == "ssd" else "1.5-2.0",
                     "The default 4.0 models spinning disks and undervalues index scans on SSD-class storage.",
                     caveat="Changes many plans at once; compare representative plans. Can also be set per tablespace.")
        for name, low in (("effective_io_concurrency", 2), ("maintenance_io_concurrency", 10)):
            value = number(self.s, name)
            if storage in ("ssd", "network") and value is not None and value <= low:
                self.add(name, "low", "200" if storage == "ssd" else "64-128",
                         "Low prefetch depth leaves SSD/network storage parallelism unused (bitmap scans, VACUUM, AIO reads).")
        if raw(self.s, "io_method") == "sync" and storage != "hdd":
            self.add("io_method", "low", "worker (default) or io_uring on supported Linux builds",
                     "PostgreSQL 18 asynchronous I/O is disabled.", caveat="Requires restart; measure before and after.")

    def jit(self):
        share = self.ev.get("jit_share")
        if raw(self.s, "jit") != "on":
            return
        if share is not None and share > 0.05:
            self.add("jit", "high", "off (or raise jit_above_cost)",
                     f"JIT compilation is {share:.0%} of top-level execution time in the interval.", evidence={"jit_share": share})
        elif self.args.workload == "oltp":
            self.add("jit", "low", "off for OLTP roles, or raise jit_above_cost",
                     "Short OLTP queries rarely benefit; mis-estimated costs can trigger expensive compilation.",
                     scope="global or role")

    def parallel(self):
        mwp, mpw = number(self.s, "max_worker_processes"), number(self.s, "max_parallel_workers")
        per_gather = number(self.s, "max_parallel_workers_per_gather")
        if mwp is not None and mpw is not None and mpw > mwp:
            self.add("max_parallel_workers", "medium", str(int(mwp)),
                     "Capped by max_worker_processes, which also serves logical replication and extensions.")
        if not self.args.cpus:
            self.skip("parallel", "--cpus not given")
            return
        if mpw is not None and mpw > self.args.cpus:
            self.add("max_parallel_workers", "low", str(self.args.cpus), "More parallel workers than CPUs only adds contention.")
        if per_gather is not None:
            if self.args.workload == "olap" and per_gather <= 2 and self.args.cpus >= 8:
                self.add("max_parallel_workers_per_gather", "low", str(min(self.args.cpus // 2, 8)),
                         "Analytic scans and aggregates can use more workers when concurrency is low.",
                         caveat="Each worker multiplies work_mem use; keep max_parallel_workers as the global cap.")
            elif self.args.workload == "oltp" and per_gather > 4:
                self.add("max_parallel_workers_per_gather", "low", "2",
                         "High per-query parallelism competes with concurrent OLTP sessions.")

    # --- autovacuum --------------------------------------------------------------------------
    def autovacuum(self):
        delay = number(self.s, "autovacuum_vacuum_cost_delay")
        if delay is not None and delay < 0:
            delay = number(self.s, "vacuum_cost_delay")
        if delay is not None and delay > 0.002:
            self.add("autovacuum_vacuum_cost_delay", "medium", "2ms",
                     "Long sleeps throttle autovacuum (20ms was the pre-12 default).")
        limit = number(self.s, "autovacuum_vacuum_cost_limit")
        if limit is not None and limit < 0:
            limit = number(self.s, "vacuum_cost_limit")
        if limit is not None and limit <= 200 and self.args.storage in ("ssd", "network"):
            self.add("autovacuum_vacuum_cost_limit", "medium", "1000-2000",
                     "The cost budget is shared by all autovacuum workers; 200 limits throughput on modern storage.",
                     caveat="Watch I/O latency; confirm need via autovacuum backlog or Timeout:VacuumDelay waits.",
                     evidence={"effective_cost_limit": limit})
        workers = number(self.s, "autovacuum_max_workers")
        if workers is not None and workers <= 3 and (self.args.cpus or 0) >= 16:
            self.add("autovacuum_max_workers", "low", "5-6",
                     "More large tables can be vacuumed concurrently.",
                     caveat="Raise the cost limit too, because it is divided among workers. PG18 changes it on reload up to autovacuum_worker_slots.")
        naptime = number(self.s, "autovacuum_naptime")
        if naptime is not None and naptime > 60:
            self.add("autovacuum_naptime", "low", "1min", "Long naptime delays vacuum of busy tables in every database.")

    # --- observability and safety nets -------------------------------------------------------
    def observability(self):
        if "pg_stat_statements" not in (raw(self.s, "shared_preload_libraries") or ""):
            self.add("shared_preload_libraries", "medium", "append pg_stat_statements to the existing list",
                     "Per-SQL statistics are the basis of query tuning.", caveat="Requires restart and CREATE EXTENSION.")
        if raw(self.s, "compute_query_id") == "off":
            self.add("compute_query_id", "medium", "auto", "query_id links pg_stat_statements, activity and logs.")
        checks = (("track_io_timing", "off", "on", "I/O time per SQL/table; check pg_test_timing overhead first."),
                  ("log_checkpoints", "off", "on", "Checkpoint cause and duration in the log."),
                  ("log_lock_waits", "off", "on", "Waits longer than deadlock_timeout are logged."),
                  ("log_temp_files", "-1", "10MB", "Identify spilling SQL in the log."),
                  ("log_autovacuum_min_duration", "-1", "10s", "Slow autovacuum runs and their cause in the log."),
                  ("log_min_duration_statement", "-1", "SLO-based, e.g. 1s (or log_min_duration_sample)",
                   "Slow statements with their parameters' shape in the log."))
        for name, bad, good, why in checks:
            if raw(self.s, name) == bad:
                self.add(name, "low", good, why)
        size_ = number(self.s, "track_activity_query_size")
        if size_ is not None and size_ < 4096:
            self.add("track_activity_query_size", "info", "4kB", "Long SQL is truncated in pg_stat_activity.")

    def safety_nets(self):
        if raw(self.s, "idle_in_transaction_session_timeout") == "0":
            self.add("idle_in_transaction_session_timeout", "low", "e.g. 5min for application roles",
                     "Forgotten open transactions hold locks and block VACUUM cleanup.", scope="role")
        if (number(self.s, "statement_timeout") or 0) > 0:
            self.add("statement_timeout", "info", "keep 0 globally; set per application role",
                     "A global limit also cancels maintenance, migrations and batch jobs.", scope="role")
        if (number(self.s, "max_prepared_transactions") or 0) > 0:
            self.add("max_prepared_transactions", "info", "0 unless two-phase commit is used",
                     "Orphaned prepared transactions hold locks and block VACUUM indefinitely.")
        if raw(self.s, "temp_file_limit") == "-1":
            self.add("temp_file_limit", "info", "a per-session cap below free temp space",
                     "One runaway query can fill the disk with temp files.")

    def run(self):
        for rule in (self.safety, self.memory, self.connections, self.wal, self.io, self.jit,
                     self.parallel, self.autovacuum, self.observability, self.safety_nets):
            rule()
        self.findings.sort(key=lambda x: SEVERITY.index(x["severity"]))
        return self.findings


def evidence_from_report(report: dict | None) -> dict:
    """Pull rates from postgres-performance-diagnostics analyze.py JSON output."""
    if not report:
        return {}
    deltas = report.get("deltas", {})

    def first(name):
        rows = deltas.get(name) or []
        return rows[0] if rows else None

    result = {}
    checkpointer = first("checkpointer")
    if checkpointer:
        result.update(checkpoints_timed=checkpointer["delta"].get("num_timed"),
                      checkpoints_requested=checkpointer["delta"].get("num_requested"),
                      interval_seconds=checkpointer.get("elapsed_seconds"))
    wal = first("wal")
    if wal:
        result["wal_bytes_per_s"] = wal["per_second"].get("wal_bytes")
        result["wal_buffers_full_per_s"] = wal["per_second"].get("wal_buffers_full")
        records, fpi = wal["delta"].get("wal_records"), wal["delta"].get("wal_fpi")
        result["fpi_share"] = fpi / records if records and fpi is not None else None
    database = first("database")
    if database:
        result["temp_bytes_per_s"] = database["per_second"].get("temp_bytes")
        result["average_active_sessions"] = database.get("average_active_sessions")
    sql = [x for x in deltas.get("pgss") or [] if x.get("toplevel") is not False]
    total = sum(x["delta"].get("total_exec_time", 0) for x in sql)
    jit = sum(x["delta"].get(k, 0) for x in sql for k in
              ("jit_generation_time", "jit_inlining_time", "jit_optimization_time", "jit_emission_time", "jit_deform_time"))
    if sql and total > 0:
        result["jit_share"] = jit / total
    return result


def review(settings: dict, args, report=None, *, provenance=None) -> dict:
    warnings = []
    if report is not None and provenance is not None:
        baseline = dict(report.get("metadata") or {})
        baseline.update({key: report[key] for key in ("instance_id", "database") if key in report})
        fields = ("instance_id", "database", "database_oid", "server_version_num", "session_role",
                  "effective_role", "in_recovery", "postmaster_start")
        conflicts = [key for key in fields if provenance.get(key) is not None
                     and baseline.get(key) is not None and provenance[key] != baseline[key]]
        unknown = [key for key in fields if provenance.get(key) is None or baseline.get(key) is None]
        sampled = (report.get("settings") or {}).get("after", {})
        conflicts.extend(name for name, value in sampled.items()
                         if raw(settings, name) not in (None, "<redacted>", value))
        if conflicts:
            warnings.append("Interval evidence excluded: different " + ", ".join(conflicts))
            report = None
        elif unknown:
            warnings.append("Interval evidence provenance unverified: " + ", ".join(unknown))
            report = None
    elif report is not None:
        warnings.append("Interval evidence target metadata not supplied to review()")
    evidence = evidence_from_report(report)
    engine = Review(settings, args, evidence)
    findings = engine.run()
    pending = sorted(name for name, row in settings.items() if row.get("pending_restart") in (True, "t", "true"))
    return {"schema_version": 1, "kind": "config_review",
            "inputs": {"ram_gb": args.ram_gb, "cpus": args.cpus, "storage": args.storage, "workload": args.workload,
                       "max_active_connections": args.max_active_connections, "managed": args.managed},
            "provenance": provenance, "warnings": warnings, "memory_budget": engine.memory_budget,
            "evidence": evidence, "findings": findings, "not_evaluated": engine.not_evaluated,
            "pending_restart": pending, "limitations": LIMITATIONS}


def markdown(result: dict) -> str:
    lines = ["# PostgreSQL configuration review", "", "Inputs: " + json.dumps(result["inputs"], ensure_ascii=False), ""]
    lines += [f"- {warning}" for warning in result["warnings"]]
    if result.get("scoped_overrides") is not None:
        lines += ["", "Session role and database overrides: "
                  + json.dumps(result["scoped_overrides"], ensure_ascii=False)]
    if result["memory_budget"] is not None:
        lines += ["", "## Memory budget", "", "| Input | Value |", "|---|---:|"]
        lines += [f"| {key} | {value} |" for key, value in result["memory_budget"].items()]
    if result["pending_restart"]:
        lines += ["Already changed but waiting for restart: " + ", ".join(result["pending_restart"]), ""]
    lines += ["| Severity | Parameter | Current | Recommended | Apply | Scope | Why |", "|---|---|---|---|---|---|---|"]
    for f in result["findings"]:
        cells = [f["severity"], f["parameter"], f["current"], f["recommended"], f["apply"], f["scope"], f["rationale"]]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
    if not result["findings"]:
        lines.append("| — | No rule matched | | | | | Not proof that the configuration is optimal |")
    lines += ["", "## Caveats and evidence", ""]
    for f in result["findings"]:
        if f["caveat"] or f["evidence"]:
            lines.append(f"- {f['parameter']}: {f['caveat'] or ''} "
                         + (json.dumps(f["evidence"], ensure_ascii=False) if f["evidence"] else ""))
    lines += ["", "## Not evaluated", ""] + [f"- {x['rule']}: {x['reason']}" for x in result["not_evaluated"]]
    lines += ["", "## Limits", ""] + ["- " + x for x in result["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path, help="Output of sql/settings.sql, or a diagnostics collect.py snapshot")
    parser.add_argument("--ram-gb", type=float, help="Memory available to PostgreSQL on this host/instance")
    parser.add_argument("--memory-limit-gb", type=float, help="cgroup or provider memory limit")
    parser.add_argument("--sort-nodes", type=int, help="Peak simultaneous sort operators per process")
    parser.add_argument("--hash-nodes", type=int, help="Peak simultaneous hash operators per process")
    parser.add_argument("--parallel-workers-per-query", type=int)
    parser.add_argument("--maintenance-jobs", type=int, default=1)
    parser.add_argument("--cpus", type=int, help="vCPUs available to PostgreSQL")
    parser.add_argument("--storage", choices=("ssd", "network", "hdd"))
    parser.add_argument("--workload", choices=("oltp", "olap", "mixed"), default="mixed")
    parser.add_argument("--max-active-connections", type=int, help="Expected concurrently active sessions (pool size)")
    parser.add_argument("--managed", action="store_true", help="Managed service: skip OS-level sizing such as shared_buffers")
    parser.add_argument("--interval-report", type=Path, help="analyze.py --format json output for evidence")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    for name in ("ram_gb", "memory_limit_gb", "cpus", "max_active_connections"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be positive")
    args.plan_shape_known = (args.sort_nodes is not None and args.hash_nodes is not None
                             and args.parallel_workers_per_query is not None
                             and args.max_active_connections is not None)
    args.sort_nodes = 0 if args.sort_nodes is None else args.sort_nodes
    args.hash_nodes = 2 if args.hash_nodes is None else args.hash_nodes
    for name in ("sort_nodes", "hash_nodes", "parallel_workers_per_query", "maintenance_jobs"):
        value = getattr(args, name)
        if value is not None and value < 0:
            parser.error(f"--{name.replace('_', '-')} must be nonnegative")
    try:
        source = json.loads(args.settings.read_text(encoding="utf-8-sig"))
        settings = load_settings(source)
        provenance = dict(source.get("metadata") or {})
        if source.get("instance_id") is not None:
            provenance["instance_id"] = source["instance_id"]
        report = json.loads(args.interval_report.read_text(encoding="utf-8")) if args.interval_report else None
        result = review(settings, args, report, provenance=provenance)
        result["scoped_overrides"] = source.get("scoped_overrides")
        text = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                if args.format == "json" else markdown(result))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
