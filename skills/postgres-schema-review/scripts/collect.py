#!/usr/bin/env python3
"""Bounded catalog-only schema snapshot via psql. Python standard library."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SECTIONS = ("tables", "columns", "constraints", "indexes", "statistics")


class CollectionError(Exception):
    pass


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def build_query(schemas, limit):
    # schema names are data literals; psql variables and identifiers are never interpolated.
    selected = ",".join(literal(s) for s in schemas)
    queries = {
        "tables": """SELECT t.relation, t.schema, t.relname AS name, t.relkind AS kind,
          t.relispartition AS partition,
          EXISTS (SELECT 1 FROM pg_inherits i JOIN pg_class child ON child.oid=i.inhrelid
                  WHERE i.inhparent=t.oid AND NOT child.relispartition) AS has_inheritance_children
          FROM targets t ORDER BY t.relation""",
        "columns": """SELECT t.relation, a.attname AS name,
          format_type(a.atttypid,a.atttypmod) AS type,
          a.attnotnull AS column_not_null,
          COALESCE((
            WITH RECURSIVE chain AS (
              SELECT ty.oid,ty.typbasetype,ty.typnotnull FROM pg_type ty WHERE ty.oid=a.atttypid
              UNION ALL SELECT ty.oid,ty.typbasetype,ty.typnotnull
              FROM pg_type ty JOIN chain ch ON ch.typbasetype=ty.oid
            ) SELECT bool_or(typnotnull) FROM chain),false) AS domain_not_null,
          (WITH RECURSIVE chain AS (
            SELECT ty.oid,ty.typbasetype FROM pg_type ty WHERE ty.oid=a.atttypid
            UNION ALL SELECT ty.oid,ty.typbasetype FROM pg_type ty JOIN chain ch ON ch.typbasetype=ty.oid
          ) SELECT EXISTS(SELECT 1 FROM pg_constraint dc JOIN chain ch ON dc.contypid=ch.oid
                          WHERE dc.contype='c')) AS domain_has_check,
          a.atthasdef AS has_default, a.attidentity AS identity, a.attgenerated AS generated
          FROM targets t JOIN pg_attribute a ON a.attrelid=t.oid
          WHERE a.attnum>0 AND NOT a.attisdropped ORDER BY t.relation,a.attnum""",
        "constraints": """SELECT t.relation, c.conname AS name, c.contype AS type,
          COALESCE((SELECT jsonb_agg(a.attname ORDER BY k.ord)
            FROM unnest(c.conkey) WITH ORDINALITY k(attnum,ord)
            JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum),'[]'::jsonb) AS columns,
          CASE WHEN c.contype='f' THEN format('%I.%I', rn.nspname,rc.relname) END AS referenced_relation,
          COALESCE((SELECT jsonb_agg(a.attname ORDER BY k.ord)
            FROM unnest(c.confkey) WITH ORDINALITY k(attnum,ord)
            JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.attnum),'[]'::jsonb) AS referenced_columns,
          c.convalidated AS validated, c.conenforced AS enforced, c.connoinherit AS no_inherit,
          CASE WHEN c.contype='f' THEN c.confmatchtype END AS match_type,
          CASE WHEN c.contype='f' THEN c.confupdtype END AS update_action,
          CASE WHEN c.contype='f' THEN c.confdeltype END AS delete_action
          FROM targets t JOIN pg_constraint c ON c.conrelid=t.oid
          LEFT JOIN pg_class rc ON rc.oid=c.confrelid
          LEFT JOIN pg_namespace rn ON rn.oid=rc.relnamespace
          WHERE c.contype IN ('p','u','f','c','n') ORDER BY t.relation,c.conname,c.oid""",
        "indexes": """SELECT t.relation, ic.relname AS name,
          COALESCE((SELECT jsonb_agg(a.attname ORDER BY k.ord)
            FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
            LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
            WHERE k.ord<=i.indnkeyatts),'[]'::jsonb) AS columns,
          i.indisunique AS unique, i.indisvalid AS valid,
          i.indpred IS NOT NULL AS partial, i.indexprs IS NOT NULL AS expression
          FROM targets t JOIN pg_index i ON i.indrelid=t.oid
          JOIN pg_class ic ON ic.oid=i.indexrelid ORDER BY t.relation,ic.relname""",
        "statistics": """SELECT t.relation, s.attname AS column, s.null_frac
          FROM targets t JOIN pg_stats s ON s.schemaname=t.schema AND s.tablename=t.relname
          WHERE NOT s.inherited ORDER BY t.relation,s.attname"""
    }
    # Limit+1 for EVERY section makes truncation observable, including table inventory.
    sections = ",\n".join(
        literal(name) + ", (SELECT COALESCE(jsonb_agg(to_jsonb(q)),'[]'::jsonb) FROM ("
        + query + f" LIMIT {limit + 1}) q)"
        for name, query in queries.items()
    )
    return f"""WITH targets AS MATERIALIZED (
       SELECT c.oid,c.relname,c.relkind,c.relispartition,n.nspname AS schema,
              format('%I.%I',n.nspname,c.relname) AS relation
       FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname IN ({selected}) AND c.relkind IN ('r','p')
       ORDER BY n.nspname,c.relname LIMIT {limit + 1}
    )
    SELECT jsonb_build_object('metadata',jsonb_build_object(
      'database',current_database(),'session_role',session_user,
      'server_version_num',current_setting('server_version_num')::integer,
      'server_version',version(),'collected_at',transaction_timestamp(),
      'in_recovery',pg_is_in_recovery()
    ),'rows',jsonb_build_object({sections}))"""


def run_psql(args, sql):
    command = [args.psql, "-X", "-q", "-A", "-t", "--no-password",
               "--set=ON_ERROR_STOP=1", "--set=VERBOSITY=sqlstate"]
    if args.docker_container:
        command = ["docker", "exec", "-i", "-e", "PGAPPNAME=postgres-schema-review",
                   "-e", "PGCLIENTENCODING=UTF8", "-e", "PGCONNECT_TIMEOUT=10",
                   args.docker_container] + command
    env = dict(os.environ)
    env.update(PGAPPNAME="postgres-schema-review", PGCLIENTENCODING="UTF8")
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    wrapped = (
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        "SET LOCAL search_path=pg_catalog;\nSET LOCAL standard_conforming_strings=on;\n"
        "SET LOCAL timezone='UTC';\n"
        f"SET LOCAL statement_timeout='{args.statement_timeout_ms}ms';\n"
        f"SET LOCAL lock_timeout='{args.lock_timeout_ms}ms';\n"
        + sql.rstrip(";\n") + ";\nCOMMIT;\n"
    )
    try:
        result = subprocess.run(command, input=wrapped, text=True, encoding="utf-8",
                                capture_output=True, env=env,
                                timeout=args.statement_timeout_ms / 1000 + 15)
    except FileNotFoundError as exc:
        raise CollectionError("psql_or_docker_not_found") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectionError("client_timeout") from exc
    if result.returncode:
        states = re.findall(r"(?:ERROR|FATAL):\s+([0-9A-Z]{5})(?:\s|$)", result.stderr)
        raise CollectionError("sqlstate_" + states[-1] if states else "psql_failed")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise CollectionError("invalid_psql_json") from exc


def collect(args):
    result = run_psql(args, build_query(args.schema, args.row_limit))
    major = result["metadata"]["server_version_num"] // 10000
    if major not in (18, 19):
        raise CollectionError("supported_majors_are_18_and_19")
    document = {"schema_version": 2, "metadata": result["metadata"], "sections": {}}
    document["metadata"].update(requested_schemas=args.schema, row_limit=args.row_limit)
    for name in SECTIONS:
        rows = result["rows"][name]
        document["sections"][name] = {
            "status": "ok", "truncated": len(rows) > args.row_limit,
            "rows": rows[:args.row_limit],
        }
    document["sections"]["statistics"]["visibility"] = "role_visible_analyzed_columns_only"
    # Stats may be absent due to privileges, RLS, ANALYZE or partition/inheritance semantics.
    seen = {(r["relation"], r["column"]) for r in document["sections"]["statistics"]["rows"]}
    document["sections"]["statistics"]["missing_columns"] = sum(
        (c["relation"], c["name"]) not in seen for c in document["sections"]["columns"]["rows"])
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", action="append", help="exact schema name, repeatable; default public")
    parser.add_argument("--row-limit", type=positive, default=10000)
    parser.add_argument("--statement-timeout-ms", type=positive, default=5000)
    parser.add_argument("--lock-timeout-ms", type=positive, default=500)
    parser.add_argument("--psql", default="psql")
    parser.add_argument("--docker-container", help="existing isolated test container")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.schema = args.schema or ["public"]
    if any(not s or "\x00" in s for s in args.schema):
        parser.error("schema names must be nonempty and contain no NUL")
    try:
        document = collect(args)
    except CollectionError as exc:
        # Keep errors useful without returning raw server text or connection secrets.
        document = {"schema_version": 2,
                    "metadata": {"collected_at": datetime.now(timezone.utc).isoformat(),
                                 "requested_schemas": args.schema},
                    "sections": {name: {"status": "error", "reason": str(exc),
                                        "truncated": False, "rows": []} for name in SECTIONS}}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print("schema collection failed: " + str(exc), file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
