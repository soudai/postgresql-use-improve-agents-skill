#!/usr/bin/env python3
"""Sample pg_stat_activity wait events over time (ASH-style) through one psql session; stdlib only."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SCHEMA_VERSION = 2
MARKER = "/* postgres-performance-diagnostics */"
WORKERS = ("client backend", "parallel worker")
# One autocommit statement per sample: backend status is re-read in every new transaction.
SAMPLE_SQL = MARKER + """ SELECT jsonb_build_object(
  'at', clock_timestamp(),
  'track_activities', pg_catalog.current_setting('track_activities'),
  'sessions', COALESCE((SELECT jsonb_agg(jsonb_build_object(
      'pid', a.pid, 'leader_pid', a.leader_pid, 'backend_type', a.backend_type,
      'datname', a.datname, 'application_name', a.application_name,
      'state', a.state, 'wait_event_type', a.wait_event_type, 'wait_event', a.wait_event,
      'query_id', a.query_id::text,
      'xact_age_s', EXTRACT(epoch FROM clock_timestamp() - a.xact_start),
      'query_age_s', EXTRACT(epoch FROM clock_timestamp() - a.query_start),
      'blocking_pids', CASE WHEN a.wait_event_type = 'Lock'
                            THEN pg_catalog.pg_blocking_pids(a.pid) END) ORDER BY a.pid)
    FROM pg_catalog.pg_stat_activity AS a
    WHERE a.pid <> pg_catalog.pg_backend_pid()
      AND a.state IS DISTINCT FROM 'idle'
      AND a.wait_event_type IS DISTINCT FROM 'Activity'), '[]'::jsonb))"""
LIMITATIONS = [
    "Samples are point-in-time. Short waits between samples are invisible; counts estimate time share, not exact durations.",
    "active with no wait event means CPU or an uninstrumented wait; confirm CPU saturation with OS metrics.",
    "Sessions waiting in a connection pool before reaching PostgreSQL are not visible here.",
    "query_id needs compute_query_id; utility statements and some protocol phases may have none.",
    "Hidden backend state/type and disabled activity tracking make AAS unknown; observed counts are lower bounds.",
    "Wait/query shares use observed active sessions only, not hidden sessions.",
    "The sampler itself is one extra session; keep intervals >= 1s on production systems.",
]


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def interval(value: str) -> float:
    number = float(value)
    if not 0.1 <= number <= 60:
        raise argparse.ArgumentTypeError("must be between 0.1 and 60 seconds")
    return number


class SamplingError(Exception):
    pass


def script(args: argparse.Namespace) -> str:
    timeout_ms = max(args.statement_timeout_ms, int(args.interval * 1000) + 1000)
    lines = ["SET default_transaction_read_only = on;", "SET search_path = pg_catalog;",
             f"SET statement_timeout = '{timeout_ms}ms';", f"SET lock_timeout = '{args.lock_timeout_ms}ms';",
             "SET timezone = 'UTC';"]
    for index in range(args.samples):
        if index:
            lines.append(f"SELECT pg_catalog.pg_sleep({args.interval});")
        lines.append(SAMPLE_SQL + ";")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> tuple[list, str | None]:
    command = [args.psql, "-X", "-q", "-A", "-t", "--no-password",
               "--set=ON_ERROR_STOP=1", "--set=VERBOSITY=sqlstate"]
    env = dict(os.environ)
    env["PGAPPNAME"] = "postgres-performance-diagnostics"
    env["PGCLIENTENCODING"] = "UTF8"
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    if args.docker_container:
        command = ["docker", "exec", "-i", "-e", "PGAPPNAME=postgres-performance-diagnostics",
                   "-e", "PGCLIENTENCODING=UTF8", "-e", "PGCONNECT_TIMEOUT=10",
                   args.docker_container] + command
    budget = args.samples * (args.interval + args.statement_timeout_ms / 1000) + 30
    try:
        result = subprocess.run(command, input=script(args), text=True, encoding="utf-8",
                                capture_output=True, env=env, timeout=budget, check=False)
    except FileNotFoundError as exc:
        raise SamplingError("psql or docker executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise SamplingError("client_timeout") from exc
    samples = []
    for line in result.stdout.splitlines():
        if line.startswith("{"):
            try:
                samples.append(json.loads(line))
            except ValueError:
                continue
    error = None
    if result.returncode:
        # Keep only SQLSTATE: server messages can contain SQL literals or connection details.
        states = re.findall(r"(?:ERROR|FATAL):\s+([0-9A-Z]{5})(?:\s|$)", result.stderr)
        error = "sqlstate_" + states[-1] if states else "psql_failed"
    return samples, error


def wait_label(session: dict) -> str:
    if not session.get("wait_event_type"):
        return "No reported wait event"
    return f"{session.get('wait_event_type')}:{session.get('wait_event')}"


def summarize(samples: list, top: int = 20) -> dict:
    count = len(samples)
    waits, background, idle_tx, blockers, queries = Counter(), Counter(), Counter(), Counter(), {}
    active_per_sample, unknown_per_sample, max_idle_tx_age = [], [], None
    incomplete_reasons = set()
    for sample in samples:
        active, unknown = 0, 0
        if sample.get("track_activities") != "on":
            incomplete_reasons.add("Sampler track_activities is disabled or was not captured.")
        if not isinstance(sample.get("sessions"), list):
            incomplete_reasons.add("Session rows are missing.")
        for session in sample.get("sessions") or []:
            kind = session.get("backend_type")
            state = session.get("state")
            # PostgreSQL masks both fields for other roles; do not count these as background.
            # track_activities can also be disabled per backend, regardless of our own setting.
            if kind is None or (kind in WORKERS and state in (None, "disabled")):
                unknown += 1
                incomplete_reasons.add("Some backend state/type is hidden or activity tracking is disabled.")
                continue
            if (session.get("state") or "").startswith("idle in transaction"):
                idle_tx[session.get("datname")] += 1
                age = session.get("xact_age_s")
                if isinstance(age, (int, float)):
                    max_idle_tx_age = max(max_idle_tx_age or 0, age)
                continue
            if kind not in WORKERS:
                background[(kind, wait_label(session))] += 1
                continue
            if state not in ("active", "fastpath function call"):
                continue
            active += 1
            label = wait_label(session)
            waits[label] += 1
            query = queries.setdefault(session.get("query_id") or "unknown", Counter())
            query[label] += 1
            for pid in session.get("blocking_pids") or []:
                blockers[pid] += 1
        active_per_sample.append(active)
        unknown_per_sample.append(unknown)
    complete = count > 0 and not incomplete_reasons
    total = sum(waits.values())

    def share(value):
        return value / total if total else None

    return {
        "samples": count,
        "average_active_sessions": total / count if complete else None,
        "peak_active_sessions": max(active_per_sample) if complete else None,
        "observed_average_active_sessions": total / count if count else None,
        "observed_peak_active_sessions": max(active_per_sample, default=None),
        "activity_coverage": "complete" if complete else "incomplete",
        "unknown_session_samples": sum(unknown_per_sample),
        "samples_with_unknown_sessions": sum(value > 0 for value in unknown_per_sample),
        "peak_unknown_sessions": max(unknown_per_sample, default=None),
        "wait_profile_share_scope": "observed_active_sessions",
        "warnings": sorted(incomplete_reasons),
        "wait_profile": [{"wait": label, "samples": value, "average_sessions": value / count, "share": share(value)}
                         for label, value in waits.most_common(top)],
        "by_query_id": [{"query_id": query_id, "samples": sum(labels.values()),
                         "average_sessions": sum(labels.values()) / count, "share": share(sum(labels.values())),
                         "top_waits": dict(labels.most_common(3))}
                        for query_id, labels in sorted(queries.items(), key=lambda x: sum(x[1].values()),
                                                       reverse=True)[:top]],
        "lock_blockers": [{"pid": pid, "waiter_samples": value} for pid, value in blockers.most_common(top)],
        "idle_in_transaction": {"session_samples": sum(idle_tx.values()), "by_database": dict(idle_tx),
                                "max_xact_age_s": max_idle_tx_age},
        "background": [{"backend_type": kind, "wait": label, "samples": value}
                       for (kind, label), value in background.most_common(top)],
    }


def sample(args: argparse.Namespace) -> dict:
    samples, error = run(args)
    if not samples:
        raise SamplingError(error or "no_samples")
    report = {"schema_version": SCHEMA_VERSION, "kind": "activity_samples", "instance_id": args.instance_id,
              "requested_samples": args.samples, "interval_seconds": args.interval,
              "first_sample_at": samples[0].get("at"), "last_sample_at": samples[-1].get("at"),
              "error": error, "summary": summarize(samples, args.top), "limitations": LIMITATIONS}
    if args.include_sessions:
        report["raw_samples"] = samples
    return report


def markdown(report: dict) -> str:
    summary = report["summary"]
    aas = summary["average_active_sessions"] if summary["average_active_sessions"] is not None else "unknown"
    peak = summary["peak_active_sessions"] if summary["peak_active_sessions"] is not None else "unknown"
    lines = ["# PostgreSQL activity samples", "",
             f"Instance: {report['instance_id']}; samples: {summary['samples']}/{report['requested_samples']} "
             f"every {report['interval_seconds']}s; {report['first_sample_at']} → {report['last_sample_at']}",
             f"Average active sessions: {aas}; peak: {peak}", ""]
    if summary["activity_coverage"] != "complete":
        lines += ["Activity coverage: incomplete; AAS and peak are unknown.",
                  f"Observed lower bounds: AAS {summary['observed_average_active_sessions']}; "
                  f"peak {summary['observed_peak_active_sessions']}; unknown session-samples "
                  f"{summary['unknown_session_samples']}.",
                  "Wait/query shares below describe observed active sessions only.", ""]
        lines += ["- " + warning for warning in summary["warnings"]] + [""]
    if report.get("error"):
        lines += [f"Sampling stopped early: {report['error']}", ""]
    lines += ["## Wait profile (client backends and parallel workers)", "",
              "| Wait | Samples | Avg sessions | Share |", "|---|---:|---:|---:|"]
    lines += [f"| {x['wait']} | {x['samples']} | {x['average_sessions']:.2f} | {x['share']:.0%} |"
              for x in summary["wait_profile"]]
    lines += ["", "## By query_id", "", "| query_id | Samples | Avg sessions | Share | Top waits |",
              "|---|---:|---:|---:|---|"]
    lines += [f"| {x['query_id']} | {x['samples']} | {x['average_sessions']:.2f} | {x['share']:.0%} | "
              + json.dumps(x["top_waits"], ensure_ascii=False).replace("|", "\\|") + " |"
              for x in summary["by_query_id"]]
    lines += ["", "## Lock blockers and idle transactions", "",
              "Blockers: " + json.dumps(summary["lock_blockers"], ensure_ascii=False),
              "Idle in transaction: " + json.dumps(summary["idle_in_transaction"], ensure_ascii=False),
              "", "## Background processes", ""]
    lines += [f"- {x['backend_type']}: {x['wait']} × {x['samples']}" for x in summary["background"]]
    lines += ["", "## Limits", ""] + ["- " + item for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id", required=True, help="Stable cluster/node identity, never a secret")
    p.add_argument("--output", required=True, type=Path, help="New report file; never overwritten")
    p.add_argument("--samples", type=positive, default=60)
    p.add_argument("--interval", type=interval, default=1.0, help="Seconds between samples (0.1-60)")
    p.add_argument("--format", choices=("json", "markdown"), default="json")
    p.add_argument("--top", type=positive, default=20)
    p.add_argument("--include-sessions", action="store_true",
                   help="Keep per-sample sessions (application/database names; no SQL text)")
    p.add_argument("--psql", default="psql", help="psql executable; connect with libpq environment/service")
    p.add_argument("--docker-container", help="Run psql inside an existing explicitly selected container")
    p.add_argument("--statement-timeout-ms", type=positive, default=5000)
    p.add_argument("--lock-timeout-ms", type=positive, default=1000)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.output.exists():
        print("error: output already exists; choose a new path", file=sys.stderr)
        return 2
    try:
        report = sample(args)
        text = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                if args.format == "json" else markdown(report))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except (SamplingError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Saved samples: {args.output}; obtained {report['summary']['samples']}/{args.samples}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
