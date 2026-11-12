#!/usr/bin/env python3
"""Summarize PostgreSQL JSON log events offline; never execute logged SQL."""

from __future__ import annotations

import argparse
from collections import Counter
import heapq
import json
import math
from pathlib import Path
import re
import sys


TEMP = re.compile(r'^temporary file: path ".*", size ([0-9]+)\s*$', re.DOTALL)
DURATION = re.compile(r'^duration:\s+([0-9]+(?:\.[0-9]+)?)\s+ms\b(.*)', re.DOTALL)
LOCK = re.compile(r'^process [0-9]+ (still waiting for|acquired) .+ after ([0-9]+(?:\.[0-9]+)?) ms(?:\s|$)', re.DOTALL)
LOCK_TARGET = re.compile(r"^process [0-9]+ (?:still waiting for|acquired) (\S+) on (\S+)")
LOCK_HOLDERS = re.compile(r"Process(?:es)? holding the lock: ([0-9, ]+)\. Wait queue: ([0-9, ]*)")
# PostgreSQL 15+ names the process type ("client backend (PID ...)"); older servers say "server process".
KILLED = re.compile(r"^(.+?) \(PID [0-9]+\) was terminated by signal ([0-9]+)")
# 18 logs "checkpoint complete: wrote ..."; 19 repeats the flags: "checkpoint complete: time: wrote ...".
CHECKPOINT = re.compile(r"^(checkpoint|restartpoint) (starting|complete):\s*(.*)$", re.DOTALL)
CHECKPOINT_FLAGS = re.compile(r"^([a-z][a-z \-]*?):\s+wrote\b")
CHECKPOINT_NUMBERS = {
    "buffers_written": (r"wrote ([0-9]+) buffers", int),
    "wal_files_added": (r"([0-9]+) WAL file\(s\) added", int),
    "wal_files_removed": (r"added, ([0-9]+) removed", int),
    "wal_files_recycled": (r"([0-9]+) recycled", int),
    "write_s": (r"write=([0-9.]+) s", float),
    "sync_s": (r"sync=([0-9.]+) s", float),
    "total_s": (r"total=([0-9.]+) s", float),
    "longest_sync_s": (r"longest=([0-9.]+) s", float),
    "distance_kb": (r"distance=([0-9]+) kB", int),
    "estimate_kb": (r"estimate=([0-9]+) kB", int),
}
TOO_FREQUENT = re.compile(r"^checkpoints are occurring too frequently \(([0-9]+) seconds? apart\)")
AUTOVACUUM = re.compile(r'^automatic (aggressive )?vacuum (to prevent wraparound )?of table "(.+?)": index scans: ([0-9]+)',
                        re.DOTALL)
AUTOANALYZE = re.compile(r'^automatic analyze of table "(.+?)"(?:\n|$)', re.DOTALL)
VACUUM_NUMBERS = {
    "pages_scanned": (r"remain, ([0-9]+) scanned", int),
    "tuples_removed": (r"tuples: ([0-9]+) removed", int),
    "tuples_remain": (r"removed, ([0-9]+) remain, [0-9]+ are dead", int),
    "dead_not_removable": (r"([0-9]+) are dead but not yet removable", int),
    "tuples_missed": (r"tuples missed: ([0-9]+) dead", int),
    "cutoff_xids_old": (r"which was ([0-9]+) XIDs old", int),
    "wal_bytes": (r"WAL usage: [0-9]+ records, [0-9]+ full page images, ([0-9]+) bytes", int),
    "delay_ms": (r"delay time: ([0-9.]+) ms", float),
    "elapsed_s": (r"elapsed: ([0-9.]+) s", float),
}
# SQLSTATEs that matter for capacity, locking and timeouts. They work in every lc_messages.
STATE_KINDS = {
    "57014": "statement_canceled", "55P03": "lock_not_available", "25P03": "idle_in_transaction_timeout",
    "25P04": "transaction_timeout", "57P05": "idle_session_timeout", "53300": "too_many_connections",
    "53100": "disk_full", "53200": "out_of_memory", "53400": "configuration_limit_exceeded",
    "40001": "serialization_failure", "57P01": "terminated_by_administrator",
}
MAX_LINE_CHARS = 2 * 1024 * 1024
MAX_GROUPS = 1000
LIMITATIONS = [
    "Input must be PostgreSQL jsonlog (one JSON object per line), from one server and a known observation window.",
    "English message patterns are recognized; localized messages may remain unclassified. SQLSTATE-based kinds (deadlock, timeouts, connection limit, disk full) work across languages.",
    "Durations are observed log events only. Sampling and duration thresholds cannot be inferred from a record; no population percentiles are calculated.",
    "Parse, bind, execute and statement durations are separate phases and must not be counted as independent completed queries or blindly summed.",
    "Temp file sizes are logged at deletion; they are not instantaneous disk use or per-table attribution.",
    "Events are associated with query_id only when that same record supplies it; previous SQL on a PID is never used to guess attribution.",
    "Lock wait logs cover waits exceeding the configured threshold, not all waits. Deadlock logs need application transaction context for a full causal analysis.",
    "Checkpoint and autovacuum summaries exist only when log_checkpoints and log_autovacuum_min_duration record them; a threshold hides shorter runs.",
    "A forced checkpoint is a CHECKPOINT command, a base backup or similar; only cause 'wal' means WAL volume reached max_wal_size.",
    "No matching events is not proof of a healthy database. Log settings, missing files and rotation determine coverage.",
    "Messages, SQL and details are omitted by default; database/user/application/session names, table names and identifiers remain visible.",
    "Extracted auto_explain plans contain the SQL text and literals; they are written only when a directory is given.",
]


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def query_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value else None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,20}", value):
        return str(int(value)) if int(value) else None
    return None


def numbers(patterns, text):
    """Named numeric fields found in a log message; absent ones are left out, never zero."""
    found = {}
    for name, (pattern, convert) in patterns.items():
        match = re.search(pattern, text)
        if match:
            try:
                value = convert(match.group(1))
            except ValueError:
                continue
            if math.isfinite(value):
                found[name] = value
    return found


def pids(text):
    return [int(value) for value in re.findall(r"[0-9]+", text)]


def checkpoint_cause(flags):
    """Why a checkpoint started. 'wal' is the only cause that points at max_wal_size."""
    for flag, cause in (("shutdown", "shutdown"), ("end-of-recovery", "end_of_recovery"), ("wal", "wal"),
                        ("force", "forced"), ("time", "time")):
        if flag in flags:
            return cause
    return "other"


def auto_explain_plan(text):
    """The JSON object auto_explain logged after 'plan:', or None for text plans and damage."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        plan = json.loads(text[start:], parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return None
    return plan if isinstance(plan, dict) and isinstance(plan.get("Plan"), dict) else None


def observed_event(record, line_number, include_details):
    message = record.get("message", "")
    if not isinstance(message, str):
        message = ""
    state = record.get("state_code", record.get("sql_state_code"))
    event = None
    if state == "40P01" or message == "deadlock detected":
        event = {"kind": "deadlock"}
    elif (match := TEMP.match(message)):
        event = {"kind": "temp_file", "bytes": int(match.group(1))}
    elif (match := LOCK.match(message)):
        wait_ms = float(match.group(2))
        if math.isfinite(wait_ms):
            event = {"kind": "lock_wait" if match.group(1) == "still waiting for" else "lock_acquired",
                     "wait_ms": wait_ms}
            target = LOCK_TARGET.match(message)
            if target:
                # e.g. ShareLock on "transaction" (a row lock held by another transaction) or "relation".
                event.update(lock_mode=target.group(1), lock_target=target.group(2))
            detail = record.get("detail")
            holders = LOCK_HOLDERS.search(detail) if isinstance(detail, str) else None
            if holders:
                # PIDs name sessions, not data: they connect the log to the blocker's own records.
                event.update(holder_pids=pids(holders.group(1)), wait_queue_pids=pids(holders.group(2)))
    elif (match := DURATION.match(message)):
        duration = float(match.group(1))
        rest = match.group(2).lstrip()
        phase = "unspecified"
        for candidate in ("statement", "execute", "parse", "bind", "plan"):
            if re.match(r"^" + candidate + r"(?:\s|:)", rest):
                phase = candidate
                break
        if math.isfinite(duration):
            event = {"kind": "duration", "phase": phase, "duration_ms": duration,
                     "selection": "sampling_or_threshold_unknown"}
            if phase == "plan":
                body = rest[len("plan"):].lstrip(": \t\r\n")
                event["plan_format"] = "json" if body.startswith("{") else "text"
                if event["plan_format"] == "json":
                    event["_plan_text"] = body  # Held for extraction only; never part of the report.
    elif (match := AUTOVACUUM.match(message)):
        event = {"kind": "autovacuum", "table": match.group(3), "aggressive": bool(match.group(1)),
                 "to_prevent_wraparound": bool(match.group(2)), "index_scans": int(match.group(4))}
        event.update(numbers(VACUUM_NUMBERS, message))
    elif message.startswith("automatic vacuum of table ") or message.startswith("automatic aggressive vacuum of table "):
        event = {"kind": "autovacuum"}
    elif (match := AUTOANALYZE.match(message)):
        event = {"kind": "autoanalyze", "table": match.group(1)}
        event.update({k: v for k, v in numbers(VACUUM_NUMBERS, message).items() if k in ("elapsed_s", "wal_bytes", "delay_ms")})
    elif message.startswith("automatic analyze of table "):
        event = {"kind": "autoanalyze"}
    elif (match := CHECKPOINT.match(message)):
        process, stage, rest = match.groups()
        if stage == "starting":
            flags = rest.split()
            event = {"kind": f"{process}_start", "flags": flags, "cause": checkpoint_cause(flags)}
        else:
            event = {"kind": f"{process}_complete"}
            flagged = CHECKPOINT_FLAGS.match(rest)
            if flagged:
                event.update(flags=flagged.group(1).split(), cause=checkpoint_cause(flagged.group(1).split()))
            event.update(numbers(CHECKPOINT_NUMBERS, rest))
    elif (match := TOO_FREQUENT.match(message)):
        event = {"kind": "checkpoint_too_frequent", "seconds_apart": int(match.group(1))}
    elif (match := KILLED.match(message)):
        # Signal 9 is usually the kernel OOM killer; every session is reset by the crash restart.
        event = {"kind": "backend_killed", "process": match.group(1), "signal": int(match.group(2))}
    elif message.startswith("terminating any other active server processes"):
        event = {"kind": "crash_restart"}
    elif state in STATE_KINDS:
        event = {"kind": STATE_KINDS[state]}
        if state == "57014":
            if record.get("backend_type") == "autovacuum worker":
                # A conflicting lock request cancelled autovacuum; repeated, the table never gets vacuumed.
                event["kind"] = "autovacuum_canceled"
            else:
                event["cause"] = ("statement_timeout" if "statement timeout" in message else
                                  "user_request" if "user request" in message else "unknown")
        elif state == "55P03":
            event["cause"] = "lock_timeout" if "lock timeout" in message else "nowait_or_unknown"
        elif state == "40001" and "conflict with recovery" in message:
            event["kind"] = "recovery_conflict"
    if event is None:
        return None
    event["line"] = line_number
    for key in ("timestamp", "dbname", "user", "session_id", "application_name", "error_severity", "backend_type"):
        if isinstance(record.get(key), str):
            event[key] = record[key]
    for key in ("pid", "leader_pid", "line_num"):
        if isinstance(record.get(key), int) and not isinstance(record[key], bool):
            event[key] = record[key]
    if isinstance(state, str):
        event["state_code"] = state
    identifier = query_id(record.get("query_id"))
    if identifier is not None:
        event["query_id"] = identifier
    if include_details:
        event["details"] = {key: record[key] for key in ("message", "detail", "hint", "statement", "context", "internal_query")
                            if isinstance(record.get(key), str)}
    return event


def new_stats():
    return {"event_counts": Counter(), "temp_bytes": 0, "durations_by_phase": {}}


def update_stats(stats, event):
    stats["event_counts"][event["kind"]] += 1
    stats["temp_bytes"] += event.get("bytes", 0)
    if event["kind"] == "duration":
        duration = stats["durations_by_phase"].setdefault(event["phase"], {"observed_count": 0, "total_ms": 0, "max_ms": 0})
        duration["observed_count"] += 1
        duration["total_ms"] += event["duration_ms"]
        duration["max_ms"] = max(duration["max_ms"], event["duration_ms"])


def finalize(stats):
    stats["event_counts"] = dict(sorted(stats["event_counts"].items()))
    for values in stats["durations_by_phase"].values():
        values["observed_mean_ms"] = values["total_ms"] / values["observed_count"]
    return stats


def new_checkpoints():
    return {"started": 0, "by_cause": Counter(), "completed": 0, "write_s_total": 0.0, "sync_s_total": 0.0,
            "total_s_max": None, "longest_sync_s_max": None, "buffers_written_total": 0, "distance_kb_max": None,
            "wal_files_added": 0, "wal_files_removed": 0, "wal_files_recycled": 0}


def update_checkpoints(stats, event):
    if event["kind"].endswith("_start"):
        stats["started"] += 1
        stats["by_cause"][event["cause"]] += 1
        return
    stats["completed"] += 1
    for source, target in (("write_s", "write_s_total"), ("sync_s", "sync_s_total"),
                           ("buffers_written", "buffers_written_total"), ("wal_files_added", "wal_files_added"),
                           ("wal_files_removed", "wal_files_removed"), ("wal_files_recycled", "wal_files_recycled")):
        stats[target] += event.get(source, 0)
    for source, target in (("total_s", "total_s_max"), ("longest_sync_s", "longest_sync_s_max"),
                           ("distance_kb", "distance_kb_max")):
        if source in event:
            stats[target] = max(stats[target] or 0, event[source])


def update_vacuum(tables, event, omitted):
    name = event.get("table")
    if name is None:
        return
    if name not in tables and len(tables) >= MAX_GROUPS:
        omitted[0] += 1
        return
    stats = tables.setdefault(name, {"table": name, "vacuum_runs": 0, "analyze_runs": 0, "aggressive_runs": 0,
                                     "wraparound_runs": 0, "elapsed_s_total": 0.0, "elapsed_s_max": None,
                                     "tuples_removed_total": 0, "dead_not_removable_max": None,
                                     "runs_with_dead_not_removable": 0, "cutoff_xids_old_max": None,
                                     "tuples_missed_total": 0, "index_scans_max": None})
    stats["elapsed_s_total"] += event.get("elapsed_s", 0)
    if "elapsed_s" in event:
        stats["elapsed_s_max"] = max(stats["elapsed_s_max"] or 0, event["elapsed_s"])
    if event["kind"] == "autoanalyze":
        stats["analyze_runs"] += 1
        return
    stats["vacuum_runs"] += 1
    stats["aggressive_runs"] += bool(event.get("aggressive"))
    stats["wraparound_runs"] += bool(event.get("to_prevent_wraparound"))
    stats["tuples_removed_total"] += event.get("tuples_removed", 0)
    stats["tuples_missed_total"] += event.get("tuples_missed", 0)
    stats["runs_with_dead_not_removable"] += event.get("dead_not_removable", 0) > 0
    for source, target in (("dead_not_removable", "dead_not_removable_max"), ("cutoff_xids_old", "cutoff_xids_old_max"),
                           ("index_scans", "index_scans_max")):
        if source in event:
            stats[target] = max(stats[target] or 0, event[source])


def analyze(lines, max_events=1000, include_details=False, keep_plans=0):
    """Summarize jsonlog lines. keep_plans > 0 also returns the slowest auto_explain JSON plans
    under 'plans' (they contain SQL text; the CLI writes them to files and removes them)."""
    if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
        raise ValueError("max_events must be a positive integer")
    if isinstance(keep_plans, bool) or not isinstance(keep_plans, int) or keep_plans < 0:
        raise ValueError("keep_plans must be a non-negative integer")
    report = {"schema_version": 1, "kind": "jsonlog_analysis", "lines": 0, "valid_records": 0,
              "blank_lines": 0, "malformed_lines": 0, "malformed_line_numbers": [], "oversized_lines": 0,
              "unclassified_records": 0, "first_timestamp": None, "last_timestamp": None,
              "events": [], "events_omitted": 0,
              "query_groups": [], "query_group_limit": MAX_GROUPS, "query_group_events_omitted": 0,
              "limitations": list(LIMITATIONS)}
    summary = new_stats()
    groups, error_states, vacuum_tables, vacuum_omitted = {}, Counter(), {}, [0]
    checkpoints = {"checkpoint": new_checkpoints(), "restartpoint": new_checkpoints()}
    plans, plan_counts = [], Counter()
    for line_number, line in enumerate(lines, 1):
        report["lines"] += 1
        if not line.strip():
            report["blank_lines"] += 1
            continue
        if len(line) > MAX_LINE_CHARS:
            report["oversized_lines"] += 1
            continue
        try:
            record = json.loads(line, parse_constant=reject_constant)
            if not isinstance(record, dict):
                raise ValueError("Expected object")
        except (ValueError, RecursionError):
            report["malformed_lines"] += 1
            if len(report["malformed_line_numbers"]) < 20:
                report["malformed_line_numbers"].append(line_number)
            continue
        report["valid_records"] += 1
        if isinstance(record.get("timestamp"), str):
            report["first_timestamp"] = report["first_timestamp"] or record["timestamp"]
            report["last_timestamp"] = record["timestamp"]
        state = record.get("state_code", record.get("sql_state_code"))
        if record.get("error_severity") in ("ERROR", "FATAL", "PANIC") and isinstance(state, str):
            error_states[state] += 1
        event = observed_event(record, line_number, include_details)
        if event is None:
            report["unclassified_records"] += 1
            continue
        plan_text = event.pop("_plan_text", None)
        if event["kind"] == "duration" and event["phase"] == "plan":
            plan_counts[event["plan_format"]] += 1
            # Parse only a plan that would enter the kept set: logs can hold many large plans.
            rank = (event["duration_ms"], line_number)
            if plan_text is not None and keep_plans and (len(plans) < keep_plans or rank > plans[0][:2]):
                plan = auto_explain_plan(plan_text)
                if plan is not None:
                    entry = (*rank, {"line": line_number, "duration_ms": event["duration_ms"],
                                     "query_id": event.get("query_id"), "timestamp": event.get("timestamp"),
                                     "plan": plan})
                    if len(plans) < keep_plans:
                        heapq.heappush(plans, entry)
                    else:
                        heapq.heapreplace(plans, entry)
        if event["kind"].startswith(("checkpoint_", "restartpoint_")) and event["kind"] != "checkpoint_too_frequent":
            update_checkpoints(checkpoints[event["kind"].split("_")[0]], event)
        if event["kind"] in ("autovacuum", "autoanalyze"):
            update_vacuum(vacuum_tables, event, vacuum_omitted)
        update_stats(summary, event)
        if len(report["events"]) < max_events:
            report["events"].append(event)
        else:
            report["events_omitted"] += 1
        if event.get("query_id") is not None:
            key = (event.get("dbname"), event.get("user"), event["query_id"])
            if key not in groups and len(groups) >= MAX_GROUPS:
                report["query_group_events_omitted"] += 1
                continue
            if key not in groups:
                groups[key] = {"dbname": key[0], "user": key[1], "query_id": key[2], **new_stats()}
            update_stats(groups[key], event)
    report["summary"] = finalize(summary)
    report["query_groups"] = [finalize(value) for value in groups.values()]
    report["error_states"] = dict(error_states.most_common(30))
    for name, stats in checkpoints.items():
        stats["by_cause"] = dict(sorted(stats["by_cause"].items()))
        report[name + "s"] = stats
    tables = sorted(vacuum_tables.values(), key=lambda x: x["elapsed_s_total"], reverse=True)
    report["autovacuum"] = {"tables": tables[:max_events], "tables_total": len(tables),
                            "tables_omitted_by_cap": vacuum_omitted[0]}
    report["auto_explain_plans"] = {"json": plan_counts.get("json", 0), "text": plan_counts.get("text", 0)}
    if keep_plans:
        report["plans"] = [entry[2] for entry in sorted(plans, reverse=True)]
    counts = dict(report["summary"]["event_counts"])
    counts["checkpoint_by_wal_volume"] = checkpoints["checkpoint"]["by_cause"].get("wal", 0)
    counts["autovacuum_dead_not_removable"] = sum(x["runs_with_dead_not_removable"] for x in tables)
    report["candidates"] = []
    actions = {
        "deadlock": "Inspect the original DETAIL and transaction order in the application; do not terminate a current PID based on historical logs.",
        "temp_file": "Correlate same-record query IDs and plans; compare input rows and memory budgets before changing work_mem.",
        "lock_wait": "Capture a current pg_blocking_pids wait graph or correlate recorded sessions (holder_pids); the historical blocker may already be gone.",
        "lock_not_available": "Inspect NOWAIT or lock_timeout behavior and the application retry policy.",
        "checkpoint_by_wal_volume": "These checkpoints started because WAL reached max_wal_size; compare with the WAL rate of the same window and the checkpointer counters.",
        "checkpoint_too_frequent": "WAL-triggered checkpoints closer together than checkpoint_warning; size max_wal_size from the WAL rate and check storage latency.",
        "checkpoint_start": "Compare checkpoint frequency and causes with the observation window, WAL rate and storage latency.",
        "autovacuum_dead_not_removable": "VACUUM ran but could not remove dead rows: find what holds the xmin horizon (old transaction, replication slot, prepared transaction, standby feedback).",
        "autovacuum_canceled": "A lock request cancelled autovacuum; repeated DDL or LOCK on the table can keep it from ever finishing.",
        "autovacuum": "Compare vacuum duration, update rates, oldest snapshots and table-specific settings.",
        "too_many_connections": "Connections were refused at max_connections; check pool sizes and connection leaks before raising the limit.",
        "idle_in_transaction_timeout": "Sessions were terminated while idle in a transaction; find the code path that leaves transactions open.",
        "transaction_timeout": "Transactions exceeded transaction_timeout; check their intended duration and the timeout's scope.",
        "statement_canceled": "Statements were cancelled (see cause); for statement_timeout compare the limit with the statement's normal duration.",
        "recovery_conflict": "Standby queries were cancelled by WAL replay; compare max_standby_streaming_delay, hot_standby_feedback and query length.",
        "serialization_failure": "Serializable/repeatable-read conflicts; verify the application retries the whole transaction.",
        "disk_full": "The server could not extend a file; check WAL retention (slots, archiving), temp files and data growth.",
        "configuration_limit_exceeded": "A configured limit such as temp_file_limit stopped a statement; identify the statement before raising it.",
        "backend_killed": "A backend was killed (signal 9 is usually the OOM killer); check memory limits, work_mem x concurrency and the kernel/cgroup OOM log.",
        "out_of_memory": "PostgreSQL could not allocate memory; check work_mem, hash_mem_multiplier, parallel workers and concurrent queries.",
    }
    for kind, next_step in actions.items():
        count = counts.get(kind, 0)
        if count:
            report["candidates"].append({"kind": kind, "observed_events": count, "next_step": next_step})
    return report


def markdown(report):
    lines = ["# PostgreSQL JSON log observations", "",
             f"Read {report['lines']} lines; valid records: {report['valid_records']}; malformed: {report['malformed_lines']}; oversized: {report['oversized_lines']}.",
             f"Log records span {report.get('first_timestamp')} → {report.get('last_timestamp')} (as logged; align with the snapshot window).",
             "", "Summary: " + json.dumps(report["summary"], ensure_ascii=False), "",
             f"Retained events: {len(report['events'])}; omitted: {report['events_omitted']}. Aggregates include omitted events.",
             f"Query group cap: {report['query_group_limit']}; ungrouped due to cap: {report['query_group_events_omitted']} events.", "",
             "## Investigation candidates", ""]
    for candidate in report["candidates"]:
        lines.append(f"- {candidate['kind']}: {candidate['observed_events']} observed events. {candidate['next_step']}")
    if not report["candidates"]:
        lines.append("No candidate rule matched. Review coverage before concluding that no problem exists.")
    for name in ("checkpoints", "restartpoints"):
        stats = report.get(name) or {}
        if stats.get("started") or stats.get("completed"):
            lines += ["", f"## {name.capitalize()}", "", json.dumps(stats, ensure_ascii=False)]
    vacuum = report.get("autovacuum") or {}
    if vacuum.get("tables"):
        lines += ["", f"## Autovacuum by table (top 15 of {vacuum['tables_total']} by elapsed time)", "",
                  "| Table | Vacuums | Analyzes | Elapsed s | Max s | Removed | Max dead not removable | Max cutoff age (XIDs) | Aggressive |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for item in vacuum["tables"][:15]:
            cells = [str(item["table"]).replace("|", "\\|"), item["vacuum_runs"], item["analyze_runs"],
                     f"{item['elapsed_s_total']:.2f}", item["elapsed_s_max"], item["tuples_removed_total"],
                     item["dead_not_removable_max"], item["cutoff_xids_old_max"], item["aggressive_runs"]]
            lines.append("| " + " | ".join("—" if cell is None else str(cell) for cell in cells) + " |")
    if report.get("error_states"):
        lines += ["", "## Errors by SQLSTATE (ERROR/FATAL/PANIC records)", "",
                  json.dumps(report["error_states"], ensure_ascii=False)]
    explained = report.get("auto_explain_plans") or {}
    if explained.get("json") or explained.get("text"):
        lines += ["", f"auto_explain plans in the log: {explained.get('json', 0)} JSON, {explained.get('text', 0)} text. "
                  "Use --extract-plans DIR to save the slowest JSON plans for analyze_plan.py."]
    if report.get("extracted_plans"):
        lines += ["", "## Extracted plans (contain SQL text)", ""]
        lines += ["- " + json.dumps(item, ensure_ascii=False) for item in report["extracted_plans"]]
    lines += ["", "## Same-record query ID groups", ""]
    lines += ["- " + json.dumps(group, ensure_ascii=False) for group in report["query_groups"]]
    lines += ["", "## Retained events", "", "```json", json.dumps(report["events"], ensure_ascii=False, indent=2), "```", "",
              "## Limits", ""] + ["- " + item for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def write_plans(report, directory):
    """Write kept plans as new files analyze_plan.py reads; replace them by file references."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for item in report.pop("plans", []):
        path = directory / f"plan-line{item['line']}.json"
        document = {"plan": item["plan"],
                    "capture": {"source": f"auto_explain jsonlog line {item['line']}",
                                "logged_duration_ms": item["duration_ms"], "query_id": item["query_id"],
                                "timestamp": item["timestamp"]}}
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        written.append({"file": str(path), "line": item["line"], "duration_ms": item["duration_ms"],
                        "query_id": item["query_id"], "timestamp": item["timestamp"]})
    report["extracted_plans"] = written


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="PostgreSQL jsonlog file, one JSON object per line")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--include-details", action="store_true", help="Include potentially sensitive messages and SQL")
    parser.add_argument("--max-events", type=positive_int, default=1000)
    parser.add_argument("--extract-plans", type=Path, metavar="DIR",
                        help="Write the slowest auto_explain JSON plans here as new files (they contain SQL text)")
    parser.add_argument("--max-plans", type=positive_int, default=20, help="Plans kept by --extract-plans")
    args = parser.parse_args(argv)
    try:
        with args.input.open(encoding="utf-8-sig") as handle:
            report = analyze(handle, max_events=args.max_events, include_details=args.include_details,
                             keep_plans=args.max_plans if args.extract_plans else 0)
        if args.extract_plans:
            write_plans(report, args.extract_plans)
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json" else markdown(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0
    except (OSError, UnicodeError, ValueError, RecursionError):
        print("error: Cannot read JSON log or write report", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
