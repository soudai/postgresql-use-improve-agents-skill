#!/usr/bin/env python3
"""Find N+1, over-fetching, large results and pool waits in application request traces (JSONL)."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys

LIMITATIONS = [
    "Classifications rely on what the trace records; missing rows_used or pool_wait_ms leaves those checks undecided.",
    "N+1 is reported when one statement repeats per row of an earlier result in the same request; other repetition is listed separately.",
    "Percentiles use individual requests in this trace only; sampling in the tracer limits what they represent.",
]


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(fraction * len(ordered)) - 1, 0)]


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load(lines):
    requests, spans, malformed = {}, defaultdict(list), 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or not record.get("request_id"):
                raise ValueError
        except ValueError:
            malformed += 1
            continue
        if record.get("type") == "request":
            requests[record["request_id"]] = record
        elif record.get("type") == "db":
            spans[record["request_id"]].append(record)
        else:
            malformed += 1
    return requests, spans, malformed


def statement_key(span):
    return str(span.get("query_id") or span.get("statement") or "unknown")


def analyze(lines, repeat=5, large_rows=10000, overfetch_ratio=0.1):
    requests, spans, malformed = load(lines)
    candidates, per_statement = [], defaultdict(lambda: {"calls": 0, "requests": set(), "max_per_request": 0,
                                                         "rows": 0, "locations": Counter()})
    endpoints = defaultdict(lambda: {"durations": [], "sql_counts": [], "db_ms": 0.0, "total_ms": 0.0,
                                     "pool_waits": [], "pool_share_ms": 0.0, "pool_request_ms": 0.0, "bytes": []})
    for request_id in sorted(set(requests) | set(spans)):
        request, items = requests.get(request_id, {}), spans.get(request_id, [])
        endpoint = request.get("endpoint") or (items[0].get("endpoint") if items else None) or "unknown"
        stats = endpoints[endpoint]
        stats["sql_counts"].append(len(items))
        stats["db_ms"] += sum(s["duration_ms"] for s in items if number(s.get("duration_ms")))
        if items and all(number(s.get("bytes")) for s in items):
            stats["bytes"].append(sum(s["bytes"] for s in items))
        if number(request.get("duration_ms")):
            stats["durations"].append(request["duration_ms"])
            stats["total_ms"] += request["duration_ms"]
        if number(request.get("pool_wait_ms")):
            stats["pool_waits"].append(request["pool_wait_ms"])
            if number(request.get("duration_ms")):
                stats["pool_share_ms"] += request["pool_wait_ms"]
                stats["pool_request_ms"] += request["duration_ms"]
        counts = Counter(statement_key(s) for s in items)
        for key, count in counts.items():
            info = per_statement[key]
            info["calls"] += count
            info["requests"].add(request_id)
            info["max_per_request"] = max(info["max_per_request"], count)
        for span in items:
            info = per_statement[statement_key(span)]
            info["rows"] += span["rows"] if number(span.get("rows")) else 0
            if span.get("code_location"):
                info["locations"][span["code_location"]] += 1
            rows, used = span.get("rows"), span.get("rows_used")
            if number(rows) and number(used) and rows >= 100 and used <= rows * overfetch_ratio:
                candidates.append({"kind": "over_fetching", "request_id": request_id, "endpoint": endpoint,
                                   "statement": statement_key(span), "rows": rows, "rows_used": used,
                                   "code_location": span.get("code_location")})
            elif number(rows) and rows >= large_rows:
                candidates.append({"kind": "large_result", "request_id": request_id, "endpoint": endpoint,
                                   "statement": statement_key(span), "rows": rows, "rows_used": used,
                                   "intended": number(used) and used >= rows * 0.9,
                                   "code_location": span.get("code_location")})
        for key, count in counts.items():
            if count < repeat:
                continue
            first = next(i for i, s in enumerate(items) if statement_key(s) == key)
            parents = [s for s in items[:first] if statement_key(s) != key and number(s.get("rows"))
                       and abs(s["rows"] - count) <= max(1, count * 0.2)]
            candidates.append({"kind": "n_plus_one" if parents else "repeated_statement", "request_id": request_id,
                               "endpoint": endpoint, "statement": key, "executions": count,
                               "parent_statement": statement_key(parents[-1]) if parents else None,
                               "code_location": items[first].get("code_location")})
    summary = []
    for endpoint, stats in sorted(endpoints.items()):
        counts = stats["sql_counts"]
        summary.append({"endpoint": endpoint, "requests": len(counts),
                        "sql_per_request_mean": sum(counts) / len(counts) if counts else None,
                        "sql_per_request_max": max(counts, default=None),
                        "bytes_per_request_mean": sum(stats["bytes"]) / len(stats["bytes"]) if stats["bytes"] else None,
                        "p50_ms": percentile(stats["durations"], 0.5), "p95_ms": percentile(stats["durations"], 0.95),
                        "p99_ms": percentile(stats["durations"], 0.99), "observed_requests": len(stats["durations"]),
                        "db_time_share": stats["db_ms"] / stats["total_ms"] if stats["total_ms"] else None,
                        "p95_pool_wait_ms": percentile(stats["pool_waits"], 0.95),
                        "pool_wait_observed_requests": len(stats["pool_waits"]),
                        "pool_wait_share": (stats["pool_share_ms"] / stats["pool_request_ms"]
                                            if stats["pool_request_ms"] else None)})
    statements = []
    for key, info in per_statement.items():
        kinds = Counter(c["kind"] for c in candidates if c["statement"] == key)
        if kinds:
            label = kinds.most_common(1)[0][0]
        elif info["max_per_request"] <= 2:
            label = "high_frequency_normal" if info["calls"] >= 100 else "normal"
        else:
            label = "normal"
        statements.append({"statement": key, "classification": label, "calls": info["calls"],
                           "requests": len(info["requests"]), "calls_per_request": info["calls"] / len(info["requests"]),
                           "max_per_request": info["max_per_request"],
                           "rows_per_call": info["rows"] / info["calls"] if info["calls"] else None,
                           "code_locations": [loc for loc, _ in info["locations"].most_common(3)]})
    statements.sort(key=lambda x: x["calls"], reverse=True)
    return {"schema_version": 1, "kind": "trace_analysis", "requests": len(requests), "malformed_lines": malformed,
            "endpoints": summary, "statements": statements, "candidates": candidates, "limitations": LIMITATIONS}


def markdown(report, top=20):
    lines = ["# Application trace observations", "", f"Requests: {report['requests']}; malformed lines: {report['malformed_lines']}",
             "", "## Endpoints", "", "| Endpoint | Requests | SQL/request (mean/max) | p95 ms | DB share | Pool wait p95 ms | Pool wait share | Pool samples |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]

    def cell(value, fmt="{:.2f}"):
        return "—" if value is None else fmt.format(value)

    for e in report["endpoints"]:
        lines.append(f"| {e['endpoint']} | {e['requests']} | {cell(e['sql_per_request_mean'])}/{e['sql_per_request_max']} | "
                     f"{cell(e['p95_ms'])} | {cell(e['db_time_share'], '{:.0%}')} | {cell(e['p95_pool_wait_ms'])} | "
                     f"{cell(e['pool_wait_share'], '{:.0%}')} | {e['pool_wait_observed_requests']} |")
    lines += ["", "## Statements", "", "| Statement | Classification | Calls | Calls/request | Rows/call | Code locations |",
              "|---|---|---:|---:|---:|---|"]
    for s in report["statements"][:top]:
        lines.append(f"| {s['statement']} | {s['classification']} | {s['calls']} | {cell(s['calls_per_request'])} | "
                     f"{cell(s['rows_per_call'])} | {', '.join(s['code_locations'])} |")
    lines += ["", "## Limits", ""] + ["- " + x for x in report["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSONL: request records and db span records")
    parser.add_argument("--repeat", type=int, default=5, help="Executions per request treated as repetition")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.repeat < 2:
        parser.error("--repeat must be at least 2")
    try:
        with args.input.open(encoding="utf-8-sig") as handle:
            report = analyze(handle, repeat=args.repeat)
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n" if args.format == "json" else markdown(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
    except (OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
