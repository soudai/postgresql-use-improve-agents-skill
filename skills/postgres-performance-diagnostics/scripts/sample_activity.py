#!/usr/bin/env python3
"""Sample pg_stat_activity wait events over time (ASH-style) through one psql session; stdlib only."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time

SCHEMA_VERSION = 2
MARKER = "/* postgres-performance-diagnostics */"
WORKERS = ("client backend", "parallel worker")
NO_WAIT = "No reported wait event"
TIMELINE_BUCKETS = 120
# One autocommit statement per sample: backend status is re-read in every new transaction.
# The session is idle between samples (the client paces them), so the sampler adds neither
# active time nor a pg_sleep entry to the statistics it is used to explain.
# The marker follows SELECT: pg_stat_statements drops a comment that precedes the statement,
# and collect.py's pgss.sql uses the marker to leave this statement out of SQL rankings.
SAMPLE_SQL = "SELECT " + MARKER + """ jsonb_build_object(
  'at', clock_timestamp(),
  'track_activities', pg_catalog.current_setting('track_activities'),
  'max_connections', pg_catalog.current_setting('max_connections')::integer,
  'client_backends', (SELECT count(*) FROM pg_catalog.pg_stat_activity AS c
                      WHERE c.backend_type = 'client backend'),
  'sessions', COALESCE((SELECT jsonb_agg(jsonb_build_object(
      'pid', a.pid, 'leader_pid', a.leader_pid, 'backend_type', a.backend_type,
      'datname', a.datname, 'usename', a.usename, 'application_name', a.application_name,
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
    "The sampler is one extra session that is idle between samples; keep intervals >= 1s on production systems.",
    "Database, role and application names appear in the summary; share it accordingly.",
]
# Coarse, fixed reasons for failures without a SQLSTATE; the matched text is never stored.
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


def setup_script(args: argparse.Namespace) -> str:
    """Session settings, run once. They are not marked: SET shares its query ID with the
    application's own SET statements, and a marker stored first would hide those as well."""
    statements = ["SET default_transaction_read_only = on", "SET search_path = pg_catalog",
                  f"SET statement_timeout = '{args.statement_timeout_ms}ms'",
                  f"SET lock_timeout = '{args.lock_timeout_ms}ms'",
                  # The session idles for up to one interval between samples.
                  "SET idle_session_timeout = 0", "SET timezone = 'UTC'"]
    return "".join(f"{statement};\n" for statement in statements)


def command_line(args: argparse.Namespace) -> tuple[list, dict]:
    command = [args.psql, "-X", "-q", "-A", "-t", "--no-password",
               "--set=ON_ERROR_STOP=1", "--set=VERBOSITY=sqlstate"]
    env = dict(os.environ)
    env["PGAPPNAME"] = "postgres-performance-diagnostics"
    env["PGCLIENTENCODING"] = "UTF8"
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    env["LC_MESSAGES"] = "C"
    if args.docker_container:
        command = ["docker", "exec", "-i", "-e", "PGAPPNAME=postgres-performance-diagnostics",
                   "-e", "PGCLIENTENCODING=UTF8", "-e", "PGCONNECT_TIMEOUT=10",
                   args.docker_container] + command
    return command, env


def _pump(stream, sink) -> None:
    """Forward lines until EOF; None marks the end of the stream."""
    try:
        for line in stream:
            sink.put(line)
    except (OSError, ValueError):
        pass
    finally:
        sink.put(None)


def _next_sample(lines, timeout: float, clock):
    """Next JSON object psql printed: (sample, None), or (None, 'eof' | 'client_timeout')."""
    deadline = clock() + timeout
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return None, "client_timeout"
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            return None, "client_timeout"
        if line is None:
            return None, "eof"
        if line.startswith("{"):
            try:
                return json.loads(line), None
            except ValueError:
                continue


def run(args: argparse.Namespace, *, popen=None, clock=None, sleep=None) -> tuple[list, str | None, dict]:
    """Drive one psql session, pacing samples on the client. Returns samples, error and pacing facts."""
    popen, clock, sleep = popen or subprocess.Popen, clock or time.monotonic, sleep or time.sleep
    command, env = command_line(args)
    try:
        process = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", env=env)
    except FileNotFoundError as exc:
        raise SamplingError("psql or docker executable not found") from exc
    lines, errors = queue.Queue(), queue.Queue()
    readers = [threading.Thread(target=_pump, args=(process.stdout, lines), daemon=True),
               threading.Thread(target=_pump, args=(process.stderr, errors), daemon=True)]
    for reader in readers:
        reader.start()
    samples, error, late = [], None, 0
    statement_seconds = args.statement_timeout_ms / 1000
    started = clock()
    try:
        process.stdin.write(setup_script(args))
        due = started
        for index in range(args.samples):
            wait = due - clock()
            if wait > 0:
                sleep(wait)
            elif index:
                late += 1  # The previous sample took longer than the interval.
            process.stdin.write(SAMPLE_SQL + ";\n")
            process.stdin.flush()
            # The first answer also covers connecting; later ones only the statement.
            sample, problem = _next_sample(lines, statement_seconds + (20 if index == 0 else 5), clock)
            if sample is None:
                error = None if problem == "eof" else problem
                break
            samples.append(sample)
            # Keep a fixed cadence; after a slow sample (or the connecting one) restart from now
            # instead of firing a burst to catch up.
            now = clock()
            due = now + args.interval if index == 0 else max(due + args.interval, now)
    except KeyboardInterrupt:
        error = "interrupted"
    except (BrokenPipeError, OSError, ValueError):
        pass  # psql ended; its exit status and stderr name the reason below.
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            process.wait(timeout=5 if error else statement_seconds + 10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for reader in readers:
        reader.join(timeout=5)
    stderr = []
    while True:
        try:
            line = errors.get_nowait()
        except queue.Empty:
            break
        if line is not None:
            stderr.append(line)
    if error is None and process.returncode:
        # Keep only SQLSTATE or a fixed category: messages can contain SQL literals or connection details.
        error = failure_reason("".join(stderr))
    elif error is None and len(samples) < args.samples:
        error = "psql_exited_early"
    pacing = {"elapsed_seconds": clock() - started, "late_samples": late}
    return samples, error, pacing


def wait_label(session: dict) -> str:
    if not session.get("wait_event_type"):
        return NO_WAIT
    return f"{session.get('wait_event_type')}:{session.get('wait_event')}"


def timeline(samples: list, active: list, lock_waiting: list, unknown: list) -> list:
    """At most TIMELINE_BUCKETS rows: when the load peaked, without one row per sample."""
    size = max(1, -(-len(samples) // TIMELINE_BUCKETS))
    rows = []
    for start in range(0, len(samples), size):
        end = min(start + size, len(samples))
        rows.append({"from": samples[start].get("at"), "to": samples[end - 1].get("at"), "samples": end - start,
                     "average_active": sum(active[start:end]) / (end - start),
                     "peak_active": max(active[start:end]),
                     "peak_lock_waiting": max(lock_waiting[start:end]),
                     "peak_unknown": max(unknown[start:end])})
    return rows


def summarize(samples: list, top: int = 20) -> dict:
    count = len(samples)
    waits, classes, background, idle_tx, blocked = Counter(), Counter(), Counter(), Counter(), Counter()
    queries, dimensions = {}, {"by_database": {}, "by_user": {}, "by_application": {}}
    active_per_sample, unknown_per_sample, lock_per_sample, max_idle_tx_age = [], [], [], None
    root_samples, seen = Counter(), {}
    incomplete_reasons = set()
    for sample in samples:
        active, unknown, lock_waiting = 0, 0, 0
        waiting_here, blocking_here = set(), set()
        if sample.get("track_activities") != "on":
            incomplete_reasons.add("Sampler track_activities is disabled or was not captured.")
        if not isinstance(sample.get("sessions"), list):
            incomplete_reasons.add("Session rows are missing.")
        for session in sample.get("sessions") or []:
            kind = session.get("backend_type")
            state = session.get("state")
            if session.get("pid") is not None:
                age = session.get("xact_age_s")
                info = seen.setdefault(session["pid"], {"max_xact_age_s": None})
                info.update(backend_type=kind, datname=session.get("datname"), last_state=state,
                            last_wait=wait_label(session) if state else None)
                if isinstance(age, (int, float)):
                    info["max_xact_age_s"] = max(info["max_xact_age_s"] or 0, age)
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
            classes[session.get("wait_event_type") or NO_WAIT] += 1
            query = queries.setdefault(session.get("query_id") or "unknown", Counter())
            query[label] += 1
            for name, field in (("by_database", "datname"), ("by_user", "usename"),
                                ("by_application", "application_name")):
                dimensions[name].setdefault(session.get(field) or "unknown", Counter())[label] += 1
            blockers = session.get("blocking_pids") or []
            if blockers:
                lock_waiting += 1
                waiting_here.add(session.get("pid"))
            for pid in blockers:
                blocked[pid] += 1
                blocking_here.add(pid)
        # A root blocker blocks someone in this sample without waiting for a lock itself.
        for pid in blocking_here - waiting_here:
            root_samples[pid] += 1
        active_per_sample.append(active)
        unknown_per_sample.append(unknown)
        lock_per_sample.append(lock_waiting)
    complete = count > 0 and not incomplete_reasons
    total = sum(waits.values())

    def share(value):
        return value / total if total else None

    def breakdown(groups):
        ordered = sorted(groups.items(), key=lambda item: sum(item[1].values()), reverse=True)[:top]
        return [{"name": name, "samples": sum(labels.values()), "average_sessions": sum(labels.values()) / count,
                 "share": share(sum(labels.values())), "top_waits": dict(labels.most_common(3))}
                for name, labels in ordered]

    backends = [s["client_backends"] for s in samples if isinstance(s.get("client_backends"), int)]
    limit = next((s["max_connections"] for s in reversed(samples) if isinstance(s.get("max_connections"), int)), None)
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
        "wait_class_profile": [{"wait_class": label, "samples": value, "average_sessions": value / count,
                                "share": share(value)} for label, value in classes.most_common()],
        "wait_profile": [{"wait": label, "samples": value, "average_sessions": value / count, "share": share(value)}
                         for label, value in waits.most_common(top)],
        "by_query_id": [{"query_id": query_id, "samples": sum(labels.values()),
                         "average_sessions": sum(labels.values()) / count, "share": share(sum(labels.values())),
                         "top_waits": dict(labels.most_common(3))}
                        for query_id, labels in sorted(queries.items(), key=lambda x: sum(x[1].values()),
                                                       reverse=True)[:top]],
        "by_database": breakdown(dimensions["by_database"]),
        "by_user": breakdown(dimensions["by_user"]),
        "by_application": breakdown(dimensions["by_application"]),
        # client_backends counts visible client connections of the whole cluster, idle ones included.
        "connections": {"average_client_backends": sum(backends) / len(backends) if backends else None,
                        "peak_client_backends": max(backends, default=None), "max_connections": limit},
        "lock_blockers": [dict({"pid": pid, "waiter_samples": value, "root_samples": root_samples.get(pid, 0),
                                "prepared_transaction": pid == 0},
                               **{key: seen.get(pid, {}).get(key) for key in
                                  ("backend_type", "datname", "last_state", "last_wait", "max_xact_age_s")})
                          for pid, value in blocked.most_common(top)],
        "idle_in_transaction": {"session_samples": sum(idle_tx.values()), "by_database": dict(idle_tx),
                                "max_xact_age_s": max_idle_tx_age},
        "background": [{"backend_type": kind, "wait": label, "samples": value}
                       for (kind, label), value in background.most_common(top)],
        "timeline": timeline(samples, active_per_sample, lock_per_sample, unknown_per_sample),
    }


def sample(args: argparse.Namespace) -> dict:
    samples, error, pacing = run(args)
    if not samples:
        raise SamplingError(error or "no_samples")
    report = {"schema_version": SCHEMA_VERSION, "kind": "activity_samples", "instance_id": args.instance_id,
              "requested_samples": args.samples, "interval_seconds": args.interval,
              "first_sample_at": samples[0].get("at"), "last_sample_at": samples[-1].get("at"),
              "error": error, "pacing": pacing, "summary": summarize(samples, args.top), "limitations": LIMITATIONS}
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
             f"Average active sessions: {aas}; peak: {peak}",
             "Client connections (cluster): " + json.dumps(summary.get("connections"), ensure_ascii=False), ""]
    if summary["activity_coverage"] != "complete":
        lines += ["Activity coverage: incomplete; AAS and peak are unknown.",
                  f"Observed lower bounds: AAS {summary['observed_average_active_sessions']}; "
                  f"peak {summary['observed_peak_active_sessions']}; unknown session-samples "
                  f"{summary['unknown_session_samples']}.",
                  "Wait/query shares below describe observed active sessions only.", ""]
        lines += ["- " + warning for warning in summary["warnings"]] + [""]
    if report.get("error"):
        lines += [f"Sampling stopped early: {report['error']}", ""]
    if (report.get("pacing") or {}).get("late_samples"):
        lines += [f"{report['pacing']['late_samples']} sample(s) started late: a sample took longer than the interval.", ""]

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    def percent(value):
        return "—" if value is None else f"{value:.0%}"

    lines += ["## Wait classes", "", "| Wait class | Samples | Avg sessions | Share |", "|---|---:|---:|---:|"]
    lines += [f"| {cell(x['wait_class'])} | {x['samples']} | {x['average_sessions']:.2f} | {percent(x['share'])} |"
              for x in summary.get("wait_class_profile", [])]
    lines += ["", "## Wait profile (client backends and parallel workers)", "",
              "| Wait | Samples | Avg sessions | Share |", "|---|---:|---:|---:|"]
    lines += [f"| {cell(x['wait'])} | {x['samples']} | {x['average_sessions']:.2f} | {percent(x['share'])} |"
              for x in summary["wait_profile"]]
    lines += ["", "## By query_id", "", "| query_id | Samples | Avg sessions | Share | Top waits |",
              "|---|---:|---:|---:|---|"]
    lines += [f"| {x['query_id']} | {x['samples']} | {x['average_sessions']:.2f} | {percent(x['share'])} | "
              + cell(json.dumps(x["top_waits"], ensure_ascii=False)) + " |"
              for x in summary["by_query_id"]]
    for key, title in (("by_database", "database"), ("by_user", "role"), ("by_application", "application")):
        lines += ["", f"## By {title}", "", "| Name | Samples | Avg sessions | Share | Top waits |",
                  "|---|---:|---:|---:|---|"]
        lines += [f"| {cell(x['name'])} | {x['samples']} | {x['average_sessions']:.2f} | {percent(x['share'])} | "
                  + cell(json.dumps(x["top_waits"], ensure_ascii=False)) + " |" for x in summary.get(key, [])]
    busiest = sorted(summary.get("timeline", []), key=lambda x: x["peak_active"], reverse=True)[:5]
    lines += ["", "## Busiest moments", "", "| From | To | Avg active | Peak active | Peak lock waiters |",
              "|---|---|---:|---:|---:|"]
    lines += [f"| {x['from']} | {x['to']} | {x['average_active']:.2f} | {x['peak_active']} | {x['peak_lock_waiting']} |"
              for x in busiest]
    lines += ["", "## Lock blockers and idle transactions", "",
              "| Blocker PID | Waiter samples | Samples as root | Last state | Last wait | Max xact age s |",
              "|---:|---:|---:|---|---|---:|"]
    lines += [f"| {x['pid']} | {x['waiter_samples']} | {x.get('root_samples')} | "
              f"{'prepared transaction' if x.get('prepared_transaction') else cell(x.get('last_state'))} | "
              f"{cell(x.get('last_wait'))} | {x.get('max_xact_age_s')} |" for x in summary["lock_blockers"]]
    lines += ["", "Idle in transaction: " + json.dumps(summary["idle_in_transaction"], ensure_ascii=False),
              "", "## Background processes", ""]
    lines += [f"- {x['backend_type']}: {x['wait']} × {x['samples']}" for x in summary["background"]]
    lines += ["", "## Limits", ""] + ["- " + item for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id", required=True, help="Stable cluster/node identity, never a secret")
    p.add_argument("--output", required=True, type=Path, help="New report file; never overwritten")
    p.add_argument("--samples", type=positive, default=60,
                   help="Number of samples; the run takes about samples x interval seconds")
    p.add_argument("--interval", type=interval, default=1.0, help="Seconds between samples (0.1-60)")
    p.add_argument("--format", choices=("json", "markdown"), default="json")
    p.add_argument("--top", type=positive, default=20)
    p.add_argument("--include-sessions", action="store_true",
                   help="Keep per-sample sessions (PIDs, application/database/role names; no SQL text)")
    p.add_argument("--psql", default="psql", help="psql executable; connect with libpq environment/service")
    p.add_argument("--docker-container", help="Run psql inside an existing explicitly selected container")
    p.add_argument("--statement-timeout-ms", type=positive, default=5000)
    p.add_argument("--lock-timeout-ms", type=positive, default=1000)
    return p


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.output.exists():
        print("error: output already exists; choose a new path", file=sys.stderr)
        return 2
    previous = None
    try:
        # A terminated run (timeout wrappers send SIGTERM) still reports the samples it has.
        previous = signal.signal(signal.SIGTERM, _interrupt)
    except (ValueError, OSError):
        pass
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
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    stopped = f"; stopped early: {report['error']}" if report.get("error") else ""
    print(f"Saved samples: {args.output}; obtained {report['summary']['samples']}/{args.samples}{stopped}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
