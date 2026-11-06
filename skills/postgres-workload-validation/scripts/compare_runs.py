#!/usr/bin/env python3
"""Compare repeated PostgreSQL workload runs from complete per-transaction logs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
from statistics import median


def measurement(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def pgbench_events(paths, directory, conditions):
    rate = conditions.get("rate") is not None
    retried = conditions.get("max_tries", 1) != 1
    width = 6 + rate + retried
    for path in paths:
        with (directory / path).open(encoding="utf-8") as log:
            for line in log:
                fields = line.split()
                if len(fields) != width or not all(value.isdecimal() for value in fields[:2] + fields[3:]):
                    raise ValueError("expected a raw pgbench transaction log matching rate and max_tries")
                if int(fields[5]) >= 1000000:
                    raise ValueError("invalid pgbench completion timestamp")
                details = {"schedule_lag_ms": int(fields[6]) / 1000} if rate else {}
                details["retries"] = int(fields[-1]) if retried else 0
                elapsed = fields[2]
                if elapsed.isdecimal():
                    yield {"status": "ok", "latency_ms": int(elapsed) / 1000, **details}
                elif elapsed in ("skipped", "failed", "serialization", "deadlock"):
                    yield {"status": "skipped" if elapsed == "skipped" else "failed", **details}
                else:
                    raise ValueError("unknown pgbench transaction status")


def percentile(values, fraction):
    return sorted(values)[math.ceil(fraction * len(values)) - 1] if values else None


def summarize(run, directory, conditions):
    elapsed = run["elapsed_seconds"]
    if not measurement(elapsed) or elapsed == 0 or datetime.fromisoformat(run["started_at"]).utcoffset() is None:
        raise ValueError("run requires positive elapsed_seconds and a timezone-aware started_at")
    events = run.get("events")
    if events is None:
        events = pgbench_events(run["pgbench_logs"], directory, conditions)
    counts, times, lag, retries = Counter(), [], [], []
    for event in events:
        status = event["status"]
        if status not in ("ok", "failed", "timeout", "skipped"):
            raise ValueError("unknown event status")
        counts[status] += 1
        if measurement(event.get("schedule_lag_ms")):
            lag.append(event["schedule_lag_ms"])
        if type(event.get("retries")) is int and event["retries"] >= 0:
            retries.append(event["retries"])
        if status == "ok":
            value = event.get("latency_ms")
            if not measurement(value):
                raise ValueError("successful events require a finite nonnegative latency")
            times.append(value)
    total = sum(counts.values())
    return {
        "started_at": run["started_at"], "elapsed_seconds": elapsed,
        "complete": run.get("complete") is True and total > 0,
        "result_digest": run.get("result_digest"), "counts": dict(counts),
        "offered_tps": total / elapsed, "completed_tps": counts["ok"] / elapsed,
        "loss_rate": (total - counts["ok"]) / total if total else None,
        "mean_ms": sum(times) / len(times) if times else None,
        "p95_ms": percentile(times, .95), "p99_ms": percentile(times, .99),
        "schedule_lag_p95_ms": percentile(lag, .95) if len(lag) == total else None,
        "retries": sum(retries) if retries and len(retries) == total else None,
        "metrics": run.get("metrics", {}),
    }


def read(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["conditions"], [summarize(run, path.parent, data["conditions"]) for run in data["runs"]]


def compare(before, after, limits, resource_limits, generator_cpu_max=None):
    base_conditions, baseline = before
    next_conditions, candidate = after
    runs = baseline + candidate
    missing, regressions = [], []
    if not base_conditions or base_conditions != next_conditions:
        missing.append("missing or different replay conditions")
    if len(baseline) < 2 or len(candidate) < 2:
        missing.append("two or more runs required per variant")
    if any(not run["complete"] for run in runs):
        missing.append("missing or incomplete event logs")
    digests = [run["result_digest"] for run in runs]
    if not all(digests):
        missing.append("missing result checks")
    if not missing and len({digest for digest in digests if digest}) > 1:
        regressions.append("result mismatch")
    required = {"cpu_pct", "io_read_bytes", "wal_bytes", "oom_kills"}
    for group, observations in (("baseline", baseline), ("candidate", candidate)):
        for index, run in enumerate(observations, 1):
            absent = sorted(name for name in required if not measurement(run["metrics"].get(name)))
            if absent:
                missing.append(f"{group} {index}: missing DB or OS metrics: {', '.join(absent)}")
            cpu = run["metrics"].get("load_generator_cpu_pct")
            if generator_cpu_max is not None and not measurement(cpu):
                missing.append(f"{group} {index}: missing load generator CPU")
            elif generator_cpu_max is not None and cpu > generator_cpu_max:
                missing.append(f"{group} {index}: load generator saturated ({cpu}%)")
    for index, run in enumerate(candidate, 1):
        for name, limit in limits.items():
            value = run[name]
            if value is None:
                missing.append(f"candidate {index}: missing {name}")
            elif (value < limit if name == "completed_tps" else value > limit):
                regressions.append(f"candidate {index}: {name}={value} violates {limit}")
        for name, limit in resource_limits.items():
            value = run["metrics"].get(name)
            if not measurement(value):
                missing.append(f"candidate {index}: missing {name}")
            elif value > limit:
                regressions.append(f"candidate {index}: {name}={value} exceeds {limit}")

    def spread(group):
        result = {}
        for name in ("offered_tps", "completed_tps", "loss_rate", "mean_ms", "p95_ms", "p99_ms", "schedule_lag_p95_ms", "retries"):
            values = [run[name] for run in group if run[name] is not None]
            result[name] = [min(values), median(values), max(values)] if values else None
        return result

    baseline_spread, candidate_spread = spread(baseline), spread(candidate)
    changes = {name: candidate_spread[name][1] - values[1]
               for name, values in baseline_spread.items() if values and candidate_spread[name]}
    return {"decision": "investigate" if missing else "rollback_candidate" if regressions else "accept_candidate",
            "missing": missing, "regressions": regressions, "baseline": baseline, "candidate": candidate,
            "median_change": changes,
            "min_median_max": {"baseline": baseline_spread, "candidate": candidate_spread}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--p95-ms", type=float, required=True)
    parser.add_argument("--min-tps", type=float, required=True)
    parser.add_argument("--max-loss-rate", type=float, required=True)
    parser.add_argument("--metric-max", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--load-generator-cpu-max", type=float,
                        help="Saturation threshold in the same units as load_generator_cpu_pct")
    args = parser.parse_args()
    try:
        limits = {"p95_ms": args.p95_ms, "completed_tps": args.min_tps, "loss_rate": args.max_loss_rate}
        resource_limits = {name: float(value) for name, value in
                           (item.split("=", 1) for item in args.metric_max)}
        if not all(measurement(value) for value in (*limits.values(), *resource_limits.values())) or args.max_loss_rate > 1:
            raise ValueError("invalid threshold")
        if args.load_generator_cpu_max is not None and not measurement(args.load_generator_cpu_max):
            raise ValueError("invalid load generator CPU threshold")
        result = compare(read(args.baseline), read(args.candidate), limits, resource_limits,
                         args.load_generator_cpu_max)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
