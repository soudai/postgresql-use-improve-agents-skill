#!/usr/bin/env python3
"""Summarize PostgreSQL JSON log events offline; never execute logged SQL."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys


TEMP = re.compile(r'^temporary file: path ".*", size ([0-9]+)\s*$', re.DOTALL)
DURATION = re.compile(r'^duration:\s+([0-9]+(?:\.[0-9]+)?)\s+ms\b(.*)', re.DOTALL)
LOCK = re.compile(r'^process [0-9]+ (still waiting for|acquired) .+ after ([0-9]+(?:\.[0-9]+)?) ms(?:\s|$)', re.DOTALL)
MAX_LINE_CHARS = 2 * 1024 * 1024
MAX_GROUPS = 1000
LIMITATIONS = [
    "Input must be PostgreSQL jsonlog (one JSON object per line), from one server and a known observation window.",
    "English message patterns are recognized; localized messages may remain unclassified. SQLSTATE 40P01 works across languages.",
    "Durations are observed log events only. Sampling and duration thresholds cannot be inferred from a record; no population percentiles are calculated.",
    "Parse, bind, execute and statement durations are separate phases and must not be counted as independent completed queries or blindly summed.",
    "Temp file sizes are logged at deletion; they are not instantaneous disk use or per-table attribution.",
    "Events are associated with query_id only when that same record supplies it; previous SQL on a PID is never used to guess attribution.",
    "Lock wait logs cover waits exceeding the configured threshold, not all waits. Deadlock logs need application transaction context for a full causal analysis.",
    "No matching events is not proof of a healthy database. Log settings, missing files and rotation determine coverage.",
    "Messages, SQL and details are omitted by default; database/user/application/session names and identifiers remain visible.",
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
    elif message.startswith("automatic vacuum of table ") or message.startswith("automatic aggressive vacuum of table "):
        event = {"kind": "autovacuum"}
    elif message.startswith("automatic analyze of table "):
        event = {"kind": "autoanalyze"}
    elif message.startswith("checkpoint starting:"):
        event = {"kind": "checkpoint_start"}
    elif message.startswith("checkpoint complete:"):
        event = {"kind": "checkpoint_complete"}
    elif state == "55P03":
        event = {"kind": "lock_not_available"}
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


def analyze(lines, max_events=1000, include_details=False):
    if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
        raise ValueError("max_events must be a positive integer")
    report = {"schema_version": 1, "kind": "jsonlog_analysis", "lines": 0, "valid_records": 0,
              "blank_lines": 0, "malformed_lines": 0, "malformed_line_numbers": [], "oversized_lines": 0,
              "unclassified_records": 0, "events": [], "events_omitted": 0,
              "query_groups": [], "query_group_limit": MAX_GROUPS, "query_group_events_omitted": 0,
              "limitations": list(LIMITATIONS)}
    summary = new_stats()
    groups = {}
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
        event = observed_event(record, line_number, include_details)
        if event is None:
            report["unclassified_records"] += 1
            continue
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
    report["candidates"] = []
    actions = {
        "deadlock": "Inspect the original DETAIL and transaction order in the application; do not terminate a current PID based on historical logs.",
        "temp_file": "Correlate same-record query IDs and plans; compare input rows and memory budgets before changing work_mem.",
        "lock_wait": "Capture a current pg_blocking_pids wait graph or correlate recorded sessions; the historical blocker may already be gone.",
        "lock_not_available": "Inspect NOWAIT or lock_timeout behavior and the application retry policy.",
        "checkpoint_start": "Compare checkpoint frequency with the observation window, WAL rate and storage latency.",
        "autovacuum": "Compare vacuum duration, update rates, oldest snapshots and table-specific settings.",
    }
    for kind, next_step in actions.items():
        count = report["summary"]["event_counts"].get(kind, 0)
        if count:
            report["candidates"].append({"kind": kind, "observed_events": count, "next_step": next_step})
    return report


def markdown(report):
    lines = ["# PostgreSQL JSON log observations", "",
             f"Read {report['lines']} lines; valid records: {report['valid_records']}; malformed: {report['malformed_lines']}; oversized: {report['oversized_lines']}.",
             "", "Summary: " + json.dumps(report["summary"], ensure_ascii=False), "",
             f"Retained events: {len(report['events'])}; omitted: {report['events_omitted']}. Aggregates include omitted events.",
             f"Query group cap: {report['query_group_limit']}; ungrouped due to cap: {report['query_group_events_omitted']} events.", "",
             "## Investigation candidates", ""]
    for candidate in report["candidates"]:
        lines.append(f"- {candidate['kind']}: {candidate['observed_events']} observed events. {candidate['next_step']}")
    if not report["candidates"]:
        lines.append("No candidate rule matched. Review coverage before concluding that no problem exists.")
    lines += ["", "## Same-record query ID groups", ""]
    lines += ["- " + json.dumps(group, ensure_ascii=False) for group in report["query_groups"]]
    lines += ["", "## Retained events", "", "```json", json.dumps(report["events"], ensure_ascii=False, indent=2), "```", "",
              "## Limits", ""] + ["- " + item for item in report["limitations"]]
    return "\n".join(lines) + "\n"


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
    args = parser.parse_args(argv)
    try:
        with args.input.open(encoding="utf-8-sig") as handle:
            report = analyze(handle, max_events=args.max_events, include_details=args.include_details)
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
