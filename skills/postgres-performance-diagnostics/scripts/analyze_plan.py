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
    # PostgreSQL 18: index descents per scan node, and tuplestore size of Material/CTE/WindowAgg nodes.
    "Index Searches", "Maximum Storage", "WAL Buffers Full",
)
# Shown in the Markdown digest next to the row counts; the JSON report keeps every metric.
DIGEST_METRICS = (
    "Actual Total Time", "Rows Removed by Filter", "Rows Removed by Join Filter", "Rows Removed by Index Recheck",
    "Heap Fetches", "Index Searches", "Shared Hit Blocks", "Shared Read Blocks", "Temp Read Blocks",
    "Temp Written Blocks", "Shared I/O Read Time", "I/O Read Time", "Sort Space Used", "Hash Batches",
    "Disk Usage", "Maximum Storage", "Workers Planned", "Workers Launched", "Subplans Removed", "WAL Bytes",
)
DETAIL_FIELDS = (
    "Filter", "Index Cond", "Hash Cond", "Merge Cond", "Join Filter", "Sort Key",
    "Group Key", "Output", "Function Call", "Recheck Cond",
)
LIMITATIONS = [
    "Candidates are investigation leads, not proof of a missing index or an unsafe setting.",
    "Actual Rows and rows removed are per-loop averages; total output is approximate when rounded.",
    "Costs, node times and buffer counters include child work; do not sum them over the tree.",
    "LIMIT, semi/anti joins and other early exits can prevent a node from running to completion. Nodes under a "
    "Limit and the inner side of a semi/anti nested loop are marked partial and not judged; EXISTS subplans and "
    "merge joins that stop early are not recognized.",
    "Row-estimate mismatches compare estimate and actual from one row up and weigh them over all loops.",
    "An auto_explain plan has no Execution Time; shares then use the logged duration or the root node's time.",
    "Parallel node averages and worker distributions require inspecting the original plan.",
    "A saved plan does not describe all parameter values, concurrency levels or cache conditions.",
    "Parameter tokens alone cannot establish generic/custom plan mode; known mode requires capture provenance.",
    "Lossy bitmap blocks are observations, not proof of work_mem exhaustion; BRIN is intrinsically lossy.",
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



def parameter_tokens(expression):
    """Lex $n tokens, ignoring SQL strings, quoted identifiers and comments."""
    tokens, index = set(), 0
    while index < len(expression):
        char = expression[index]
        if char in ("'", '"'):
            quote = char
            escaped = (quote == "'" and index > 0 and expression[index - 1] in "eE"
                       and (index < 2 or not (expression[index - 2].isalnum() or expression[index - 2] == "_")))
            index += 1
            while index < len(expression):
                if escaped and expression[index] == "\\":
                    index += 2
                elif expression[index] == quote:
                    index += 1
                    if index < len(expression) and expression[index] == quote:
                        index += 1
                    else:
                        break
                else:
                    index += 1
            continue
        if expression.startswith("--", index):
            end = expression.find("\n", index + 2)
            index = len(expression) if end < 0 else end + 1
            continue
        if expression.startswith("/*", index):
            depth, index = 1, index + 2
            while index < len(expression) and depth:
                if expression.startswith("/*", index):
                    depth, index = depth + 1, index + 2
                elif expression.startswith("*/", index):
                    depth, index = depth - 1, index + 2
                else:
                    index += 1
            continue
        if char == "$" and (index == 0 or not (expression[index - 1].isalnum() or expression[index - 1] in "_$")):
            delimiter = re.match(r"\$(?:[^\W\d]\w*)?\$", expression[index:])
            if delimiter:
                end = expression.find(delimiter[0], index + len(delimiter[0]))
                index = len(expression) if end < 0 else end + len(delimiter[0])
                continue
            parameter = re.match(r"\$[0-9]+(?![\w$])", expression[index:])
            if parameter:
                tokens.add(parameter[0])
                index += len(parameter[0])
                continue
        index += 1
    return tokens


def initplan_parameters(root):
    """Older EXPLAIN versions name executor parameters in '(returns $n)'."""
    parameters, stack = set(), [root]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("Parent Relationship") == "InitPlan":
            parameters.update(re.findall(r"\(returns (\$[0-9]+)\)", str(node.get("Subplan Name", ""))))
        children = node.get("Plans", [])
        if isinstance(children, list):
            stack.extend(children)
    return parameters


def bitmap_access_methods(node, relation_evidence):
    """Match inspect_relation.sql evidence only with exact schema/table/index identities."""
    reports = relation_evidence if isinstance(relation_evidence, list) else [relation_evidence]
    if relation_evidence is not None and not isinstance(relation_evidence, (dict, list)):
        raise ValueError("Invalid relation evidence")
    methods, names = {}, []
    for report in reports:
        if report is None:
            continue
        if not isinstance(report, dict):
            raise ValueError("Invalid relation evidence")
        relation = report.get("relation", {})
        if not isinstance(relation, dict) or not isinstance(report.get("indexes", []), list):
            raise ValueError("Invalid relation evidence")
        if (not node.get("Schema") or not node.get("Relation Name")
                or relation.get("schema") != node["Schema"] or relation.get("name") != node["Relation Name"]):
            continue
        for item in report.get("indexes", []):
            if isinstance(item, dict) and isinstance(item.get("access_method"), str):
                methods.setdefault(item.get("index_name"), set()).add(item["access_method"])
    stack = list(node.get("Plans", []))
    while stack:
        child = stack.pop()
        if not isinstance(child, dict):
            continue
        if child.get("Node Type") == "Bitmap Index Scan":
            names.append(child.get("Index Name"))
        elif child.get("Node Type") in ("BitmapAnd", "BitmapOr"):
            stack.extend(child.get("Plans", []))
    matched = [next(iter(methods[name])) for name in names if len(methods.get(name, set())) == 1]
    return sorted(set(matched)), bool(names) and len(matched) == len(names)


def analyze(data, include_details=False, *, plan_mode=None, plan_source=None, relation_evidence=None):
    capture = data.get("capture", {}) if isinstance(data, dict) else {}
    if not isinstance(capture, dict):
        raise ValueError("Invalid plan capture metadata")
    mode = plan_mode or capture.get("plan_mode", "unknown")
    source = plan_source if plan_source is not None else capture.get("source")
    if mode not in ("unknown", "generic", "custom"):
        raise ValueError("Invalid plan mode")
    if source is not None and not isinstance(source, str):
        raise ValueError("Invalid capture source")
    if mode != "unknown" and (not isinstance(source, str) or not source.strip()):
        raise ValueError("Known plan mode requires a capture source")
    logged_ms = capture.get("logged_duration_ms")
    capture = {"plan_mode": mode, "source": source}

    report = {"schema_version": 1, "kind": "explain_analysis", "plans": [],
              "limitations": list(LIMITATIONS)}
    for plan_index, document in enumerate(documents(data)):
        plan = {"index": plan_index, "nodes": [], "candidates": [], "uses_parameter_placeholders": False,
                "plan_capture": capture}
        internal_parameters = initplan_parameters(document["Plan"])
        for source, target in (("Planning Time", "planning_time_ms"), ("Execution Time", "execution_time_ms")):
            if number(document.get(source)):
                plan[target] = document[source]
        # auto_explain logs no Execution Time; the duration in its log line is the closest measure.
        if number(logged_ms):
            plan["logged_duration_ms"] = logged_ms
        # Non-default planner settings (EXPLAIN SETTINGS) explain a plan that production would not choose.
        if isinstance(document.get("Settings"), dict):
            plan["settings"] = {key: value for key, value in document["Settings"].items()
                                if isinstance(key, str) and isinstance(value, str)}
        identifier = document.get("Query Identifier")
        if isinstance(identifier, int) and not isinstance(identifier, bool):
            plan["query_id"] = str(identifier)  # Links the plan to pg_stat_statements and wait samples.
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
        # early: why a node may have stopped before reading all its rows ("limit" or "semi_join").
        stack = [(document["Plan"], "Plan", None)]
        while stack:
            raw, path, early = stack.pop()
            below_limit = early == "limit"
            if not isinstance(raw, dict) or not isinstance(raw.get("Node Type"), str):
                raise ValueError("Every plan node must have a string Node Type")
            node = {"path": path, "node_type": raw["Node Type"], "metrics": numeric_fields(raw)}
            for source, target in (("Relation Name", "relation"), ("Schema", "schema"),
                                   ("Index Name", "index_name"), ("Parent Relationship", "relationship"),
                                   ("Join Type", "join_type"), ("Storage", "storage"),
                                   ("Sort Method", "sort_method"), ("Sort Space Type", "sort_space_type")):
                if isinstance(raw.get(source), str):
                    node[target] = raw[source]
            # Tokens are parameter evidence; capture context establishes generic/custom mode.
            expressions = []
            for key in DETAIL_FIELDS:
                if key == "Output":
                    continue
                value = raw.get(key)
                expressions.extend([value] if isinstance(value, str) else
                                   [item for item in value if isinstance(item, str)] if isinstance(value, list) else [])
            if any(parameter_tokens(expression) - internal_parameters for expression in expressions):
                plan["uses_parameter_placeholders"] = True
            if include_details:
                node["details"] = {key: raw[key] for key in DETAIL_FIELDS if key in raw}
            loops, actual, estimated = (raw.get(key) for key in ("Actual Loops", "Actual Rows", "Plan Rows"))
            executed = number(loops) and loops > 0
            cardinality = {"status": "not_measured"}
            if number(loops) and loops == 0:
                cardinality["status"] = "not_executed"
            elif executed and number(actual) and number(estimated):
                status = ("partial_under_limit" if below_limit else
                          "partial_early_exit" if early else "observed")
                cardinality = {"status": status,
                               "actual_rows_per_loop": actual, "plan_rows": estimated,
                               "loops": loops, "approximate_total_output_rows": actual * loops,
                               "actual_over_estimate": actual / estimated if estimated > 0 else None}
                if not early:
                    # The planner never estimates below one row, and an actual 0 can be a rounded
                    # per-loop average: compare both from one row up. Size is judged over all
                    # loops, because a small per-loop miss on the inner side of a nested loop
                    # multiplies into the join's row count.
                    low, high = sorted((max(actual, 1), max(estimated, 1)))
                    if high * loops >= 1000 and high / low >= 10:
                        plan["candidates"].append({"kind": "cardinality_mismatch", "path": path,
                                                   "evidence": cardinality.copy(),
                                                   "next_step": "Check early exits, parameter skew, ANALYZE and column correlation before changing indexes."})
            node["cardinality"] = cardinality
            if executed:
                join_removed = raw.get("Rows Removed by Join Filter")
                if (number(join_removed) and number(actual) and join_removed * loops >= 100000
                        and join_removed / max(join_removed + actual, 1) >= 0.9):
                    plan["candidates"].append({"kind": "join_filter_discards", "path": path,
                                               "evidence": {"removed_per_loop": join_removed, "output_per_loop": actual,
                                                            "loops": loops,
                                                            "approximate_total_removed": join_removed * loops},
                                               "next_step": "Rows were paired and then rejected by the join filter; check for an "
                                                            "equality join key that an index or hash join could use, and the join order."})
                if raw.get("Storage") == "Disk" and number(raw.get("Maximum Storage")):
                    plan["candidates"].append({"kind": "tuplestore_disk", "path": path,
                                               "evidence": {"maximum_storage_kb": raw["Maximum Storage"]},
                                               "next_step": "A materialized result (Material, CTE or WindowAgg) exceeded work_mem and went "
                                                            "to disk; check whether it must be materialized, its rows and width, then scoped work_mem."})
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
                    methods, complete_methods = bitmap_access_methods(raw, relation_evidence)
                    advice = ("BRIN is intrinsically lossy; inspect pages_per_range, physical correlation and recheck work. "
                              if "brin" in methods else "Inspect index access methods, selectivity and recheck work. ")
                    plan["candidates"].append({"kind": "lossy_bitmap", "path": path,
                                               "evidence": {"lossy_heap_blocks": raw["Lossy Heap Blocks"],
                                                            "exact_heap_blocks": raw.get("Exact Heap Blocks"),
                                                            "removed_by_recheck_per_loop": recheck,
                                                            "index_access_methods": methods,
                                                            "access_methods_complete": complete_methods,
                                                            "cause_status": "unconfirmed"},
                                               "next_step": advice + "Lossy blocks alone do not prove memory pressure; "
                                                            "test work_mem only as a measured hypothesis."})
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
            # A semi or anti nested loop stops reading its inner side at the first match, so that
            # side's actual rows are not comparable with its estimate (which is for a full scan).
            stops_inner = raw["Node Type"] == "Nested Loop" and raw.get("Join Type") in ("Semi", "Anti")
            for child_index in reversed(range(len(children))):
                child = children[child_index]
                inherited = early or ("limit" if raw["Node Type"] == "Limit" else None)
                if (not inherited and stops_inner and isinstance(child, dict)
                        and child.get("Parent Relationship") == "Inner"):
                    inherited = "semi_join"
                stack.append((child, f"{path}/Plans/{child_index}", inherited))
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
        # Shares need a total: EXPLAIN's Execution Time, else the duration auto_explain logged,
        # else the root node's own inclusive time (which excludes triggers and planning).
        root_metrics = plan["nodes"][0]["metrics"]
        root_ms = (root_metrics["Actual Total Time"] * root_metrics["Actual Loops"]
                   if number(root_metrics.get("Actual Total Time")) and number(root_metrics.get("Actual Loops")) else None)
        basis = next(((name, value) for name, value in (("execution_time", plan.get("execution_time_ms")),
                                                        ("logged_duration", plan.get("logged_duration_ms")),
                                                        ("root_node", root_ms)) if number(value) and value > 0), (None, None))
        plan["time_basis"] = basis[0]
        plan["hotspots"] = hotspots(plan["nodes"], basis[1])
        plan["candidates"] += join_candidates(plan["nodes"])
        if mode == "generic" and any(c["kind"] == "cardinality_mismatch" for c in plan["candidates"]):
            plan["candidates"].append({"kind": "generic_plan_misestimate", "path": "Plan",
                                       "evidence": {"plan_capture": capture},
                                       "next_step": "A captured generic plan has a row-estimate mismatch; compare custom plans "
                                                    "over representative parameters before attributing the mismatch to plan mode."})
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
        # An anti join returns the rows WITHOUT a partner, so a small output means the opposite.
        # A semi join returns outer rows only: its inner side says nothing about unmatched rows.
        if node.get("join_type") == "Anti":
            continue
        for index in range(1 if node.get("join_type") == "Semi" else 2):
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


def node_line(node):
    """One line per node for the Markdown digest: identity, estimate against actual, non-zero work."""
    identity = ".".join(str(node.get(key, "")) for key in ("schema", "relation")).strip(".")
    parts = [f"`{node['path']}` {node['node_type']}"]
    parts += [str(node[key]) for key in ("join_type",) if node.get(key)]
    if identity:
        parts.append("on " + identity)
    if node.get("index_name"):
        parts.append("using " + node["index_name"])
    cardinality = node["cardinality"]
    if "plan_rows" in cardinality:
        rows = (f"rows est {cardinality['plan_rows']:g} / act {cardinality['actual_rows_per_loop']:g}"
                f" x{cardinality['loops']:g}")
        if cardinality["status"] != "observed":
            rows += f" [{cardinality['status']}]"
    else:
        rows = cardinality["status"].replace("_", " ")
    metrics = {key: node["metrics"][key] for key in DIGEST_METRICS if node["metrics"].get(key)}
    extras = {key: node[key] for key in ("sort_method", "sort_space_type", "storage") if node.get(key)}
    text = " ".join(parts) + ": " + rows
    if metrics or extras:
        text += "; " + json.dumps({**extras, **metrics}, ensure_ascii=False, separators=(", ", ": "))
    return text


def markdown(report, max_nodes=80):
    lines = ["# Execution plan observations", "", "Candidates require confirmation with representative parameters and workload.", ""]
    for plan in report["plans"]:
        lines += [f"## Plan {plan['index'] + 1}", "",
                  f"Planning: {plan.get('planning_time_ms', 'not measured')} ms; execution: {plan.get('execution_time_ms', 'not measured')} ms.", "",
                  "Plan capture: " + json.dumps(plan["plan_capture"], ensure_ascii=False), "",
                  "Root buffer counts (not a sum of all nodes): " + json.dumps(plan["root_buffers"], ensure_ascii=False), ""]
        if plan.get("logged_duration_ms") is not None:
            lines += [f"Logged duration (auto_explain): {plan['logged_duration_ms']} ms.", ""]
        if plan.get("query_id"):
            lines += [f"Query ID: {plan['query_id']}", ""]
        if plan.get("settings"):
            lines += ["Non-default planner settings in effect: " + json.dumps(plan["settings"], ensure_ascii=False), ""]
        if "jit" in plan:
            lines += ["JIT: " + json.dumps(plan["jit"], ensure_ascii=False), ""]
        if plan.get("triggers"):
            lines += ["### Trigger evidence", "", "Trigger times can overlap plan node times.", "",
                      json.dumps(plan["triggers"], ensure_ascii=False), ""]
        if plan.get("partition_evidence"):
            lines += ["### Append and pruning evidence", "",
                      json.dumps(plan["partition_evidence"], ensure_ascii=False), ""]
        if plan["hotspots"]:
            basis = {"execution_time": "of Execution Time", "logged_duration": "of the logged duration",
                     "root_node": "of the root node's time"}.get(plan.get("time_basis"))
            lines += ["### Self-time hotspots (approximate" + (f"; share {basis}" if basis else "") + ")", ""]
            for spot in plan["hotspots"]:
                share = f"{spot['share_of_execution']:.0%}" if spot["share_of_execution"] is not None else "—"
                lines.append(f"- `{spot['path']}` {spot['node_type']} {spot.get('relation') or ''}: {spot['self_ms_approx']:.3f} ms ({share})")
            lines.append("")
        lines += ["### Nodes (rows and times are per loop; the JSON report has every metric)", ""]
        for node in plan["nodes"][:max_nodes]:
            lines.append("- " + node_line(node))
            if "details" in node:
                lines.append("  Details: " + json.dumps(node["details"], ensure_ascii=False))
        if len(plan["nodes"]) > max_nodes:
            lines.append(f"- {len(plan['nodes']) - max_nodes} more node(s) not shown; raise --max-nodes or use --format json.")
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
    parser.add_argument("--plan-mode", choices=("unknown", "generic", "custom"),
                        help="Known mode from the capture session, never inferred from placeholder text")
    parser.add_argument("--plan-source", help="Non-secret provenance locator; required for known plan mode")
    parser.add_argument("--relation-evidence", type=Path,
                        help="inspect_relation.sql JSON (one object or a list); use VERBOSE plans for schema matching")
    parser.add_argument("--max-nodes", type=int, default=80,
                        help="Nodes listed per plan in Markdown (the JSON report always has all)")
    args = parser.parse_args(argv)
    if args.max_nodes <= 0:
        parser.error("--max-nodes must be positive")
    try:
        with args.input.open(encoding="utf-8-sig") as handle:
            data = json.load(handle)
        relation_evidence = None
        if args.relation_evidence:
            with args.relation_evidence.open(encoding="utf-8-sig") as handle:
                relation_evidence = json.load(handle)
        report = analyze(data, include_details=args.include_details, plan_mode=args.plan_mode,
                         plan_source=args.plan_source, relation_evidence=relation_evidence)
        output = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json"
                  else markdown(report, args.max_nodes))
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
