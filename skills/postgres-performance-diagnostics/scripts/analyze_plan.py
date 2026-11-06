#!/usr/bin/env python3
"""Inspect saved EXPLAIN (FORMAT JSON) output without connecting to a database."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys


METRICS = (
    "Subplans Removed", "WAL Records", "WAL FPI", "WAL Bytes",
    "Startup Cost", "Total Cost", "Plan Rows", "Plan Width", "Actual Startup Time",
    "Actual Total Time", "Actual Rows", "Actual Loops", "Rows Removed by Filter",
    "Rows Removed by Join Filter", "Rows Removed by Index Recheck", "Heap Fetches",
    "Shared Hit Blocks", "Shared Read Blocks", "Shared Dirtied Blocks", "Shared Written Blocks",
    "Local Hit Blocks", "Local Read Blocks", "Local Dirtied Blocks", "Local Written Blocks",
    "Temp Read Blocks", "Temp Written Blocks", "I/O Read Time", "I/O Write Time",
    "Shared I/O Read Time", "Shared I/O Write Time", "Local I/O Read Time", "Local I/O Write Time",
    "Temp I/O Read Time", "Temp I/O Write Time", "Sort Space Used", "Hash Buckets",
    "Original Hash Buckets", "Hash Batches", "Original Hash Batches", "Peak Memory Usage",
    "HashAgg Batches", "Disk Usage", "Workers Planned", "Workers Launched",
    "Exact Heap Blocks", "Lossy Heap Blocks", "Cache Hits", "Cache Misses", "Cache Evictions",
)
DETAIL_FIELDS = (
    "Filter", "Index Cond", "Hash Cond", "Merge Cond", "Join Filter", "Sort Key",
    "Group Key", "Output", "Function Call", "Recheck Cond",
)
PARAMETER = re.compile(r"\$[0-9]+")
LIMITATIONS = [
    "Candidates are investigation leads, not proof of a missing index or an unsafe setting.",
    "Actual Rows and rows removed are per-loop averages; total output is approximate when rounded.",
    "Costs, node times and buffer counters include child work; do not sum them over the tree.",
    "LIMIT, semi/anti joins and other early exits can prevent a node from running to completion.",
    "Parallel node averages and worker distributions require inspecting the original plan.",
    "A saved plan does not describe all parameter values, concurrency levels or cache conditions.",
    "Relation/index names remain visible. SQL expressions are omitted unless --include-details is used.",
    "Hotspot self time is Actual Total Time x loops minus children; parallel workers and CTE/SubPlan reuse make it approximate.",
]


def number(value):
    """Accept finite JSON numbers, excluding booleans."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def numeric_fields(data):
    return {key: data[key] for key in METRICS if number(data.get(key))}


def numeric_tree(data):
    """Keep numeric JIT measurements and booleans; avoid copying arbitrary text."""
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        if number(value) or isinstance(value, bool):
            result[key] = value
        elif isinstance(value, dict):
            result[key] = numeric_tree(value)
    return result


def documents(value):
    if isinstance(value, dict) and "plan" in value and isinstance(value["plan"], (dict, list)):
        value = value["plan"]
    if isinstance(value, dict) and "Node Type" in value:
        value = {"Plan": value}
    result = value if isinstance(value, list) else [value]
    if not result or any(not isinstance(item, dict) or not isinstance(item.get("Plan"), dict)
                         for item in result):
        raise ValueError("Expected EXPLAIN JSON: an object with Plan, or an array of such objects")
    return result


def analyze(data, include_details=False):
    report = {"schema_version": 1, "kind": "explain_analysis", "plans": [],
              "limitations": list(LIMITATIONS)}
    for plan_index, document in enumerate(documents(data)):
        plan = {"index": plan_index, "nodes": [], "candidates": [], "uses_parameter_placeholders": False}
        for source, target in (("Planning Time", "planning_time_ms"), ("Execution Time", "execution_time_ms")):
            if number(document.get(source)):
                plan[target] = document[source]
        if isinstance(document.get("JIT"), dict):
            plan["jit"] = numeric_tree(document["JIT"])
        triggers = document.get("Triggers")
        if isinstance(triggers, list):
            plan["triggers"] = [
                {key: trigger[key] for key in ("Trigger Name", "Relation", "Constraint Name", "Calls", "Time")
                 if key in trigger and (number(trigger[key]) if key in ("Calls", "Time")
                                        else isinstance(trigger[key], str))}
                for trigger in triggers if isinstance(trigger, dict)
            ]
            timed = [trigger for trigger in plan["triggers"] if trigger.get("Time", 0) > 0]
            slowest = max(timed, key=lambda trigger: trigger["Time"], default={})
            if slowest.get("Time", 0) >= max(10, plan.get("execution_time_ms", 0) * 0.1):
                plan["candidates"].append({"kind": "dml_trigger_time", "path": "Triggers", "evidence": slowest,
                    "next_step": "Check trigger timing, foreign key indexes and nested statements. Trigger time can overlap node time."})
        stack = [(document["Plan"], "Plan", False)]
        while stack:
            raw, path, below_limit = stack.pop()
            if not isinstance(raw, dict) or not isinstance(raw.get("Node Type"), str):
                raise ValueError("Every plan node must have a string Node Type")
            node = {"path": path, "node_type": raw["Node Type"], "metrics": numeric_fields(raw)}
            for source, target in (("Relation Name", "relation"), ("Schema", "schema"),
                                   ("Index Name", "index_name"), ("Parent Relationship", "relationship"),
                                   ("Sort Method", "sort_method"), ("Sort Space Type", "sort_space_type")):
                if isinstance(raw.get(source), str):
                    node[target] = raw[source]
            # $n in a condition means a generic plan (prepared statement); only a flag is kept, never the text.
            if any(isinstance(raw.get(key), str) and PARAMETER.search(raw[key]) for key in DETAIL_FIELDS if key != "Output"):
                plan["uses_parameter_placeholders"] = True
            if include_details:
                node["details"] = {key: raw[key] for key in DETAIL_FIELDS if key in raw}
            loops, actual, estimated = (raw.get(key) for key in ("Actual Loops", "Actual Rows", "Plan Rows"))
            executed = number(loops) and loops > 0
            cardinality = {"status": "not_measured"}
            if number(loops) and loops == 0:
                cardinality["status"] = "not_executed"
            elif executed and number(actual) and number(estimated):
                cardinality = {"status": "partial_under_limit" if below_limit else "observed",
                               "actual_rows_per_loop": actual, "plan_rows": estimated,
                               "loops": loops, "approximate_total_output_rows": actual * loops,
                               "actual_over_estimate": actual / estimated if estimated > 0 else None}
                if not below_limit:
                    factor = max(actual, estimated) / min(actual, estimated) if min(actual, estimated) > 0 else None
                    if max(actual, estimated) >= 1000 and (factor is None or factor >= 10):
                        plan["candidates"].append({"kind": "cardinality_mismatch", "path": path,
                                                   "evidence": cardinality.copy(),
                                                   "next_step": "Check early exits, parameter skew, ANALYZE and column correlation before changing indexes."})
            node["cardinality"] = cardinality
            if executed:
                removed = raw.get("Rows Removed by Filter")
                # Per-loop values: a small removal repeated by a nested loop is still large in total.
                if (number(removed) and number(actual) and removed * loops >= 10000
                        and removed / max(removed + actual, 1) >= 0.9):
                    plan["candidates"].append({"kind": "heavy_filtering", "path": path,
                                               "evidence": {"removed_per_loop": removed, "output_per_loop": actual, "loops": loops,
                                                            "approximate_total_removed": removed * loops},
                                               "next_step": "Check predicate selectivity and scan cost; a sequential scan may still be appropriate."})
                recheck = raw.get("Rows Removed by Index Recheck")
                if number(raw.get("Lossy Heap Blocks")) and raw["Lossy Heap Blocks"] > 0:
                    plan["candidates"].append({"kind": "lossy_bitmap", "path": path,
                                               "evidence": {"lossy_heap_blocks": raw["Lossy Heap Blocks"],
                                                            "exact_heap_blocks": raw.get("Exact Heap Blocks"),
                                                            "removed_by_recheck_per_loop": recheck},
                                               "next_step": "The bitmap exceeded work_mem and rechecks whole pages; compare selectivity, a narrower index and work_mem for this query."})
                if raw["Node Type"] == "Index Only Scan" and number(raw.get("Heap Fetches")) and raw["Heap Fetches"] >= 1000:
                    plan["candidates"].append({"kind": "index_only_heap_fetches", "path": path,
                                               "evidence": {"heap_fetches": raw["Heap Fetches"], "actual_rows_per_loop": actual, "loops": loops},
                                               "next_step": "Pages not all-visible force heap visits; check VACUUM frequency and the visibility map for this table."})
                if raw["Node Type"] == "Seq Scan" and loops >= 100:
                    plan["candidates"].append({"kind": "repeated_seq_scan", "path": path,
                                               "evidence": {"loops": loops, "rows_removed_per_loop": removed},
                                               "next_step": "A sequential scan re-executed per outer row; check the join method, join-key index and row estimates of the outer side."})
                batches = raw.get("Hash Batches", raw.get("HashAgg Batches"))
                if number(batches) and batches > 1:
                    plan["candidates"].append({"kind": "hash_batches", "path": path,
                                               "evidence": {"batches": batches},
                                               "next_step": "Check temp I/O, input cardinality, work_mem and concurrent hash memory budgets."})
                if number(raw.get("Disk Usage")) and raw["Disk Usage"] > 0:
                    plan["candidates"].append({"kind": "aggregate_disk_usage", "path": path,
                                               "evidence": {"disk_kb": raw["Disk Usage"]},
                                               "next_step": "Check aggregate input size and temp I/O before allocating more memory."})
            sort_sources = [(raw, path)]
            workers = raw.get("Workers", [])
            if not isinstance(workers, list):
                raise ValueError("Workers must be an array when present")
            worker_summaries = []
            for worker_index, worker in enumerate(workers):
                if not isinstance(worker, dict):
                    raise ValueError("Workers entries must be objects")
                summary = numeric_fields(worker)
                if number(worker.get("Worker Number")):
                    summary["Worker Number"] = worker["Worker Number"]
                worker_summaries.append(summary)
                sort_sources.append((worker, f"{path}/Workers/{worker_index}"))
            if worker_summaries:
                node["workers"] = worker_summaries
            for source, sort_path in sort_sources:
                if source.get("Sort Space Type") == "Disk" and number(source.get("Sort Space Used")):
                    plan["candidates"].append({"kind": "sort_spill", "path": sort_path,
                                               "evidence": {"disk_kb": source["Sort Space Used"]},
                                               "next_step": "Compare filtering, order-compatible indexes and per-operation memory within a concurrency budget."})
                for group_name in ("Full-sort Groups", "Pre-sorted Groups"):
                    group = source.get(group_name, {})
                    if not isinstance(group, dict):
                        continue
                    disk = group.get("Sort Space Disk", {})
                    if isinstance(disk, dict) and number(disk.get("Peak Sort Space Used")) and disk["Peak Sort Space Used"] > 0:
                        plan["candidates"].append({"kind": "incremental_sort_spill", "path": sort_path,
                                                   "evidence": {"group": group_name, "peak_disk_kb": disk["Peak Sort Space Used"]},
                                                   "next_step": "Inspect the incremental sort group distribution and concurrency memory budget."})
            planned, launched = raw.get("Workers Planned"), raw.get("Workers Launched")
            if number(planned) and number(launched) and launched < planned:
                plan["candidates"].append({"kind": "parallel_worker_shortfall", "path": path,
                                           "evidence": {"planned": planned, "launched": launched},
                                           "next_step": "Check available parallel workers and concurrency at execution time."})
            plan["nodes"].append(node)
            children = raw.get("Plans", [])
            if not isinstance(children, list):
                raise ValueError("Plans must be an array when present")
            for child_index in reversed(range(len(children))):
                stack.append((children[child_index], f"{path}/Plans/{child_index}", below_limit or raw["Node Type"] == "Limit"))
        plan["root_buffers"] = {key: value for key, value in plan["nodes"][0]["metrics"].items() if "Blocks" in key}
        children_by_path = {}
        for child in plan["nodes"][1:]:
            children_by_path.setdefault(child["path"].rsplit("/Plans/", 1)[0], []).append(child)
        plan["partition_evidence"] = [
            {"path": node["path"], "subplans_removed": node["metrics"].get("Subplans Removed"),
             "children": [{"path": child["path"], "relation": child.get("relation"),
                           "actual_loops": child["metrics"].get("Actual Loops")}
                          for child in children_by_path.get(node["path"], [])]}
            for node in plan["nodes"]
            if node["node_type"] in ("Append", "Merge Append")
        ]
        plan["hotspots"] = hotspots(plan["nodes"], plan.get("execution_time_ms"))
        plan["candidates"] += join_candidates(plan["nodes"])
        if plan["uses_parameter_placeholders"] and any(c["kind"] == "cardinality_mismatch" for c in plan["candidates"]):
            plan["candidates"].append({"kind": "generic_plan_misestimate", "path": "Plan", "evidence": {},
                                       "next_step": "A generic plan was misestimated; compare custom plans for representative parameter values before changing plan_cache_mode for that statement."})
        plan_ms, exec_ms = plan.get("planning_time_ms"), plan.get("execution_time_ms")
        if number(plan_ms) and number(exec_ms) and plan_ms >= 10 and plan_ms > exec_ms:
            plan["candidates"].append({"kind": "planning_dominates", "path": "Plan",
                                       "evidence": {"planning_ms": plan_ms, "execution_ms": exec_ms},
                                       "next_step": "Check partition count and pruning, join count, statistics targets and whether a prepared statement could reuse the plan."})
        jit_ms = plan.get("jit", {}).get("Timing", {}).get("Total")
        if number(jit_ms) and number(exec_ms) and jit_ms >= 10 and jit_ms >= 0.2 * exec_ms:
            plan["candidates"].append({"kind": "jit_overhead", "path": "Plan",
                                       "evidence": {"jit_total_ms": jit_ms, "execution_ms": exec_ms},
                                       "next_step": "JIT compile time is a large share; compare with jit_above_cost raised or jit off for this workload."})
        report["plans"].append(plan)
    return report


def join_candidates(nodes):
    """A join that keeps <=1% of a large scanned input: an index on the join key may avoid the scan."""
    by_path = {node["path"]: node for node in nodes}
    found = []
    for node in nodes:
        if node["node_type"] not in ("Hash Join", "Merge Join", "Nested Loop") or node["cardinality"]["status"] != "observed":
            continue
        output = node["cardinality"]["approximate_total_output_rows"]
        for index in range(2):
            child = by_path.get(f"{node['path']}/Plans/{index}")
            if child and child["node_type"] == "Hash":
                child = by_path.get(child["path"] + "/Plans/0")
            if not child or "Scan" not in child["node_type"] or child["cardinality"]["status"] != "observed":
                continue
            scanned = child["cardinality"]["approximate_total_output_rows"]
            if scanned >= 100000 and output <= scanned * 0.01:
                found.append({"kind": "join_discards_scanned_rows", "path": child["path"],
                              "evidence": {"join_path": node["path"], "join_type": node["node_type"],
                                           "scanned_rows": scanned, "join_output_rows": output},
                              "next_step": "Most scanned rows find no join partner; check an index on this side's join key or filtering before the join."})
    return found


def hotspots(nodes, execution_ms, limit=5):
    """Rank nodes by approximate self time so the reader starts where time is actually spent."""
    inclusive = {}
    for node in nodes:
        metrics = node["metrics"]
        if number(metrics.get("Actual Total Time")) and number(metrics.get("Actual Loops")):
            inclusive[node["path"]] = metrics["Actual Total Time"] * metrics["Actual Loops"]
    result = []
    for node in nodes:
        path = node["path"]
        if path not in inclusive:
            continue
        children = sum(value for child, value in inclusive.items()
                       if child.startswith(path + "/Plans/") and "/Plans/" not in child[len(path) + 7:])
        self_ms = max(inclusive[path] - children, 0)
        result.append({"path": path, "node_type": node["node_type"], "relation": node.get("relation"),
                       "self_ms_approx": self_ms,
                       "share_of_execution": self_ms / execution_ms if number(execution_ms) and execution_ms > 0 else None})
    return sorted(result, key=lambda x: x["self_ms_approx"], reverse=True)[:limit]


def markdown(report):
    lines = ["# Execution plan observations", "", "Candidates require confirmation with representative parameters and workload.", ""]
    for plan in report["plans"]:
        lines += [f"## Plan {plan['index'] + 1}", "",
                  f"Planning: {plan.get('planning_time_ms', 'not measured')} ms; execution: {plan.get('execution_time_ms', 'not measured')} ms.", "",
                  "Root buffer counts (not a sum of all nodes): " + json.dumps(plan["root_buffers"], ensure_ascii=False), ""]
        if "jit" in plan:
            lines += ["JIT: " + json.dumps(plan["jit"], ensure_ascii=False), ""]
        if plan.get("triggers"):
            lines += ["### Trigger evidence", "", "Trigger times can overlap plan node times.", "",
                      json.dumps(plan["triggers"], ensure_ascii=False), ""]
        if plan.get("partition_evidence"):
            lines += ["### Append and pruning evidence", "",
                      json.dumps(plan["partition_evidence"], ensure_ascii=False), ""]
        if plan["hotspots"]:
            lines += ["### Self-time hotspots (approximate)", ""]
            for spot in plan["hotspots"]:
                share = f"{spot['share_of_execution']:.0%}" if spot["share_of_execution"] is not None else "—"
                lines.append(f"- `{spot['path']}` {spot['node_type']} {spot.get('relation') or ''}: {spot['self_ms_approx']:.3f} ms ({share})")
            lines.append("")
        for node in plan["nodes"]:
            identity = ".".join(str(node.get(key, "")) for key in ("schema", "relation")).strip(".")
            lines += [f"- `{node['path']}`: {node['node_type']} {identity}; cardinality={json.dumps(node['cardinality'], ensure_ascii=False)}",
                      "  Metrics: " + json.dumps(node["metrics"], ensure_ascii=False)]
            if node.get("index_name"):
                lines.append("  Index: " + node["index_name"])
            if "details" in node:
                lines.append("  Details: " + json.dumps(node["details"], ensure_ascii=False))
        lines += ["", "### Investigation candidates", ""]
        for candidate in plan["candidates"]:
            lines.append(f"- {candidate['kind']} at `{candidate['path']}`: {json.dumps(candidate['evidence'], ensure_ascii=False)}. {candidate['next_step']}")
        if not plan["candidates"]:
            lines.append("No rule matched. This is not a conclusion that the query is optimal.")
        lines.append("")
    lines += ["## Limits", ""] + ["- " + item for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Saved EXPLAIN FORMAT JSON file")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--include-details", action="store_true", help="Include potentially sensitive SQL expressions")
    args = parser.parse_args(argv)
    try:
        with args.input.open(encoding="utf-8-sig") as handle:
            data = json.load(handle)
        report = analyze(data, include_details=args.include_details)
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json" else markdown(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0
    except (OSError, ValueError, RecursionError) as error:
        # Do not echo malformed source fragments (which may contain SQL secrets).
        message = "Invalid JSON or plan structure" if isinstance(error, (ValueError, RecursionError)) else "Cannot read input or write output"
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
