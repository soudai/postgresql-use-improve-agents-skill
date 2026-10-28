#!/usr/bin/env python3
"""Compare a baseline and a candidate interval report (analyze.py JSON) for before/after validation."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

PER_CALL = ("total_exec_time", "rows", "shared_blks_hit", "shared_blks_read", "temp_blks_written", "wal_bytes",
            "wal_fpi", "total_plan_time")
SYSTEM = (("database", ("xact_commit", "xact_rollback", "temp_bytes", "deadlocks", "blks_read", "blks_hit",
                         "tup_returned", "tup_fetched", "active_time")),
          ("wal", ("wal_records", "wal_fpi", "wal_bytes", "wal_buffers_full")),
          ("checkpointer", ("num_timed", "num_requested", "write_time", "buffers_written")))
LOAD_TOLERANCE = 0.2
LIMITATIONS = [
    "Counters are aggregates: compare percentiles only from individual request/execution logs.",
    "Equal rows per call is necessary, not sufficient, for identical results; verify result content separately.",
    "Different query IDs after a SQL rewrite must be paired explicitly with --map.",
]


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ratio(a, b):
    return a / b if numeric(a) and numeric(b) and b != 0 else None


def statements(report):
    result = {}
    for entry in report.get("deltas", {}).get("pgss", []):
        identity = entry.get("identity", {})
        key = str(identity.get("queryid"))
        if entry.get("toplevel") is False or key in result:
            continue  # Same SQL by several users: keep the first top-level entry; see warnings.
        result[key] = entry
    return result


def profile(entry):
    delta, calls = entry["delta"], entry["delta"].get("calls")
    data = {"calls": calls, "calls_per_second": entry["per_second"].get("calls"),
            "shared_hit_ratio": entry.get("shared_hit_ratio"), "read_time_share": entry.get("read_time_share")}
    for field in PER_CALL:
        data[f"{field}_per_call"] = ratio(delta.get(field), calls)
    return data


def compare_statement(before, after):
    a, b = profile(before), profile(after)
    warnings = []
    # A replayed fixed workload keeps the call count; a fixed time window keeps the rate. Either is comparable.
    rate, count = ratio(b["calls_per_second"], a["calls_per_second"]), ratio(b["calls"], a["calls"])
    if rate is not None and count is not None and abs(rate - 1) > LOAD_TOLERANCE and abs(count - 1) > LOAD_TOLERANCE:
        warnings.append(f"load differs: calls ratio {count:.2f}, calls/s ratio {rate:.2f}")
    if numeric(a["shared_hit_ratio"]) and numeric(b["shared_hit_ratio"]) and abs(a["shared_hit_ratio"] - b["shared_hit_ratio"]) > 0.2:
        warnings.append("cache condition differs: shared hit ratio "
                        f"{a['shared_hit_ratio']:.2f} -> {b['shared_hit_ratio']:.2f}")
    rows = ratio(b["rows_per_call"], a["rows_per_call"])
    if rows is not None and abs(rows - 1) > 0.01:
        warnings.append(f"rows per call changed ({a['rows_per_call']} -> {b['rows_per_call']}); verify result correctness")
    changes = {key: ratio(b[key], a[key]) for key in a if key.endswith("_per_call")}
    return {"before": a, "after": b, "after_over_before": changes, "warnings": warnings}


def system(report):
    result = {}
    for section, fields in SYSTEM:
        rows = report.get("deltas", {}).get(section) or []
        if rows:
            result[section] = {field: rows[0]["per_second"].get(field) for field in fields}
    return result


def compare(before, after, mapping=None, queryids=None):
    warnings = []
    if before.get("instance_id") != after.get("instance_id") or before.get("database") != after.get("database"):
        warnings.append("different instance or database: environment differences may dominate")
    settings_a = (before.get("settings") or {}).get("after", {})
    settings_b = (after.get("settings") or {}).get("after", {})
    changed = sorted(name for name in set(settings_a) | set(settings_b) if settings_a.get(name) != settings_b.get(name))
    if not settings_a or not settings_b:
        warnings.append("settings unavailable in one report: configuration differences not checked")
    for report, label in ((before, "baseline"), (after, "candidate")):
        for warning in report.get("warnings", []):
            if "suppressed" in warning or "unavailable" in warning or "truncated" in warning:
                warnings.append(f"{label}: {warning}")
    a, b = statements(before), statements(after)
    pairs = dict(mapping or {})
    if queryids:
        pairs.update({q: pairs.get(q, q) for q in queryids})
    else:
        pairs.update({q: q for q in a if q in b and q not in pairs})
    results = []
    for old, new in pairs.items():
        if old not in a or new not in b:
            warnings.append(f"query {old} -> {new}: missing in {'baseline' if old not in a else 'candidate'}")
            continue
        item = compare_statement(a[old], b[new])
        item.update(baseline_queryid=old, candidate_queryid=new)
        results.append(item)
    results.sort(key=lambda x: -(x["before"]["total_exec_time_per_call"] or 0) * (x["before"]["calls"] or 0))
    sys_a, sys_b = system(before), system(after)
    return {"schema_version": 1, "kind": "interval_comparison",
            "baseline": {"instance_id": before.get("instance_id"), "window": before.get("window")},
            "candidate": {"instance_id": after.get("instance_id"), "window": after.get("window")},
            "settings_changed": {name: [settings_a.get(name), settings_b.get(name)] for name in changed},
            "statements": results,
            "only_in_baseline": sorted(set(a) - set(pairs)), "only_in_candidate": sorted(set(b) - set(pairs.values())),
            "system_per_second": {"before": sys_a, "after": sys_b},
            "warnings": warnings, "limitations": LIMITATIONS}


def markdown(result, top=20):
    lines = ["# Before/after interval comparison", ""]
    lines += ["## Comparability", ""] + (["- " + w for w in result["warnings"]] or ["- No difference detected in recorded conditions."])
    if result["settings_changed"]:
        lines += ["", "Settings changed: " + json.dumps(result["settings_changed"], ensure_ascii=False)]
    lines += ["", "## Statements", "", "| queryid | calls | ms/call before | ms/call after | ratio | rows/call | warnings |",
              "|---|---:|---:|---:|---:|---:|---|"]

    def cell(value):
        return "—" if value is None else (f"{value:.3f}" if isinstance(value, float) else str(value))

    for item in result["statements"][:top]:
        a, b = item["before"], item["after"]
        lines.append("| " + " | ".join([f"{item['baseline_queryid']}→{item['candidate_queryid']}",
                                        f"{cell(a['calls'])}→{cell(b['calls'])}", cell(a["total_exec_time_per_call"]),
                                        cell(b["total_exec_time_per_call"]), cell(item["after_over_before"]["total_exec_time_per_call"]),
                                        f"{cell(a['rows_per_call'])}→{cell(b['rows_per_call'])}",
                                        "; ".join(item["warnings"]).replace("|", "\\|")]) + " |")
    lines += ["", "## System rates (per second)", "", "```json",
              json.dumps(result["system_per_second"], ensure_ascii=False, indent=2), "```", "", "## Limits", ""]
    lines += ["- " + x for x in result["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="analyze.py --format json report before the change")
    parser.add_argument("candidate", type=Path, help="analyze.py --format json report after the change")
    parser.add_argument("--queryid", action="append", help="Compare only these query IDs (repeatable)")
    parser.add_argument("--map", action="append", default=[], help="OLD=NEW query ID pairing for rewritten SQL")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        mapping = dict(item.split("=", 1) for item in args.map)
        before = json.loads(args.baseline.read_text(encoding="utf-8"))
        after = json.loads(args.candidate.read_text(encoding="utf-8"))
        result = compare(before, after, mapping, args.queryid)
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json" else markdown(result)
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
