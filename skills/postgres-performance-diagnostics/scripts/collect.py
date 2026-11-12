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
MARKER = "/* postgres-performance-diagnostics */"
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
    # PostgreSQL 19 replaces io_workers with a worker pool and logs autoanalyze separately.
    "io_min_workers", "io_max_workers", "log_autoanalyze_min_duration", "autovacuum_max_parallel_workers",
    "timing_clock_source", "effective_wal_level",
    "track_cost_delay_timing", "vacuum_failsafe_age", "huge_pages_status", "temp_buffers",
    "logical_decoding_work_mem", "data_checksums", "bgwriter_lru_maxpages",
    "max_wal_senders", "max_replication_slots", "wal_keep_size", "archive_mode", "archive_timeout",
    "idle_replication_slot_timeout", "recovery_min_apply_delay", "max_standby_archive_delay",
    "idle_session_timeout", "log_lock_failures", "log_statement", "log_duration",
    "default_transaction_isolation", "jit_inline_above_cost", "jit_optimize_above_cost",
)
# Coarse, fixed reasons for failures that carry no SQLSTATE. The matched text is never stored:
# libpq and docker messages can include host names, user names and socket paths.
FAILURES = (
    (r"no such container", "docker_container_not_found"),
    (r"container \S+ is not running|is not running", "docker_container_not_running"),
    (r"cannot connect to the docker daemon|docker daemon is not running|permission denied while trying to connect to the docker",
     "docker_unavailable"),
    (r"executable file not found|no such file or directory: unknown", "psql_not_found_in_container"),
    (r"database \".*\" does not exist", "database_not_found"),
    (r"role \".*\" does not exist", "role_not_found"),
    (r"password authentication failed|authentication failed", "authentication_failed"),
    (r"no password supplied", "password_required"),
    (r"no pg_hba\.conf entry", "rejected_by_pg_hba"),
    (r"too many clients|remaining connection slots are reserved", "too_many_connections"),
    (r"the database system is (starting up|shutting down|in recovery mode|not yet accepting connections)",
     "server_not_accepting_connections"),
    (r"definition of service \".*\" not found|service file \".*\" not found", "service_not_found"),
    (r"could not translate host name|name or service not known|nodename nor servname|temporary failure in name resolution",
     "unknown_host"),
    (r"connection refused", "connection_refused"),
    (r"timeout expired|timed out", "connection_timeout"),
    (r"failed: no such file or directory|is the server running locally", "socket_not_found"),
    (r"ssl error|ssl connection|sslmode|does not support ssl|certificate|gssapi", "tls_or_gss_negotiation_failed"),
)
HINTS = {
    "password_required": "No password was available non-interactively; use a libpq password file or service, never a command-line password.",
    "authentication_failed": "The server rejected the credentials; check the role, password file entry and pg_hba method.",
    "rejected_by_pg_hba": "pg_hba.conf has no rule for this client address, database, role and TLS mode.",
    "database_not_found": "Check PGDATABASE or the service definition; collect once per database.",
    "role_not_found": "Check PGUSER or the service definition.",
    "connection_refused": "Nothing listens on that host and port; check PGHOST, PGPORT and the server state.",
    "connection_timeout": "No answer within PGCONNECT_TIMEOUT; check the network path and firewall.",
    "unknown_host": "The host name does not resolve; check PGHOST or the service definition.",
    "socket_not_found": "No server socket at the default location; set PGHOST or PGSERVICE.",
    "service_not_found": "PGSERVICE names a service that pg_service.conf does not define.",
    "too_many_connections": "No connection slot is free; this is itself evidence of connection exhaustion.",
    "server_not_accepting_connections": "The server is starting, stopping or recovering; retry and record the event time.",
    "tls_or_gss_negotiation_failed": "Encryption negotiation failed; check sslmode, certificates and the server's ssl setting.",
    "docker_container_not_found": "No container has that name; check --docker-container.",
    "docker_container_not_running": "The container exists but is stopped.",
    "docker_unavailable": "The Docker daemon is not reachable from this user.",
    "psql_not_found_in_container": "The container image has no psql on its PATH.",
    "client_timeout": "psql did not finish in time; raise --statement-timeout-ms or check the connection.",
    "sqlstate_42501": "Insufficient privilege; a monitoring role with pg_monitor or pg_read_all_stats sees these statistics.",
    "sqlstate_55P03": "lock_timeout expired behind a conflicting lock; the section was retried once.",
    "sqlstate_57014": "statement_timeout expired; raise --statement-timeout-ms or lower --row-limit.",
    "sqlstate_55000": "The object is not usable in this state, e.g. pg_stat_statements is installed but not in shared_preload_libraries.",
    "sqlstate_53200": "The server is out of memory or shared memory.",
    "sqlstate_25006": "The transaction is read-only, as intended; a collector statement tried to write.",
}


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def mark(sql: str) -> str:
    """Tag a collector statement so pgss.sql can leave it out of the SQL rankings.

    pg_stat_statements (verified on 18.6 and 19rc1) stores a statement from its first token, so
    a comment in front of it is lost; one after the first keyword is kept. Only the collector's
    SELECTs are tagged: BEGIN/SET/COMMIT share their query ID with the application's, and a tag
    stored first by the collector would hide the application's calls as well.
    """
    match = re.match(r"\s*(\w+)\s+(.*)", sql, re.DOTALL)
    if not match:
        raise ValueError("collector SQL must start with a keyword")
    return f"{match.group(1)} {MARKER} {match.group(2)}"


def failure_reason(stderr: str) -> str:
    """Reduce psql/docker stderr to a SQLSTATE or a fixed category; never return its text."""
    states = re.findall(r"(?:ERROR|FATAL):\s+([0-9A-Z]{5})(?:\s|$)", stderr or "")
    if states:
        return "sqlstate_" + states[-1]
    text = (stderr or "").lower()
    for pattern, reason in FAILURES:
        if re.search(pattern, text):
            return reason
    return "psql_failed"


class CollectionError(Exception):
    pass


class Psql:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.env = dict(os.environ)
        self.env["PGAPPNAME"] = "postgres-performance-diagnostics"
        self.env["PGCLIENTENCODING"] = "UTF8"
        self.env.setdefault("PGCONNECT_TIMEOUT", "10")
        # Connection failures are classified from untranslated client messages.
        self.env["LC_MESSAGES"] = "C"

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
            + mark(sql.rstrip().rstrip(";")) + ";\nCOMMIT;\n"
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
            # Server errors can contain SQL literals or connection details: store only a
            # SQLSTATE or a fixed failure category.
            raise CollectionError(failure_reason(result.stderr))
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
  'server_version', version(), 'database', current_database(), 'session_role', session_user, 'effective_role', current_user,
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
    # source tells a role/database override (ALTER ROLE ... SET) of this collecting role from the
    # cluster-wide value; the collector's own SET LOCAL sources are not reported as such.
    sections["settings"] = client.section(
        "SELECT name, CASE WHEN name IN ('statement_timeout', 'lock_timeout', 'stats_fetch_consistency', "
        "'search_path', 'timezone') THEN reset_val ELSE setting END AS setting, "
        "unit, context, CASE WHEN name IN ('statement_timeout', 'lock_timeout', 'stats_fetch_consistency', "
        "'search_path', 'timezone') THEN NULL ELSE source END AS source, pending_restart FROM pg_settings "
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
        # Definitions are computed only on request: pg_get_indexdef() also takes a table lock.
        sql = sql.replace("__INCLUDE_TEXT__", "true" if args.include_query_text else "false")
        sensitive = ("conninfo",) if args.include_query_text else ("query", "indexdef", "conninfo")
        for attempt in (1, 2):
            try:
                sections[item["name"]] = client.section(sql, sensitive)
                if attempt == 2:
                    sections[item["name"]]["retried_after"] = "sqlstate_55P03"
                break
            except CollectionError as exc:
                reason = str(exc)
                # A lock taken between the "busy relation" check and the size call: the second
                # attempt sees it and skips that relation.
                if reason == "sqlstate_55P03" and attempt == 1:
                    continue
                sections[item["name"]] = {"status": "error", "reason": reason, "rows": []}
                if reason in HINTS:
                    sections[item["name"]]["hint"] = HINTS[reason]
                break
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
        hint = HINTS.get(str(exc)) if isinstance(exc, CollectionError) else None
        print(f"error: {exc}" + (f" ({hint})" if hint else ""), file=sys.stderr)
        return 2
    failed = {name: value.get("reason") for name, value in snapshot["sections"].items() if value["status"] == "error"}
    detail = " (" + ", ".join(f"{name}={reason}" for name, reason in failed.items()) + ")" if failed else ""
    print(f"Saved snapshot: {args.output}; section errors: {len(failed)}{detail}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
