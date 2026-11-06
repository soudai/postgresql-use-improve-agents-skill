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
        "event_count": total, "successful_count": counts["ok"],
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


def validate_policy(policy):
    if policy is None:
        return None
    fields = {"schema_version", "source", "representative_workload", "min_runs",
              "min_successful_events_per_run", "min_elapsed_seconds", "max_relative_spread"}
    if not isinstance(policy, dict) or set(policy) != fields:
        raise ValueError("validation policy requires documented fields")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("unsupported validation policy version")
    if not isinstance(policy["source"], str) or not policy["source"].strip():
        raise ValueError("validation policy requires a source for the workload plan")
    if type(policy["representative_workload"]) is not bool:
        raise ValueError("representative_workload must be boolean")
    for key, minimum in (("min_runs", 2), ("min_successful_events_per_run", 1)):
        if type(policy[key]) is not int or policy[key] < minimum:
            raise ValueError(key + " is below its supported minimum")
    for key in ("min_elapsed_seconds", "max_relative_spread"):
        if not measurement(policy[key]) or (key == "min_elapsed_seconds" and policy[key] == 0):
            raise ValueError("invalid " + key)
    return policy


def compare(before, after, limits, resource_limits, generator_cpu_max=None, *, validation_policy=None):
    policy = validate_policy(validation_policy)
    base_conditions, baseline = before
    next_conditions, candidate = after
    runs = baseline + candidate
    missing, regressions = [], []
    comparable = bool(base_conditions) and base_conditions == next_conditions
    if not comparable:
        missing.append("missing or different replay conditions")
    minimum_runs = policy["min_runs"] if policy else 2
    if len(baseline) < minimum_runs or len(candidate) < minimum_runs:
        missing.append(f"{minimum_runs} or more runs required per variant")
    if any(not run["complete"] for run in runs):
        missing.append("missing or incomplete event logs")

    # Correctness evidence is independent of performance sampling/OS metric coverage.
    digests = [run["result_digest"] for run in runs]
    observed_mismatch = len({digest for digest in digests if digest}) > 1
    if not digests or not all(digests):
        missing.append("missing result checks")
    if comparable and baseline and candidate and observed_mismatch:
        correctness = "mismatch"
        regressions.append("result mismatch")
    elif comparable and baseline and candidate and all(digests):
        correctness = "match"
    else:
        correctness = "unknown"

    required = {"cpu_pct", "io_read_bytes", "wal_bytes", "oom_kills"}
    if policy is None:
        missing.append("validation policy missing: sample size, observation period, variability and representative workload")
    elif not policy["representative_workload"]:
        missing.append("representative workload not established by the validation plan")
    for group, observations in (("baseline", baseline), ("candidate", candidate)):
        starts = [run["started_at"] for run in observations]
        if len(set(starts)) != len(starts):
            missing.append(f"{group}: repeated start times; confirm these are separate trials")
        for index, run in enumerate(observations, 1):
            absent = sorted(name for name in required if not measurement(run["metrics"].get(name)))
            if absent:
                missing.append(f"{group} {index}: missing DB or OS metrics: {', '.join(absent)}")
            cpu = run["metrics"].get("load_generator_cpu_pct")
            if generator_cpu_max is not None and not measurement(cpu):
                missing.append(f"{group} {index}: missing load generator CPU")
            elif generator_cpu_max is not None and cpu > generator_cpu_max:
                missing.append(f"{group} {index}: load generator saturated ({cpu}%)")
            if policy:
                count = run.get("successful_count", run["counts"].get("ok", 0))
                if count < policy["min_successful_events_per_run"]:
                    missing.append(f"{group} {index}: insufficient successful events for latency percentiles")
                if run["elapsed_seconds"] < policy["min_elapsed_seconds"]:
                    missing.append(f"{group} {index}: observation period below validation plan")

    slo_violations = []
    slo_missing = not bool(candidate)
    for index, run in enumerate(candidate, 1):
        for name, limit in limits.items():
            value = run[name]
            if value is None:
                missing.append(f"candidate {index}: missing {name}")
                slo_missing = True
            elif (value < limit if name == "completed_tps" else value > limit):
                message = f"candidate {index}: {name}={value} violates {limit}"
                slo_violations.append(message)
                regressions.append(message)
        for name, limit in resource_limits.items():
            value = run["metrics"].get(name)
            if not measurement(value):
                missing.append(f"candidate {index}: missing {name}")
            elif value > limit:
                message = f"candidate {index}: {name}={value} exceeds {limit}"
                regressions.append(message)

    def spread(group):
        result = {}
        for name in ("offered_tps", "completed_tps", "loss_rate", "mean_ms", "p95_ms", "p99_ms", "schedule_lag_p95_ms", "retries"):
            values = [run[name] for run in group if run[name] is not None]
            result[name] = [min(values), median(values), max(values)] if values else None
        return result

    baseline_spread, candidate_spread = spread(baseline), spread(candidate)
    relative_spreads = {}
    for label, group in (("baseline", baseline_spread), ("candidate", candidate_spread)):
        relative_spreads[label] = {}
        for name in ("p95_ms", "completed_tps"):
            values = group[name]
            relative = ((values[2] - values[0]) / values[1] if values[1] > 0
                        else 0 if values[2] == values[0] else None) if values else None
            relative_spreads[label][name] = relative
            if policy and (relative is None or relative > policy["max_relative_spread"]):
                missing.append(f"{label}: {name} variability is missing or exceeds the validation plan")
    changes = {name: candidate_spread[name][1] - values[1]
               for name, values in baseline_spread.items() if values and candidate_spread[name]}
    # Preserve observed violations even when another assessment lacks evidence.
    decision = "rollback_candidate" if regressions else "investigate" if missing else "accept_candidate"
    return {"schema_version": 2, "decision": decision,
            "correctness": {"status": correctness, "observed_digest_difference": observed_mismatch,
                            "conditions_comparable": comparable},
            "performance": {"status": "slo_violations_observed" if slo_violations else
                            "unknown" if slo_missing else "slo_met_in_observed_runs",
                            "violations": slo_violations},
            "evidence_quality": {"status": "insufficient" if missing else "sufficient_for_declared_plan",
                                 "policy": policy, "relative_spreads": relative_spreads},
            "improvement": {"status": "not_established",
                            "reason": "SLO compliance and median differences alone do not establish a causal improvement."},
            "missing": list(dict.fromkeys(missing)), "regressions": regressions,
            "baseline": baseline, "candidate": candidate, "median_change": changes,
            "min_median_max": {"baseline": baseline_spread, "candidate": candidate_spread}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--p95-ms", type=float, required=True)
    parser.add_argument("--min-tps", type=float, required=True)
    parser.add_argument("--max-loss-rate", type=float, required=True)
    parser.add_argument("--metric-max", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--policy", type=Path,
                        help="JSON validation plan; required for accept_candidate, not for reporting violations")
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
        policy = json.loads(args.policy.read_text(encoding="utf-8")) if args.policy else None
        result = compare(read(args.baseline), read(args.candidate), limits, resource_limits,
                         args.load_generator_cpu_max, validation_policy=policy)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
