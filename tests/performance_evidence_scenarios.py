#!/usr/bin/env python3
"""Disposable PostgreSQL acceptance tests for visibility, bitmap and plan provenance, and insert VACUUM evidence."""
import argparse
import json
from pathlib import Path
import tempfile

from scenarios import ANALYZE, PLAN, ROOT, Cluster, blocks, wait_until


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def exercise(image, directory):
    directory.mkdir(parents=True, exist_ok=True)
    cluster = Cluster(image, directory, extra_args=("-c", "autovacuum_naptime=1h"))
    result = {"image": image}
    try:
        cluster.start()
        result["version"] = cluster.value("SHOW server_version")
        cluster.psql("CREATE EXTENSION pg_stat_statements; CREATE ROLE evidence_observer LOGIN;")
        cluster.shell("cat > /tmp/evidence-observer-psql <<'SH'\n"
                      "#!/bin/sh\nexec psql -U evidence_observer \"$@\"\nSH\n"
                      "chmod 755 /tmp/evidence-observer-psql\n")
        sleeper = cluster.session("evidence-active")
        try:
            sleeper.send("SELECT pg_sleep(15);")
            check(wait_until(lambda: cluster.value(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'evidence-active' "
                "AND state = 'active'") == "1"), "active session not observed")
            hidden = cluster.sample(samples=2, interval=.1, psql="/tmp/evidence-observer-psql")
            visible = cluster.sample(samples=2, interval=.1)
            check(hidden["average_active_sessions"] is None and hidden["unknown_session_samples"] > 0,
                  "restricted sampling must be unknown")
            check(visible["average_active_sessions"] >= 1, "privileged sampling must observe active workload")
            result["activity"] = {"restricted": hidden, "privileged": visible}
            cluster.psql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                         "WHERE application_name = 'evidence-active';")
            sleeper.send("SET track_activities=off; SELECT pg_sleep(15);")
            check(wait_until(lambda: cluster.value(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'evidence-active' "
                "AND state = 'disabled'") == "1"), "disabled activity state not observed")
            disabled = cluster.sample(samples=2, interval=.1)
            check(disabled["average_active_sessions"] is None and disabled["unknown_session_samples"] > 0,
                  "per-backend disabled tracking must be unknown")
            result["activity"]["tracking_disabled"] = disabled
        finally:
            cluster.psql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE application_name = 'evidence-active';", check=False)
            sleeper.close()

        cluster.psql("CREATE TABLE evidence_brin AS SELECT i FROM generate_series(1,100000) i; "
                     "CREATE INDEX evidence_brin_idx ON evidence_brin USING brin(i); ANALYZE evidence_brin;")
        raw = json.loads(cluster.psql("SET work_mem='1GB'; SET enable_seqscan=off; "
                         "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) "
                         "SELECT * FROM evidence_brin WHERE i=50000;"))
        relation = json.loads(cluster.psql(
            (ROOT / "skills/postgres-performance-diagnostics/sql/inspect_relation.sql").read_text(),
            extra=("--set=relation=public.evidence_brin",)))
        plan = PLAN.analyze(raw, relation_evidence=relation)["plans"][0]
        lossy = next(c for c in plan["candidates"] if c["kind"] == "lossy_bitmap")
        check(lossy["evidence"]["index_access_methods"] == ["brin"], "BRIN metadata did not match")
        check(lossy["evidence"]["cause_status"] == "unconfirmed" and "exceeded work_mem" not in lossy["next_step"],
              "normal BRIN lossiness must not establish memory exhaustion")
        result["brin"] = lossy

        literal = json.loads(cluster.psql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                                         "SELECT * FROM evidence_brin WHERE (i::text || '$1') > 'z';"))
        literal_report = PLAN.analyze(literal)["plans"][0]
        check(not literal_report["uses_parameter_placeholders"], "literal $1 misclassified as a token")
        check(any(c["kind"] == "cardinality_mismatch" for c in literal_report["candidates"]),
              "the literal control must still expose observed row-estimate mismatch")
        check(not any(c["kind"] == "generic_plan_misestimate" for c in literal_report["candidates"]),
              "literal $1 misclassified as a generic plan")
        initplan = json.loads(cluster.psql("EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM evidence_brin "
                                          "WHERE i = (SELECT max(i) FROM evidence_brin);"))
        check(not PLAN.analyze(initplan)["plans"][0]["uses_parameter_placeholders"],
              "InitPlan executor output misclassified as prepared parameter")
        captured = blocks(cluster.psql(
            "PREPARE evidence(text) AS SELECT * FROM evidence_brin WHERE (i::text || $1) > 'z';\n"
            "SET plan_cache_mode=force_generic_plan;\n\\echo @@generic\n"
            "EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE evidence('$1');\n"
            "SET plan_cache_mode=force_custom_plan;\n\\echo @@custom\n"
            "EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE evidence('$1');"))
        for mode in ("generic", "custom"):
            parsed = PLAN.analyze(json.loads(captured[mode]), plan_mode=mode,
                                  plan_source="acceptance: EXPLAIN EXECUTE under force_" + mode + "_plan")["plans"][0]
            check(any(c["kind"] == "generic_plan_misestimate" for c in parsed["candidates"]) == (mode == "generic"),
                  mode + " capture provenance did not determine classification")
        unproven = PLAN.analyze(json.loads(captured["generic"]))["plans"][0]
        check(not any(c["kind"] == "generic_plan_misestimate" for c in unproven["candidates"]),
              "parameter tokens alone must leave plan mode unknown")
        result["plan_modes"] = {"literal": "not_generic", "initplan": "not_prepared_parameter",
                                "generic_with_source": "candidate", "custom_with_source": "not_generic",
                                "generic_without_source": "unknown"}

        cluster.psql("CREATE TABLE evidence_frozen WITH (autovacuum_enabled=false) AS "
                     "SELECT i FROM generate_series(1,100000) i;")
        cluster.psql("VACUUM (FREEZE, ANALYZE) evidence_frozen;")
        cluster.psql("INSERT INTO evidence_frozen SELECT i FROM generate_series(100001,102000) i; "
                     "ANALYZE evidence_frozen; SELECT pg_stat_force_next_flush();")
        cluster.psql("ALTER TABLE evidence_frozen RESET (autovacuum_enabled);")
        snapshot = cluster.collect()
        row = next(r for r in snapshot["sections"]["tables"]["rows"] if r["relname"] == "evidence_frozen")
        values = {r["name"]: r["setting"] for r in snapshot["sections"]["settings"]["rows"]}
        found = ANALYZE.autovacuum_backlog([row], values)
        check(row["relallfrozen"] > 0 and len(found) == 1, "freeze-adjusted insert trigger not detected")
        check("insert" in found[0]["exceeded_triggers"], "insert trigger did not exceed threshold")
        check(found[0]["insert_threshold"] < row["n_ins_since_vacuum"] < 1000 + .2 * row["reltuples"],
              "reproduction did not distinguish old and freeze-adjusted thresholds")
        check(found[0]["autovacuum_enabled"] is True, "reproduction must leave table vacuum enabled")
        missing = dict(row)
        del missing["relallfrozen"]
        unknown = ANALYZE.autovacuum_backlog([missing], values, include_unknown=True)
        check(unknown[0]["reason"] == "insufficient_trigger_evidence", "older snapshots must remain unassessed")
        result["insert_vacuum"] = found[0]
        result["status"] = "pass"
        (directory / "acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        cluster.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", nargs="+", default=["postgres:18-bookworm", "postgres:19rc1-bookworm"])
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pgskills-performance-evidence-") as temporary:
        directory = args.output_dir or Path(temporary)
        for index, image in enumerate(args.images):
            result = exercise(image, directory / str(index))
            print(json.dumps({"image": image, "version": result["version"], "status": result["status"],
                              "insert_threshold": result["insert_vacuum"]["insert_threshold"]}), flush=True)


if __name__ == "__main__":
    main()
