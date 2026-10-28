#!/usr/bin/env python3
"""Exercise the CLI against a NEW disposable, network-isolated PostgreSQL container.

Requires Docker and a locally available official PostgreSQL 18/19 image. No ports,
host data directories, existing databases, roles, grants, or replication slots are
used. The fixture makes synthetic tables and loads pg_stat_statements. Results are
temporary. The exact container created by this process is checked before cleanup.

Example: python tests/integration.py --image postgres:18-bookworm
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "postgres-performance-diagnostics" / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "workload.sql"
RELATION_SQL = SCRIPTS.parent / "sql" / "inspect_relation.sql"
CONFIG_SQL = ROOT / "skills" / "postgres-config-tuning" / "sql" / "settings.sql"
CONFIG_SCRIPT = ROOT / "skills" / "postgres-config-tuning" / "scripts" / "review_config.py"
TASK_LABEL = "postgres-performance-diagnostics"
OPTIONAL_SECTIONS = {"lock_stats", "autovacuum_scores", "recovery"}


def run(command, *, sql=None, timeout=180, check=True):
    result = subprocess.run(command, input=sql, text=True, encoding="utf-8",
                            capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): "
                           f"{result.stderr.strip() or result.stdout.strip()}")
    return result


def psql(container, sql, *, check=True):
    return run(["docker", "exec", "-i", container, "psql", "-X", "-qAt",
                "--no-password", "--set=ON_ERROR_STOP=1"], sql=sql, check=check)


def collect(container, output, *, row_limit=10000):
    run([sys.executable, str(SCRIPTS / "collect.py"), "--docker-container", container,
         "--instance-id", container, "--row-limit", str(row_limit), "--output", str(output)])
    return json.loads(output.read_text(encoding="utf-8"))


def compare(before, after, output, *, check=True):
    return run([sys.executable, str(SCRIPTS / "analyze.py"), str(before), str(after),
                "--format", "json", "--output", str(output)], check=check)


def check_snapshot(snapshot):
    major = snapshot["metadata"]["server_version_num"] // 10000
    assert major in (18, 19), f"Unexpected server major: {major}"
    for name, section in snapshot["sections"].items():
        if section["status"] == "unavailable":
            assert name in OPTIONAL_SECTIONS, f"Unexpected unavailable section: {name}"
            assert section["reason"] == "missing_relation"
            continue
        assert section["status"] == "ok", (name, section)
        assert not section["truncated"], f"Small fixture unexpectedly truncated: {name}"
        for row in section["rows"]:
            assert "query" not in row, f"Raw SQL leaked in {name}"
            assert "indexdef" not in row, f"Index SQL leaked in {name}"
            assert "conninfo" not in row, f"Connection string leaked in {name}"
    extensions = snapshot["metadata"]["extensions"]
    assert any(e["name"] == "pg_stat_statements" and e["schema"] == "diagnostics"
               for e in extensions), "Non-public extension schema was not detected"
    tables = snapshot["sections"]["tables"]["rows"]
    assert any(row["relname"] == "events" and row["relkind"] == "p" for row in tables)
    assert any(row["relname"] == "events_2026" and row["relispartition"] for row in tables)
    assert snapshot["sections"]["pgss"]["rows"], "Statement statistics were not collected"


def check_created_container(container_id, expected_name):
    """Only return evidence for the exact container ID this invocation created."""
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise RuntimeError("Docker did not return a full container ID; refusing cleanup")
    result = run(["docker", "inspect", container_id], timeout=15)
    details = json.loads(result.stdout)[0]
    if (details.get("Id") != container_id
            or details.get("Name") != "/" + expected_name
            or details.get("Config", {}).get("Labels", {}).get("codex.task") != TASK_LABEL):
        raise RuntimeError("Container ownership evidence changed; refusing cleanup")
    return details


def check_relation_inspection(container):
    # -f - executes the actual psql script via stdin and binds the relation as a quoted literal.
    def inspect(relation, *, check=True):
        return run(["docker", "exec", "-i", container, "psql", "-X", "-qAt",
                    "--no-password", "--set=relation=" + relation, "--file=-"],
                   sql=RELATION_SQL.read_text(encoding="utf-8"), check=check)

    result = json.loads(inspect("pgskills_fixture.accounts").stdout)
    assert result["relation"]["name"] == "accounts"
    assert result["visibility"]["relation_select_permitted"]
    column = next(c for c in result["columns"] if c["attname"] == "tenant_id")
    assert column["stats_status"] == "visible"
    assert column["statistics"] and column["statistics"][0]["n_distinct"] > 0
    assert "most_common_freqs" in column["statistics"][0]
    assert result["indexes"] and result["constraints"]
    extended = next(s for s in result["extended_statistics"]
                    if s["statistics_name"] == "account_correlation")
    assert extended["stats_status"] == "visible" and extended["statistics"]
    assert extended["statistics"][0]["dependencies"]
    forbidden = {"most_common_vals", "histogram_bounds", "indexdef", "exprs", "conbin"}
    for collection in (result["columns"], result["indexes"], result["constraints"],
                       result["extended_statistics"]):
        for row in collection:
            assert not forbidden.intersection(row)
            for stats in row.get("statistics", []):
                assert not forbidden.intersection(stats)

    assert inspect("pgskills_fixture.missing_relation", check=False).returncode != 0
    # A successful injected SELECT would emit this marker. Instead, regclass rejects the entire value.
    attempt = inspect("pgskills_fixture.accounts'; COMMIT; SELECT 'injected_marker'; --", check=False)
    assert attempt.returncode != 0, "Invalid regclass input unexpectedly succeeded"
    assert "injected_marker" not in attempt.stdout, "Relation input was executed as SQL"
    assert psql(container, "SELECT count(*) FROM pgskills_fixture.accounts;").stdout.strip() == "20000"


def exercise(container, directory):
    psql(container, FIXTURE.read_text(encoding="utf-8"))
    check_relation_inspection(container)
    before_path = directory / "before.json"
    after_path = directory / "after.json"
    before = collect(container, before_path)
    check_snapshot(before)
    # Repeat the same normalized statements so interval counters have a baseline.
    psql(container, FIXTURE.read_text(encoding="utf-8"))
    after = collect(container, after_path)
    check_snapshot(after)

    report_path = directory / "report.json"
    compare(before_path, after_path, report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["rankings"]["sql_calls"], "No comparable SQL calls were detected"
    assert report["rankings"]["sql_temp_written_bytes"], "The small-work_mem fixture did not spill"
    assert any(row["delta"].get("seq_scan", 0) > 0
               for row in report["deltas"]["tables"]), "No table activity was compared"

    # Reset semantics can be tested without resetting any database statistics.
    reset = copy.deepcopy(after)
    reset["sections"]["pgss_info"]["rows"][0]["stats_reset"] = "2099-01-01T00:00:00+00:00"
    reset_path = directory / "reset.json"
    reset_path.write_text(json.dumps(reset), encoding="utf-8")
    reset_report_path = directory / "reset-report.json"
    compare(before_path, reset_path, reset_report_path)
    reset_report = json.loads(reset_report_path.read_text(encoding="utf-8"))
    assert not reset_report["deltas"]["pgss"], "A reset interval was compared"
    assert any("pgss: reset marker" in warning for warning in reset_report["warnings"])

    incompatible = copy.deepcopy(after)
    incompatible["instance_id"] = "different-instance"
    incompatible_path = directory / "incompatible.json"
    incompatible_path.write_text(json.dumps(incompatible), encoding="utf-8")
    assert compare(before_path, incompatible_path, directory / "invalid-report.json",
                   check=False).returncode != 0, "Different instances must be rejected"

    assert all("backend_xmin_age" in row for row in after["sections"]["activity"]["rows"])
    assert "xmin_horizon_holders" in report["current"] and "autovacuum_backlog" in report["current"]
    # The fixture ANALYZEs the partitioned parent, so last_analyze must be reported for it.
    assert not any(x.get("relname") == "events" for x in report["current"]["autovacuum_backlog"]), \
        "A manually analyzed partitioned parent was flagged"

    collected = {row["name"]: row["setting"] for row in after["sections"]["settings"]["rows"]}
    assert collected["statement_timeout"] == "0", "The collector's own SET LOCAL leaked into settings"

    export = run(["docker", "exec", "-i", container, "psql", "-X", "-qAt", "--no-password", "--file=-"],
                 sql=CONFIG_SQL.read_text(encoding="utf-8")).stdout
    settings = json.loads(export)
    by_name = {row["name"]: row for row in settings["settings"]}
    assert by_name["statement_timeout"]["setting"] == "0", "The export's own SET LOCAL leaked"
    assert by_name["primary_conninfo"]["setting"] == "<redacted>"
    settings_path = directory / "settings.json"
    settings_path.write_text(export, encoding="utf-8")
    review = run([sys.executable, str(CONFIG_SCRIPT), str(settings_path), "--ram-gb", "8", "--cpus", "4",
                  "--storage", "ssd", "--interval-report", str(report_path), "--format", "json"]).stdout
    assert any(f["parameter"] == "shared_buffers" for f in json.loads(review)["findings"])

    samples_path = directory / "samples.json"
    run([sys.executable, str(SCRIPTS / "sample_activity.py"), "--docker-container", container,
         "--instance-id", container, "--samples", "3", "--interval", "0.2", "--output", str(samples_path)])
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    assert samples["summary"]["samples"] == 3 and samples["error"] is None, samples

    limited = collect(container, directory / "limited.json", row_limit=1)
    assert limited["sections"]["tables"]["truncated"], "Row limit did not mark truncation"
    assert limited["sections"]["pgss"]["truncated"], "SQL row limit did not mark truncation"
    assert len(limited["sections"]["pgss"]["rows"]) == 1
    return {"server_version": after["metadata"]["server_version"],
            "ok_sections": sum(s["status"] == "ok" for s in after["sections"].values()),
            "unavailable_sections": [n for n, s in after["sections"].items()
                                     if s["status"] == "unavailable"],
            "sql_interval_rows": len(report["deltas"]["pgss"]),
            "checks": ["all available SQL", "non-public extension schema", "partition metadata",
                       "default SQL redaction", "interval counters", "temp spill",
                       "reset suppression", "instance rejection", "truncation",
                       "targeted planner statistics", "quoted regclass input",
                       "xmin age columns", "activity sampling", "settings export and config review"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="postgres:18-bookworm",
                        help="Locally available official postgres 18/19 image; no automatic pull")
    args = parser.parse_args(argv)
    # Restrict this test harness to the expected public vendor image, not an arbitrary command image.
    if not re.fullmatch(r"(?:docker\.io/library/)?postgres:(?:18|19)[A-Za-z0-9_.-]*", args.image):
        parser.error("--image must be an official postgres:18... or postgres:19... tag")
    name = "pgskills-integration-" + uuid.uuid4().hex[:12]
    container_id = None
    exit_code = 1
    try:
        command = ["docker", "run", "--pull=never", "--rm", "-d", "--name", name,
                   "--network", "none", "--tmpfs", "/var/lib/postgresql",
                   "--label", "codex.task=" + TASK_LABEL,
                   "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                   "-e", "PGUSER=postgres", "-e", "PGDATABASE=postgres",
                   args.image, "-c", "shared_preload_libraries=pg_stat_statements",
                   "-c", "track_io_timing=on", "-c", "track_wal_io_timing=on"]
        container_id = run(command, timeout=60).stdout.strip()
        details = check_created_container(container_id, name)
        assert details["HostConfig"]["NetworkMode"] == "none"
        assert "/var/lib/postgresql" in details["HostConfig"].get("Tmpfs", {})
        assert not details["HostConfig"].get("PortBindings")
        deadline = time.monotonic() + 60
        while True:
            # The entrypoint's temporary init server listens on the socket only; TCP means the final server.
            ready = run(["docker", "exec", name, "psql", "-h", "127.0.0.1", "-X", "-qAt", "--no-password",
                         "-c", "SELECT current_setting('shared_preload_libraries')"],
                        timeout=10, check=False)
            if ready.returncode == 0 and "pg_stat_statements" in ready.stdout:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("The isolated PostgreSQL container did not become ready in 60 seconds")
            time.sleep(0.25)
        with tempfile.TemporaryDirectory(prefix="pgskills-integration-") as scratch:
            result = exercise(name, Path(scratch))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        exit_code = 0
    except (AssertionError, RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"integration failed: {exc}", file=sys.stderr)
    finally:
        if container_id:
            # Never enumerate containers or remove by name/prefix: only this returned and checked ID.
            try:
                check_created_container(container_id, name)
                run(["docker", "rm", "-f", container_id], timeout=30)
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                print(f"cleanup failed for newly created container {container_id}: {exc}", file=sys.stderr)
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
