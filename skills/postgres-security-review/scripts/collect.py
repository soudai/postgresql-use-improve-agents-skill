#!/usr/bin/env python3
"""Collect bounded PostgreSQL security metadata using libpq and psql."""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from queries import queries
from report import write_new

IDENTITY = """jsonb_build_object(
    'database', current_database(), 'session_role', session_user,
    'effective_role', current_user, 'version', current_setting('server_version'),
    'version_num', current_setting('server_version_num')::int,
    'database_oid', (SELECT oid FROM pg_database WHERE datname = current_database()),
    'postmaster_started', pg_postmaster_start_time(),
    'in_recovery', pg_is_in_recovery(),
    'endpoint', inet_server_addr(), 'port', inet_server_port())"""


class CollectionError(Exception):
    pass


class Collector:
    def __init__(self, timeout):
        self.timeout = timeout

    def query(self, sql):
        env = {**os.environ, "PGAPPNAME": "postgres-security-review",
               "PGCLIENTENCODING": "UTF8", "PGCONNECT_TIMEOUT": "5"}
        script = f"""BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
    'name', name, 'setting', setting, 'unit', unit, 'source', source) ORDER BY name)
FROM pg_catalog.pg_settings
WHERE name OPERATOR(pg_catalog.=) ANY (ARRAY['search_path', 'statement_timeout', 'lock_timeout', 'TimeZone']::pg_catalog.text[]);
SET LOCAL search_path TO pg_catalog;
SET LOCAL statement_timeout TO '{self.timeout}ms';
SET LOCAL lock_timeout TO '1000ms';
SET LOCAL timezone TO 'UTC';
{sql};
COMMIT;
"""
        try:
            result = subprocess.run(
                ["psql", "-X", "-qAtw", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate"],
                input=script, capture_output=True, text=True, encoding="utf-8",
                env=env, check=False, timeout=8 + self.timeout / 1000,
            )
        except subprocess.TimeoutExpired:
            raise CollectionError("client_timeout") from None
        except (OSError, UnicodeError):
            raise CollectionError("psql_unavailable") from None
        if result.returncode:
            state = re.search(r"(?m)^(?:ERROR|FATAL):\s+([0-9A-Z]{5})\b", result.stderr)
            raise CollectionError("sqlstate_" + state[1] if state else "connection_or_query_failed")
        try:
            lines = result.stdout.splitlines()
            if len(lines) != 2:
                raise ValueError("unexpected result count")
            settings, value = (json.loads(line) for line in lines)
            if not isinstance(settings, list) or not isinstance(value, dict):
                raise ValueError("unexpected result type")
            value["collection_settings"] = settings
            return value
        except ValueError:
            raise CollectionError("invalid_query_output") from None

    def collect(self, target, database, limit, privileged):
        meta = self.query(f"""SELECT jsonb_build_object(
            'identity', {IDENTITY}, 'collected_at', clock_timestamp())""")
        identity = meta["identity"]
        if identity["database"] != database:
            raise CollectionError("unexpected_database")
        sections = {}
        for name, sql in queries(identity["version_num"]).items():
            if name in {"hba", "passwords"} and not privileged:
                sections[name] = {"status": "not_collected"}
                continue
            try:
                value = self.query(f"""SELECT jsonb_build_object(
                    'identity', {IDENTITY}, 'observed_at', clock_timestamp(),
                    'rows', coalesce((SELECT jsonb_agg(to_jsonb(q))
                                      FROM ({sql} LIMIT {limit + 1}) q), '[]'::jsonb))""")
                if value["identity"] != identity:
                    raise CollectionError("target_changed")
                rows = value["rows"]
                sections[name] = {"status": "truncated" if len(rows) > limit else "ok",
                                  "observed_at": value["observed_at"], "rows": rows[:limit]}
            except CollectionError as exc:
                sections[name] = {"status": "unavailable", "reason": str(exc)}
        return {"schema_version": 1, "target": target, **meta, "sections": sections}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Stable, nonsecret cluster/node label")
    parser.add_argument("--database", required=True, help="Expected database name (libpq selects the connection)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--privileged", action="store_true", help="Also request HBA and verifier format classifications")
    args = parser.parse_args()
    if not 1 <= args.limit <= 50000 or not 1 <= args.timeout_ms <= 60000:
        parser.error("limit must be 1..50000; timeout-ms must be 1..60000")
    try:
        snapshot = Collector(args.timeout_ms).collect(args.target, args.database, args.limit, args.privileged)
        write_new(args.output, json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    except CollectionError as exc:
        parser.exit(2, f"Collection failed: {exc}\n")
    except OSError:
        parser.exit(2, "Could not create a new output file.\n")


if __name__ == "__main__":
    main()
