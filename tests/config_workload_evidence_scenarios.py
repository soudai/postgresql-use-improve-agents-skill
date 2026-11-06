#!/usr/bin/env python3
"""Check configuration counterexamples and raw workload reporting in an isolated DB.

These short runs validate diagnostic behavior, not production performance gains.
Uses only a locally available PostgreSQL 18/19 image and the disposable Cluster harness.
"""
import argparse
import json
from pathlib import Path
import re
import tempfile

from followups import replay
from scenarios import Cluster, CONFIG


def checkpoints(cluster):
    before = int(cluster.value("SELECT num_requested FROM pg_stat_checkpointer"))
    cluster.psql("CHECKPOINT; CHECKPOINT; CHECKPOINT;")
    after = int(cluster.value("SELECT num_requested FROM pg_stat_checkpointer"))
    assert after - before >= 3, (before, after)
    print(json.dumps({"manual_checkpoint_requested_delta": after - before}), flush=True)
    # Replay these observed counts in a long-window fixture to test cause inference.
    # The 30-minute span is synthetic; the actual checkpoints above were immediate.
    evidence = {"checkpoints_requested": after - before, "checkpoints_timed": 0,
                "interval_seconds": 1800, "wal_bytes_per_s": 1}
    settings = {"max_wal_size": {"setting": "1024", "unit": "MB"},
                "checkpoint_timeout": {"setting": "300", "unit": "s"}}
    review = CONFIG.Review(settings, argparse.Namespace(ram_gb=None, memory_limit_gb=None), evidence)
    review.wal()
    candidate = next(f for f in review.findings if f["parameter"] == "max_wal_size")
    assert candidate["recommended"] is None, candidate
    assert candidate["evidence"]["cause"] == "unknown", candidate


def jit(cluster):
    cluster.psql("CREATE EXTENSION IF NOT EXISTS pg_stat_statements;")
    report = cluster.interval(lambda: cluster.psql("""
SET jit=on;
SET jit_above_cost=0;
SET jit_inline_above_cost=0;
SET jit_optimize_above_cost=0;
SELECT /* configuration_jit_evidence */ sum(i::bigint*i) FROM generate_series(1,10000) i;
"""))
    rows = [r["delta"] for r in report["deltas"]["pgss"] if r.get("toplevel", True)]
    total = sum(r.get("total_exec_time", 0) for r in rows)
    compile_time = sum(sum(r.get(k, 0) for k in (
        "jit_generation_time", "jit_inlining_time", "jit_optimization_time", "jit_emission_time")) for r in rows)
    assert compile_time > 0 and total > 0, rows
    evidence = CONFIG.evidence_from_report(report)
    assert abs(evidence["jit_share"] - compile_time / total) < 1e-12, evidence
    review = CONFIG.Review({"jit": {"setting": "on"}},
                          argparse.Namespace(ram_gb=None, workload="mixed"), evidence)
    review.jit()
    assert all(f["recommended"] is None for f in review.findings), review.findings
    print("JIT generation/inlining/optimization/emission accounting passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"postgres:(?:18|19)[A-Za-z0-9_.-]*", args.image):
        parser.error("use a local postgres:18 or postgres:19 image")
    with tempfile.TemporaryDirectory(prefix="pgskills-config-workload-") as scratch:
        directory = Path(scratch)
        cluster = Cluster(args.image, directory)
        try:
            cluster.start()
            print(cluster.value("SELECT version()"), flush=True)
            checkpoints(cluster)
            jit(cluster)
            replay(cluster, directory)
        finally:
            cluster.stop()


if __name__ == "__main__":
    main()
