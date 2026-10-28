#!/usr/bin/env python3
"""Collect bounded, read-only PostgreSQL evidence through psql; stdlib only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SCHEMA_VERSION = 1
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
SETTINGS = (
    "track_counts", "track_activities", "track_io_timing", "track_wal_io_timing",
    "track_functions", "compute_query_id", "stats_fetch_consistency", "block_size",
    "shared_buffers", "effective_cache_size", "work_mem", "hash_mem_multiplier",
    "maintenance_work_mem", "autovacuum_work_mem", "max_connections",
    "max_worker_processes", "max_parallel_workers", "max_parallel_workers_per_gather",
    "max_parallel_maintenance_workers", "autovacuum", "autovacuum_max_workers",
    "autovacuum_naptime", "autovacuum_vacuum_threshold", "autovacuum_vacuum_scale_factor",
    "autovacuum_analyze_threshold", "autovacuum_analyze_scale_factor",
    "autovacuum_vacuum_insert_threshold", "autovacuum_vacuum_insert_scale_factor",
    "autovacuum_vacuum_max_threshold", "autovacuum_worker_slots", "vacuum_cost_limit",
    "vacuum_buffer_usage_limit", "autovacuum_freeze_max_age","autovacuum_multixact_freeze_max_age",
    "vacuum_freeze_table_age", "vacuum_multixact_freeze_table_age",
    "autovacuum_vacuum_cost_delay", "autovacuum_vacuum_cost_limit",
    "checkpoint_timeout", "checkpoint_completion_target", "max_wal_size", "min_wal_size",
    "wal_level", "wal_compression", "fsync", "full_page_writes", "synchronous_commit",
    "max_slot_wal_keep_size", "hot_standby_feedback", "max_standby_streaming_delay",
    "wal_buffers", "statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout",
    "transaction_timeout", "max_locks_per_transaction", "max_prepared_transactions",
    "temp_file_limit", "huge_pages", "track_activity_query_size",
    "random_page_cost", "seq_page_cost", "effective_io_concurrency", "io_method",
    "io_workers", "io_combine_limit", "maintenance_io_concurrency",
    "plan_cache_mode", "default_statistics_target", "jit", "jit_above_cost",
    "log_min_duration_statement", "log_min_duration_sample", "log_statement_sample_rate",
    "log_temp_files", "log_lock_waits", "deadlock_timeout", "log_autovacuum_min_duration",
    "log_checkpoints", "log_destination", "logging_collector", "shared_preload_libraries",
    "pg_stat_statements.track", "pg_stat_statements.track_planning", "pg_stat_statements.max",
    "pg_stat_statements.save", "pg_stat_statements.track_utility","auto_explain.log_min_duration", "auto_explain.sample_rate",
    "auto_explain.log_analyze", "auto_explain.log_timing", "auto_explain.log_buffers",
)


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class CollectionError(Exception):
    pass


class Psql:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.env = dict(os.environ)
        self.env["PGAPPNAME"] = "postgres-performance-diagnostics"
        self.env["PGCLIENTENCODING"] = "UTF8"
        self.env.setdefault("PGCONNECT_TIMEOUT", "10")

    def run(self, sql: str):
        # SQL, never passwords/connection strings, is sent on stdin. -X ignores .psqlrc.
        command = [self.args.psql, "-X", "-q", "-A", "-t", "--no-password",
                   "--set=ON_ERROR_STOP=1", "--set=VERBOSITY=sqlstate"]
        if self.args.docker_container:
            command = ["docker", "exec", "-i", "-e", "PGAPPNAME=postgres-performance-diagnostics",
                       "-e", "PGCLIENTENCODING=UTF8", "-e", "PGCONNECT_TIMEOUT=10",
                       self.args.docker_container] + command
        wrapped = (
            "BEGIN READ ONLY;\nSET LOCAL search_path = pg_catalog;\n"
            f"SET LOCAL statement_timeout = '{self.args.statement_timeout_ms}ms';\n"
            f"SET LOCAL lock_timeout = '{self.args.lock_timeout_ms}ms';\n"
            "SET LOCAL timezone = 'UTC';\nSET LOCAL stats_fetch_consistency = 'none';\n"
            + "/* postgres-performance-diagnostics */ " + sql.rstrip().rstrip(";") + ";\nCOMMIT;\n"
        )
        try:
            result = subprocess.run(command, input=wrapped, text=True, encoding="utf-8",
                                    capture_output=True, env=self.env,
                                    timeout=max(15, self.args.statement_timeout_ms / 1000 + 15),
                                    check=False)
        except FileNotFoundError as exc:
            raise CollectionError("psql or docker executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CollectionError("client_timeout") from exc
        if result.returncode:
            # Server errors can contain SQL literals or connection details: store only SQLSTATE.
            states = re.findall(r"(?:ERROR|FATAL):\s+([0-9A-Z]{5})(?:\s|$)", result.stderr)
            raise CollectionError("sqlstate_" + states[-1] if states else "psql_failed")
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise CollectionError("invalid_psql_json") from exc

    def section(self, sql: str, sensitive: tuple[str, ...] = ()) -> dict:
        subtract = "".join(" - '" + key + "'" for key in sensitive)
        query = sql.strip().rstrip(";")
        result = self.run(
            "SELECT jsonb_build_object('collected_at', clock_timestamp(), 'rows', "
            "COALESCE(jsonb_agg(to_jsonb(q)" + subtract + "), '[]'::jsonb)) "
            "FROM (" + query + f"\nLIMIT {self.args.row_limit + 1}) q"
        )
        rows = result["rows"]
        return {"status": "ok", "collected_at": result["collected_at"],
                "truncated": len(rows) > self.args.row_limit, "rows": rows[:self.args.row_limit]}


def preflight(client: Psql) -> dict:
    return client.run("""
SELECT jsonb_build_object(
  'server_version_num', current_setting('server_version_num')::integer,
  'server_version', version(), 'database', current_database(),
  'database_oid', (SELECT oid FROM pg_database WHERE datname = current_database()),
  'postmaster_start', pg_postmaster_start_time(), 'in_recovery', pg_is_in_recovery(),
  'block_size', current_setting('block_size')::integer,
  'collected_at', clock_timestamp(),
  'stats_visible', (SELECT rolsuper OR pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')
                    FROM pg_roles WHERE rolname = current_user),
  'extensions', COALESCE((SELECT jsonb_agg(jsonb_build_object(
     'name', e.extname, 'version', e.extversion, 'schema', n.nspname))
     FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace), '[]'::jsonb),
  'relations', COALESCE((SELECT jsonb_agg(jsonb_build_object(
     'schema', n.nspname, 'name', c.relname,
     'columns', (SELECT jsonb_agg(a.attname ORDER BY a.attnum)
                 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped)))
     FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE (n.nspname='pg_catalog' AND c.relkind IN ('v','m','r'))
        OR (c.relname IN ('pg_stat_statements','pg_stat_statements_info')
            AND n.oid IN (SELECT extnamespace FROM pg_extension WHERE extname='pg_stat_statements'))),
     '[]'::jsonb))
""")


def collect(args: argparse.Namespace) -> dict:
    client = Psql(args)
    meta = preflight(client)
    major = meta["server_version_num"] // 10000
    if major not in (18, 19):
        raise CollectionError("supported majors are PostgreSQL 18 and 19")
    inventory = meta.pop("relations")
    available = {(r["schema"], r["name"]) for r in inventory}
    extension = next((e for e in meta["extensions"] if e["name"] == "pg_stat_statements"), None)
    sections = {}
    names = ",".join("'" + name + "'" for name in SETTINGS)
    # Psql.run SET LOCALs these; report the session default instead of the collector's own value.
    sections["settings"] = client.section(
        "SELECT name, CASE WHEN name IN ('statement_timeout', 'lock_timeout', 'stats_fetch_consistency', "
        "'search_path', 'timezone') THEN reset_val ELSE setting END AS setting, "
        "unit, context, pending_restart FROM pg_settings "
        f"WHERE name IN ({names}) ORDER BY name"
    )
    manifest = json.loads((SQL_DIR / "queries.json").read_text(encoding="utf-8"))
    for item in manifest:
        missing = []
        for relation in item["requires"]:
            schema, name = relation.split(".", 1)
            if schema == "extension":
                schema = extension["schema"] if extension else None
            if (schema, name) not in available:
                missing.append(relation)
        if missing:
            sections[item["name"]] = {"status": "unavailable", "reason": "missing_relation",
                                      "missing": missing, "rows": []}
            continue
        sql = (SQL_DIR / item["file"]).read_text(encoding="utf-8")
        if "__PGSS_SCHEMA__" in sql:
            sql = sql.replace("__PGSS_SCHEMA__", quote_identifier(extension["schema"]))
        sensitive = ("conninfo",) if args.include_query_text else ("query", "indexdef", "conninfo")
        try:
            sections[item["name"]] = client.section(sql, sensitive)
        except CollectionError as exc:
            sections[item["name"]] = {"status": "error", "reason": str(exc), "rows": []}
    end_meta = preflight(client)
    warnings = []
    if any(meta[k] != end_meta[k] for k in ("postmaster_start", "in_recovery", "database_oid")):
        warnings.append("server_identity_changed_during_collection")
    if not meta["stats_visible"]:
        warnings.append("restricted_statistics_visibility")
    warnings.append("Sequential section snapshots are not an atomic cluster snapshot.")
    return {"schema_version": SCHEMA_VERSION, "instance_id": args.instance_id,
            "started_at": meta["collected_at"], "finished_at": end_meta["collected_at"],
            "metadata": meta, "capabilities": inventory,
            "options": {"include_query_text": args.include_query_text, "row_limit": args.row_limit},
            "warnings": warnings, "sections": sections}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id", required=True, help="Stable cluster/node identity, never a secret")
    p.add_argument("--output", required=True, type=Path, help="New JSON file; never overwritten")
    p.add_argument("--psql", default="psql", help="psql executable; connect with libpq environment/service")
    p.add_argument("--docker-container", help="Run psql inside an existing explicitly selected container")
    p.add_argument("--include-query-text", action="store_true", help="Include SQL/index definitions (sensitive)")
    p.add_argument("--row-limit", type=positive, default=10000)
    p.add_argument("--statement-timeout-ms", type=positive, default=10000)
    p.add_argument("--lock-timeout-ms", type=positive, default=1000)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.output.exists():
        print("error: output already exists; choose a new snapshot path", file=sys.stderr)
        return 2
    try:
        snapshot = collect(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    except (CollectionError, OSError, ValueError, KeyError) as exc:
        # File errors might include a path, but never echo connection strings or server error text.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failed = [name for name, value in snapshot["sections"].items() if value["status"] == "error"]
    print(f"Saved snapshot: {args.output}; section errors: {len(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
