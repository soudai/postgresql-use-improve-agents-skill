#!/usr/bin/env python3
"""Compare two collected snapshots without interpreting counters as proof of a cause."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys

PGSS_COUNTERS = (
    "calls", "plans", "total_exec_time", "total_plan_time", "rows",
    "shared_blks_hit", "shared_blks_read", "shared_blks_dirtied", "shared_blks_written",
    "local_blks_hit", "local_blks_read", "local_blks_dirtied", "local_blks_written",
    "temp_blks_read", "temp_blks_written", "shared_blk_read_time", "shared_blk_write_time",
    "local_blk_read_time", "local_blk_write_time", "temp_blk_read_time", "temp_blk_write_time",
    "wal_records", "wal_fpi", "wal_bytes", "wal_buffers_full", "jit_functions", "jit_generation_time",
    "jit_inlining_count", "jit_inlining_time", "jit_optimization_count", "jit_optimization_time",
    "jit_emission_count", "jit_emission_time", "jit_deform_count", "jit_deform_time",
    "parallel_workers_to_launch", "parallel_workers_launched", "generic_plan_calls", "custom_plan_calls",
)
SPECS = {
    "pgss": (("dbid", "userid", "queryid", "toplevel"), PGSS_COUNTERS),
    "database": (("datid",), ("xact_commit", "xact_rollback", "blks_read", "blks_hit",
        "tup_returned", "tup_fetched", "tup_inserted", "tup_updated", "tup_deleted",
        "temp_files", "temp_bytes", "deadlocks", "blk_read_time", "blk_write_time",
        "sessions", "sessions_abandoned", "sessions_fatal", "sessions_killed", "session_time",
        "active_time", "idle_in_transaction_time", "conflicts", "checksum_failures",
        "parallel_workers_to_launch", "parallel_workers_launched")),
    "tables": (("relid",), ("seq_scan", "seq_tup_read", "idx_scan", "idx_tup_fetch",
        "n_tup_ins", "n_tup_upd", "n_tup_del", "n_tup_hot_upd", "n_tup_newpage_upd",
        "vacuum_count", "autovacuum_count", "analyze_count", "autoanalyze_count",
        "total_vacuum_time", "total_autovacuum_time", "total_analyze_time", "total_autoanalyze_time")),
    "indexes": (("indexrelid",), ("idx_scan", "idx_tup_read", "idx_tup_fetch")),
    "table_io": (("relid",), tuple(f"{kind}_blks_{op}" for kind in ("heap", "idx", "toast", "tidx")
                                     for op in ("read", "hit"))),
    "index_io": (("indexrelid",), ("idx_blks_read", "idx_blks_hit")),
    "io": (("backend_type", "object", "context"), ("reads", "read_bytes", "read_time",
        "writes", "write_bytes", "write_time", "writebacks", "writeback_time", "extends",
        "extend_bytes", "extend_time", "hits", "evictions", "reuses", "fsyncs", "fsync_time")),
    "wal": ((), ("wal_records", "wal_fpi", "wal_bytes", "wal_buffers_full")),
    "checkpointer": ((), ("num_timed", "num_requested", "num_done", "restartpoints_timed",
        "restartpoints_req", "restartpoints_done", "write_time", "sync_time", "buffers_written",
        "slru_written")),
    "bgwriter": ((), ("buffers_clean", "maxwritten_clean", "buffers_alloc")),
    "archiver": ((), ("archived_count", "failed_count")),
    "conflicts": (("datid",), ("confl_tablespace", "confl_lock", "confl_snapshot",
        "confl_bufferpin", "confl_deadlock", "confl_active_logicalslot")),
    "replication_slots": (("slot_name",), ("spill_txns", "spill_count", "spill_bytes",
        "stream_txns", "stream_count", "stream_bytes", "total_txns", "total_bytes")),
}
IDENTITY = ("relfilenode", "relname", "indexrelname", "schemaname", "relid")
LIMITATIONS = [
    "Rankings are investigation candidates, not confirmed causes or universal thresholds.",
    "A shared-buffer miss is not necessarily a physical disk read; correlate OS/storage metrics.",
    "pg_stat_io is cluster/backend/object/context data, not direct SQL or relation attribution.",
    "pg_stat_statements rows are returned/affected rows, not rows scanned. Calls alone do not prove N+1.",
    "Percentiles require individual execution observations; aggregate mean/stddev/max are insufficient.",
    "Counters may lag active work. Section timestamps are separate, and endpoint settings do not prove interval history.",
    "Per-object resets without a visible reset marker may be undetectable if counters grow past the old value.",
    "Collector SELECTs are filtered by a marker, but monitoring BEGIN/SET/COMMIT utility calls may remain in pg_stat_statements.",
    "n_dead_tup is an estimate, not a physical bloat ratio; zero index scans are not permission to drop an index.",
    "exec_time_share uses comparable top-level entries only; nested (track=all) and excluded entries are not in the denominator.",
    "Redundant-index and autovacuum-backlog lists are rule-based candidates from one snapshot; verify definitions and settings first.",
]
AUTOVACUUM_RELOPTIONS = ("autovacuum_enabled", "autovacuum_vacuum_threshold", "autovacuum_vacuum_scale_factor",
                         "autovacuum_vacuum_max_threshold", "autovacuum_vacuum_insert_threshold",
                         "autovacuum_vacuum_insert_scale_factor", "autovacuum_analyze_threshold",
                         "autovacuum_analyze_scale_factor")


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must contain a timezone")
    return result


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ratio(a, b):
    return a / b if numeric(a) and numeric(b) and b > 0 else None


def rows(snapshot, section):
    value = snapshot.get("sections", {}).get(section, {})
    return value.get("rows", []) if value.get("status") == "ok" else []


def settings(snapshot):
    return {row["name"]: row["setting"] for row in rows(snapshot, "settings")}


def validate_pair(before, after):
    for snapshot in (before, after):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("metadata"), dict):
            raise ValueError("snapshot must be an object with metadata")
        if not isinstance(snapshot.get("sections"), dict):
            raise ValueError("snapshot sections must be an object")
        for section in snapshot["sections"].values():
            if not isinstance(section, dict) or not isinstance(section.get("rows", []), list):
                raise ValueError("section must be an object containing a rows array")
            if any(not isinstance(row, dict) for row in section.get("rows", [])):
                raise ValueError("section rows must be objects")
    if before.get("schema_version") != 1 or after.get("schema_version") != 1:
        raise ValueError("unsupported snapshot schema_version")
    if not before.get("instance_id") or before["instance_id"] != after.get("instance_id"):
        raise ValueError("different or missing instance_id")
    for key in ("database_oid", "database", "server_version_num", "postmaster_start", "in_recovery", "block_size"):
        if before["metadata"].get(key) is None or before["metadata"][key] != after["metadata"].get(key):
            raise ValueError(f"incompatible snapshots: {key} changed or missing")
    if "server_identity_changed_during_collection" in before.get("warnings", []) + after.get("warnings", []):
        raise ValueError("server identity changed inside a snapshot")
    if timestamp(after["started_at"]) <= timestamp(before["finished_at"]):
        raise ValueError("snapshots are reversed, overlapping, or have no observation interval")


def created_after(value, boundary):
    try:
        return value is not None and timestamp(value) > timestamp(boundary)
    except (TypeError, ValueError):
        return False


def changed_marker(old, new, name):
    return old.get(name) != new.get(name) if name in old or name in new else False


def section_deltas(before, after, name, warnings):
    key_fields, counters = SPECS[name]
    a = before.get("sections", {}).get(name, {})
    b = after.get("sections", {}).get(name, {})
    if a.get("status") != "ok" or b.get("status") != "ok":
        warnings.append(f"{name}: unavailable at one or both endpoints")
        return []
    if a.get("truncated") or b.get("truncated"):
        warnings.append(f"{name}: truncated snapshot; rankings cover only matching captured rows")
    elapsed = (timestamp(b["collected_at"]) - timestamp(a["collected_at"])).total_seconds()
    if elapsed <= 0:
        warnings.append(f"{name}: no positive interval")
        return []
    if name in ("tables", "indexes", "table_io", "index_io", "database", "conflicts"):
        db_a, db_b = rows(before, "database"), rows(after, "database")
        if (not db_a or not db_b or "stats_reset" not in db_a[0] or "stats_reset" not in db_b[0]
                or changed_marker(db_a[0], db_b[0], "stats_reset")):
            warnings.append(f"{name}: database reset marker changed or unavailable; interval suppressed")
            return []
    if name == "pgss":
        ext = lambda snap: next((e.get("version") for e in snap["metadata"].get("extensions", [])
                                if e.get("name") == "pg_stat_statements"), None)
        if ext(before) != ext(after):
            warnings.append("pgss: extension version changed; interval suppressed")
            return []
        info_a, info_b = rows(before, "pgss_info"), rows(after, "pgss_info")
        if (not info_a or not info_b or "stats_reset" not in info_a[0] or "stats_reset" not in info_b[0]
                or changed_marker(info_a[0], info_b[0], "stats_reset")):
            warnings.append("pgss: reset marker changed or unavailable; interval suppressed")
            return []
        if info_a[0].get("dealloc") != info_b[0].get("dealloc"):
            warnings.append("pgss: entry eviction occurred; rows require unchanged stats_since")

    def key(row):
        return tuple(row.get(field) for field in key_fields)

    # A hidden queryid is NULL; never merge separate hidden SQL under the same key.
    old = {}
    duplicates = set()
    for row in a["rows"]:
        k = key(row)
        if k in old:
            duplicates.add(k)
        old[k] = row
    new_keys = set()
    for row in b["rows"]:
        k = key(row)
        if k in new_keys:
            duplicates.add(k)
        new_keys.add(k)
    output = []
    excluded = {}
    new_in_interval = 0
    setting_a, setting_b = settings(before), settings(after)
    for current in b["rows"]:
        k = key(current)
        reason = None
        if any(value is None for value in k) or k in duplicates:
            reason = "missing_or_duplicate_identity"
        previous = old.get(k)
        if previous is None and reason is None and name == "pgss" and created_after(current.get("stats_since"), a["collected_at"]):
            # Created inside the interval (PG17+ stats_since): every counted call is in the window, so start from zero.
            previous = dict({field: 0 for field in counters if field in current}, stats_since=current["stats_since"])
            new_in_interval += 1
        if previous is None:
            reason = reason or "no_baseline"
        if reason is None:
            marker_fields = ("stats_reset", "stats_since") + (IDENTITY if name in ("tables", "indexes", "table_io", "index_io") else ())
            if any(changed_marker(previous, current, marker) for marker in marker_fields):
                reason = "reset_or_object_identity_changed"
            if name == "pgss" and (previous.get("stats_since") is None or current.get("stats_since") is None):
                reason = "missing_entry_reset_marker"
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        delta = {field: current[field] - previous[field] for field in counters
                 if numeric(previous.get(field)) and numeric(current.get(field))}
        if any(value < 0 for value in delta.values()):
            excluded["counter_decreased"] = excluded.get("counter_decreased", 0) + 1
            continue
        for field in list(delta):
            timing_setting = None
            if field.endswith("_time") and ("blk" in field or name == "io"):
                timing_setting = "track_wal_io_timing" if name == "io" and current.get("object") == "wal" else "track_io_timing"
            if field == "total_plan_time" or (name == "pgss" and field == "plans"):
                timing_setting = "pg_stat_statements.track_planning"
            if timing_setting and (setting_a.get(timing_setting) != "on" or setting_b.get(timing_setting) != "on"):
                del delta[field]
        result = {"identity": dict(zip(key_fields, k)), "elapsed_seconds": elapsed,
                  "delta": delta, "per_second": {field: value / elapsed for field, value in delta.items()}}
        for label in ("schemaname", "relname", "indexrelname"):
            if label in current:
                result["identity"][label] = current[label]
        if name == "pgss":
            calls = delta.get("calls")
            result["mean_exec_ms"] = ratio(delta.get("total_exec_time"), calls)
            result["rows_per_call"] = ratio(delta.get("rows"), calls)
            result["temp_written_bytes"] = (delta["temp_blks_written"] * after["metadata"]["block_size"]
                                             if "temp_blks_written" in delta else None)
            result["shared_read_bytes"] = (delta["shared_blks_read"] * after["metadata"]["block_size"]
                                           if "shared_blks_read" in delta else None)
            result["shared_hit_ratio"] = (ratio(delta["shared_blks_hit"], delta["shared_blks_hit"] + delta["shared_blks_read"])
                                           if {"shared_blks_hit", "shared_blks_read"} <= delta.keys() else None)
            # Logical buffer accesses track CPU/memory-bandwidth cost even when every access is a hit.
            result["shared_blks_accessed"] = (delta["shared_blks_hit"] + delta["shared_blks_read"]
                                              if {"shared_blks_hit", "shared_blks_read"} <= delta.keys() else None)
            # Share of execution spent waiting on reads; low means a low hit ratio is not what users wait for.
            read_time = sum(delta[f] for f in ("shared_blk_read_time", "local_blk_read_time", "temp_blk_read_time")
                            if f in delta)
            result["read_time_share"] = (ratio(read_time, delta.get("total_exec_time"))
                                         if "shared_blk_read_time" in delta else None)
            result["toplevel"] = current.get("toplevel")
        if name == "table_io":
            result["heap_hit_ratio"] = (ratio(delta["heap_blks_hit"], delta["heap_blks_hit"] + delta["heap_blks_read"])
                                        if {"heap_blks_hit", "heap_blks_read"} <= delta.keys() else None)
        if name == "tables":
            result["hot_update_ratio"] = ratio(delta.get("n_tup_hot_upd"), delta.get("n_tup_upd"))
        if name == "database":
            # active_time is milliseconds spent executing; per wall-clock second it is average active sessions.
            result["average_active_sessions"] = (delta["active_time"] / 1000 / elapsed
                                                 if "active_time" in delta else None)
            result["idle_in_transaction_share"] = ratio(delta.get("idle_in_transaction_time"), delta.get("session_time"))
            result["rollback_ratio"] = (ratio(delta["xact_rollback"], delta["xact_commit"] + delta["xact_rollback"])
                                        if {"xact_commit", "xact_rollback"} <= delta.keys() else None)
        output.append(result)
    if new_in_interval:
        warnings.append(f"{name}: {new_in_interval} entry(ies) created during the interval counted from zero")
    for reason, count in sorted(excluded.items()):
        warnings.append(f"{name}: excluded {count} row(s): {reason}")
    return output


def lock_chains(snapshot):
    """Root blockers with every session waiting behind them, built from pg_blocking_pids()."""
    sessions = {}
    for row in rows(snapshot, "locks") + rows(snapshot, "activity"):
        if row.get("pid") is not None:
            sessions.setdefault(row["pid"], {}).update({k: v for k, v in row.items() if v is not None})
    blocked_by = {pid: list(row["blocking_pids"]) for pid, row in sessions.items() if row.get("blocking_pids")}
    waiters_of = {}
    for pid, blockers in blocked_by.items():
        for blocker in blockers:
            waiters_of.setdefault(blocker, set()).add(pid)
    chains, reached = [], set()
    for root in sorted(pid for pid in waiters_of if not blocked_by.get(pid)):
        seen, frontier, depth = {root}, [root], 0
        while frontier:
            frontier = [w for p in frontier for w in waiters_of.get(p, ()) if w not in seen]
            seen.update(frontier)
            depth += bool(frontier)
        reached |= seen
        row = sessions.get(root, {})
        chains.append({"root_pid": root, "prepared_transaction": root == 0, "state": row.get("state"),
                       "xact_start": row.get("xact_start"), "backend_start": row.get("backend_start"),
                       "wait_event_type": row.get("wait_event_type"), "wait_event": row.get("wait_event"),
                       "query_id": row.get("query_id"), "blocked_sessions": len(seen) - 1, "max_depth": depth,
                       "direct_waiters": sorted(waiters_of[root])})
    chains.sort(key=lambda x: x["blocked_sessions"], reverse=True)
    cycle = sorted(set(blocked_by) - reached)
    if cycle:
        # Waiters with no root are in a cycle: a deadlock not yet resolved by deadlock_timeout.
        chains.append({"root_pid": None, "cycle_pids": cycle, "blocked_sessions": len(cycle)})
    return chains


def xmin_horizon_holders(snapshot):
    """Who keeps VACUUM from removing dead rows, oldest first. Sessions are limited to this DB."""
    holders = []
    for row in rows(snapshot, "activity"):
        ages = [row[k] for k in ("backend_xid_age", "backend_xmin_age") if numeric(row.get(k))]
        if ages:
            holders.append({"source": "session", "scope": "this database", "age": max(ages), "pid": row.get("pid"),
                            "backend_type": row.get("backend_type"), "state": row.get("state"),
                            "xact_start": row.get("xact_start")})
    for row in rows(snapshot, "prepared"):
        if numeric(row.get("transaction_age")):
            holders.append({"source": "prepared_transaction", "scope": "this database", "age": row["transaction_age"],
                            "gid": row.get("gid"), "prepared": row.get("prepared")})
    for row in rows(snapshot, "slots"):
        for field, source, scope in (("xmin_age", "replication_slot_xmin", "cluster"),
                                     ("catalog_xmin_age", "replication_slot_catalog_xmin", "system catalogs")):
            if numeric(row.get(field)):
                holders.append({"source": source, "scope": scope, "age": row[field],
                                "slot_name": row.get("slot_name"), "active": row.get("active")})
    for row in rows(snapshot, "replication"):
        if numeric(row.get("backend_xmin_age")):
            holders.append({"source": "standby_feedback", "scope": "cluster", "age": row["backend_xmin_age"],
                            "pid": row.get("pid"), "application_name": row.get("application_name")})
    return sorted(holders, key=lambda x: x["age"], reverse=True)


def redundant_indexes(indexes):
    """B-tree indexes whose keys equal or prefix another index on the same table."""
    usable = []
    for row in indexes:
        count = row.get("indnkeyatts")
        if (row.get("access_method") != "btree" or not isinstance(count, int) or count <= 0
                or row.get("has_expressions") or row.get("is_partial") or row.get("indisvalid") is False
                or row.get("relid") is None):
            continue
        # indclass/indcollation/indoption cover key columns only; indkey also lists INCLUDE columns.
        vectors = [str(row.get(field) or "").split() for field in ("indkey", "indclass", "indcollation", "indoption")]
        if any(len(vector) < count for vector in vectors):
            continue
        usable.append((row, list(zip(*(vector[:count] for vector in vectors)))))
    found = []
    for row, keys in usable:
        # Unique/constraint indexes enforce rules, and INCLUDE columns may serve index-only scans.
        if (row.get("indisunique") or row.get("indisprimary") or row.get("indisexclusion")
                or row.get("constraint_names") or row.get("indnatts", row["indnkeyatts"]) != row["indnkeyatts"]):
            continue
        for other, other_keys in usable:
            if other is row or other.get("relid") != row.get("relid") or other_keys[:len(keys)] != keys:
                continue
            duplicate = len(other_keys) == len(keys)
            if duplicate and not other.get("indisunique") and (other.get("indexrelid") or 0) > (row.get("indexrelid") or 0):
                continue  # Report only one member of an identical non-unique pair.
            found.append({"schemaname": row.get("schemaname"), "relname": row.get("relname"),
                          "index": row.get("indexrelname"), "covered_by": other.get("indexrelname"),
                          "reason": "duplicate" if duplicate else "prefix", "index_bytes": row.get("index_bytes"),
                          "idx_scan": row.get("idx_scan")})
            break
    return sorted(found, key=lambda x: x["index_bytes"] if numeric(x["index_bytes"]) else 0, reverse=True)


def autovacuum_backlog(tables, values):
    """Tables already past their autovacuum/autoanalyze trigger, using table reloptions over globals."""
    def setting(options, name):
        try:
            return float(options[name] if name in options else values[name])
        except (KeyError, TypeError, ValueError):
            return None

    found = []
    for row in tables:
        options = {}
        for item in row.get("reloptions") or []:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                if key in AUTOVACUUM_RELOPTIONS:
                    options[key] = value
        if row.get("relkind") == "p":
            # Autovacuum never ANALYZEs partitioned parents; whole-table estimates need a manual ANALYZE.
            if not row.get("last_analyze") and not row.get("last_autoanalyze"):
                found.append({"schemaname": row.get("schemaname"), "relname": row.get("relname"),
                              "reason": "partitioned_parent_never_analyzed", "ratio": None})
            continue
        if row.get("relkind") not in ("r", "m"):
            continue
        reltuples = max(row["reltuples"], 0) if numeric(row.get("reltuples")) else 0
        enabled = (values.get("autovacuum") == "on"
                   and options.get("autovacuum_enabled", "on").lower() not in ("off", "false", "no", "0"))
        entry = {"schemaname": row.get("schemaname"), "relname": row.get("relname"), "autovacuum_enabled": enabled,
                 "last_autovacuum": row.get("last_autovacuum"), "last_autoanalyze": row.get("last_autoanalyze")}
        base, scale = setting(options, "autovacuum_vacuum_threshold"), setting(options, "autovacuum_vacuum_scale_factor")
        if base is not None and scale is not None and numeric(row.get("n_dead_tup")):
            threshold = base + scale * reltuples
            cap = setting(options, "autovacuum_vacuum_max_threshold")
            if cap is not None and cap >= 0:
                threshold = min(threshold, cap)
            entry.update(n_dead_tup=row["n_dead_tup"], dead_threshold=threshold,
                         dead_ratio=ratio(row["n_dead_tup"], threshold))
        base = setting(options, "autovacuum_vacuum_insert_threshold")
        scale = setting(options, "autovacuum_vacuum_insert_scale_factor")
        if base is not None and base >= 0 and scale is not None and numeric(row.get("n_ins_since_vacuum")):
            # Approximate: PostgreSQL 18+ scales this term by the unfrozen fraction of the table.
            threshold = base + scale * reltuples
            entry.update(n_ins_since_vacuum=row["n_ins_since_vacuum"], insert_threshold=threshold,
                         insert_ratio=ratio(row["n_ins_since_vacuum"], threshold))
        base, scale = setting(options, "autovacuum_analyze_threshold"), setting(options, "autovacuum_analyze_scale_factor")
        if base is not None and scale is not None and numeric(row.get("n_mod_since_analyze")):
            threshold = base + scale * reltuples
            entry.update(n_mod_since_analyze=row["n_mod_since_analyze"], analyze_threshold=threshold,
                         analyze_ratio=ratio(row["n_mod_since_analyze"], threshold))
        ratios = [entry.get(k) for k in ("dead_ratio", "insert_ratio", "analyze_ratio") if numeric(entry.get(k))]
        if ratios and max(ratios) > 1:
            entry.update(reason="past_trigger_threshold", ratio=max(ratios))
            found.append(entry)
    return sorted(found, key=lambda x: (x["ratio"] is not None, x["ratio"] or 0), reverse=True)


def analyze(before, after, top=10):
    validate_pair(before, after)
    warnings = list(dict.fromkeys(before.get("warnings", []) + after.get("warnings", [])))
    sa, sb = settings(before), settings(after)
    for setting in ("track_counts", "track_activities", "track_io_timing", "track_wal_io_timing",
                    "pg_stat_statements.track_planning", "pg_stat_statements.track"):
        if sa.get(setting) != sb.get(setting):
            warnings.append(f"setting changed: {setting}; interval comparability requires investigation")
        if setting != "pg_stat_statements.track" and (sa.get(setting) != "on" or sb.get(setting) != "on"):
            warnings.append(f"{setting}: not enabled at both endpoints; related metrics incomplete/unmeasured")
    deltas = {name: section_deltas(before, after, name, warnings) for name in SPECS}
    if sa.get("pg_stat_statements.track") not in ("top", "all") or sb.get("pg_stat_statements.track") not in ("top", "all"):
        warnings.append("pg_stat_statements.track: disabled or unknown; SQL interval suppressed")
        deltas["pgss"] = []
    if sa.get("track_counts") != "on" or sb.get("track_counts") != "on":
        for name in ("database", "tables", "indexes", "table_io", "index_io", "conflicts"):
            deltas[name] = []
    def rank(items, metric):
        selected = sorted((x for x in items if numeric(metric(x)) and metric(x) > 0), key=metric, reverse=True)[:top]
        return [dict(x, ranking_value=metric(x)) for x in selected]
    sql = deltas["pgss"]
    # With track=all, nested statements are also inside their caller's time: share only top-level work.
    toplevel_total = sum(x["delta"].get("total_exec_time", 0) for x in sql if x.get("toplevel") is not False)
    for entry in sql:
        entry["exec_time_share"] = (ratio(entry["delta"].get("total_exec_time"), toplevel_total)
                                    if entry.get("toplevel") is not False else None)
    rankings = {
        "sql_total_exec_ms": rank(sql, lambda x: x["delta"].get("total_exec_time")),
        "sql_total_plan_ms": rank(sql, lambda x: x["delta"].get("total_plan_time")),
        "sql_shared_blks_accessed": rank(sql, lambda x: x.get("shared_blks_accessed")),
        "sql_wal_bytes": rank(sql, lambda x: x["delta"].get("wal_bytes")),
        "sql_mean_exec_ms": rank(sql, lambda x: x.get("mean_exec_ms")),
        "sql_calls": rank(sql, lambda x: x["delta"].get("calls")),
        "sql_temp_written_bytes": rank(sql, lambda x: x.get("temp_written_bytes")),
        "sql_shared_read_bytes": rank(sql, lambda x: x.get("shared_read_bytes")),
        "sql_rows_per_call": rank(sql, lambda x: x.get("rows_per_call")),
        "table_heap_reads": rank(deltas["table_io"], lambda x: x["delta"].get("heap_blks_read")),
        "table_seq_tuples": rank(deltas["tables"], lambda x: x["delta"].get("seq_tup_read")),
    }
    current = {}
    # Preserve evidence, not interpretations, for gauges and instantaneous relationships.
    gauge_keys = {
        "tables": ("relid", "schemaname", "relname", "n_live_tup", "n_dead_tup", "n_mod_since_analyze",
                   "last_autovacuum", "last_autoanalyze", "xid_age", "mxid_age", "total_bytes", "reloptions"),
        "activity": ("pid", "backend_start", "datname", "state", "xact_start", "query_start", "state_change",
                     "wait_event_type", "wait_event", "query_id", "backend_xmin", "backend_xmin_age",
                     "leader_pid", "backend_type"),
        "locks": ("pid", "locktype", "mode", "granted", "waitstart", "blocking_pids", "relation_schema",
                  "relation_name", "transactionid", "xact_start", "state"),
        "slots": ("slot_name", "slot_type", "database", "active", "restart_lsn", "confirmed_flush_lsn",
                  "xmin", "catalog_xmin", "xmin_age", "catalog_xmin_age", "wal_status", "safe_wal_size",
                  "retained_wal_bytes", "in_recovery"),
        "replication": ("pid", "state", "sync_state", "sent_lsn", "write_lsn", "flush_lsn", "replay_lsn",
                        "write_lag", "flush_lag", "replay_lag", "backend_xmin_age"),
        "prepared": ("transaction", "transaction_age", "gid", "prepared", "database"),
    }
    for name, fields in gauge_keys.items():
        data = rows(after, name)
        if name == "tables":
            data = sorted(data, key=lambda x: x.get("n_dead_tup") or 0, reverse=True)
        if name == "activity":
            # Oldest transactions first: they matter most for locks and VACUUM, and pid order would hide them.
            data = sorted(data, key=lambda x: (x.get("xact_start") is None, x.get("xact_start") or ""))
        if name == "locks":
            data = [x for x in data if x.get("blocking_pids") or x.get("granted") is False]
        if len(data) > top:
            warnings.append(f"current {name}: showing {top} of {len(data)} rows; inspect original snapshot")
        current[name] = [{key: row[key] for key in fields if key in row} for row in data[:top]]
        section = after.get("sections", {}).get(name, {})
        if section.get("status") != "ok" or section.get("truncated"):
            warnings.append(f"current {name}: missing or truncated evidence")
    current["lock_chains"] = lock_chains(after)[:top]
    current["xmin_horizon_holders"] = xmin_horizon_holders(after)[:top]
    current["redundant_indexes"] = redundant_indexes(rows(after, "indexes"))[:top]
    current["autovacuum_backlog"] = autovacuum_backlog(rows(after, "tables"), settings(after))[:top]
    return {"schema_version": 1, "instance_id": after["instance_id"], "database": after["metadata"]["database"],
            "window": {"from": before["started_at"], "to": after["finished_at"],
                       "note": "Overall collection bounds; use each section's elapsed_seconds for interval rates."},
            "rankings": rankings, "deltas": deltas, "current": current,
            "settings": {"before": sa, "after": sb},
            "warnings": list(dict.fromkeys(warnings)), "limitations": LIMITATIONS,
            "next_steps": ["Inspect the relevant skill reference, SQL plan and representative parameter distribution.",
                           "Correlate application traces, OS metrics and incident/change timestamps before recommending changes."]}


def render_markdown(report):
    lines = ["# PostgreSQL interval evidence", "", f"Instance: {report['instance_id']}; database: {report['database']}",
             f"Collection bounds: {report['window']['from']} → {report['window']['to']}",
             report['window']['note'], "",
             "Rankings identify candidates. An empty list is not a clean bill of health.", "", "## Data quality", ""]
    lines += ["- " + value for value in report["warnings"]] or ["- No detected discontinuity; see limitations."]
    for group, entries in report["rankings"].items():
        lines += ["", "## " + group, "", "| Identity | Ranking value | share | calls | total ms | mean ms | rows/call | temp bytes | read bytes |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for entry in entries:
            def cell(value):
                if value is None:
                    return "—"
                if isinstance(value, float):
                    return f"{value:.3f}"
                return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
            values = [json.dumps(entry["identity"], ensure_ascii=False), entry["ranking_value"], entry.get("exec_time_share"),
                      entry["delta"].get("calls"),
                      entry["delta"].get("total_exec_time"), entry.get("mean_exec_ms"), entry.get("rows_per_call"),
                      entry.get("temp_written_bytes"), entry.get("shared_read_bytes")]
            lines.append("| " + " | ".join(cell(value) for value in values) + " |")
        if not entries:
            lines += ["", "No comparable positive samples."]
    lines += ["", "## System deltas and current evidence", "", "```json", json.dumps({
        "deltas": {k: v for k, v in report["deltas"].items() if k not in ("pgss", "tables", "table_io", "indexes", "index_io")},
        "current": report["current"]}, ensure_ascii=False, indent=2), "```", "", "## Interpretation limits", ""]
    lines += ["- " + value for value in report["limitations"]]
    lines += ["", "## Next steps", ""] + ["- " + value for value in report["next_steps"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    if args.top <= 0:
        parser.error("--top must be positive")
    try:
        before = json.loads(args.before.read_text(encoding="utf-8"))
        after = json.loads(args.after.read_text(encoding="utf-8"))
        report = analyze(before, after, args.top)
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json" else render_markdown(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
