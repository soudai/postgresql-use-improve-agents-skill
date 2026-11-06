#!/usr/bin/env python3
"""Exercise the follow-up diagnostics in a disposable PostgreSQL 18/19 container."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
import time

from scenarios import ANALYZE, Cluster, CONFIG_SCRIPT, PLAN, ROOT, SCRIPTS, run, wait_until


def inspect(cluster, relation):
    return json.loads(cluster.psql((SCRIPTS.parent / "sql/inspect_relation.sql").read_text(),
                                   extra=("-v", f"relation={relation}", "-f", "-")))


def dml_and_partition(cluster):
    cluster.psql("""
CREATE TABLE parent (id int PRIMARY KEY);
CREATE TABLE child (id int REFERENCES parent);
INSERT INTO parent SELECT generate_series(1, 4);
INSERT INTO child SELECT 1 FROM generate_series(1, 30000);
CREATE FUNCTION slow_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM pg_sleep(.01); RETURN OLD; END $$;
CREATE TRIGGER slow_delete AFTER DELETE ON parent FOR EACH ROW EXECUTE FUNCTION slow_delete();
CREATE TABLE parts (k int, payload int) PARTITION BY RANGE (k);
CREATE TABLE part1 PARTITION OF parts FOR VALUES FROM (0) TO (10);
CREATE TABLE part2 PARTITION OF parts FOR VALUES FROM (10) TO (20);
CREATE TABLE part3 PARTITION OF parts FOR VALUES FROM (20) TO (30);
INSERT INTO parts SELECT i,i FROM generate_series(0,29) i;
ANALYZE parts;
""")
    def delete():
        plan = cluster.psql("BEGIN; EXPLAIN (ANALYZE, WAL, BUFFERS, FORMAT JSON) "
                            "DELETE FROM parent WHERE id IN (2,3,4); ROLLBACK;")
        return PLAN.analyze(json.loads(plan))["plans"][0]

    before = delete()
    assert any(t.get("Constraint Name") and t["Calls"] == 3 for t in before["triggers"]), before["triggers"]
    assert any(t.get("Trigger Name") == "slow_delete" and t["Time"] > 0 for t in before["triggers"])
    assert any(t["constraint_type"] == "f" for t in inspect(cluster, "public.parent")["constraints"])
    cluster.psql("CREATE INDEX child_id ON child (id); ANALYZE child;")
    after = delete()
    assert cluster.value("SELECT count(*) FROM parent") == "4"
    assert after["triggers"] and after["nodes"][0]["metrics"]["WAL Records"] > 0

    tree = inspect(cluster, "public.parts")["partition_tree"]
    assert len(tree["members"]) == 4 and not tree["truncated"], tree
    assert all(member["bound"] is None for member in tree["members"])
    text = cluster.psql("SET plan_cache_mode=force_generic_plan; PREPARE q(int) AS SELECT sum(payload) FROM parts WHERE k=$1;"
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE q(3);")
    pruned = PLAN.analyze(json.loads(text))["plans"][0]["partition_evidence"]
    assert any(p["subplans_removed"] == 2 for p in pruned), pruned
    full = cluster.explain("SELECT sum(payload) FROM parts WHERE k + 0 = 3")["partition_evidence"]
    assert any(len(p["children"]) == 3 for p in full), full
    assert cluster.value("SELECT sum(payload) FROM parts WHERE k + 0 = 3") == "3"
    print("DML trigger and partition evidence passed", flush=True)


def config(cluster):
    sql = (CONFIG_SCRIPT.parent.parent / "sql/settings.sql").read_text()
    cluster.psql("CREATE ROLE followup_app LOGIN; ALTER DATABASE scen SET statement_timeout='5s'; "
                 "ALTER ROLE followup_app SET statement_timeout='4s'; "
                 "ALTER ROLE followup_app IN DATABASE scen SET statement_timeout='3s';")
    def export():
        return json.loads(cluster.psql(sql, extra=("-U", "followup_app", "-v", f"instance_id={cluster.name}", "-f", "-")))

    def effective(document):
        row = next(r for r in document["settings"] if r["name"] == "statement_timeout")
        return row["setting"], row["source"]

    before = export()
    assert effective(before) == ("3000", "database user"), effective(before)
    assert {o["setting"] for o in before["scoped_overrides"] if o["name"] == "statement_timeout"} == {"3s", "4s", "5s"}
    cluster.psql("ALTER ROLE followup_app IN DATABASE scen SET statement_timeout='2s';")
    assert effective(export())[0] == "2000"
    cluster.psql("ALTER ROLE followup_app IN DATABASE scen SET statement_timeout='3s';")
    assert effective(export()) == effective(before)
    cluster.psql("ALTER ROLE followup_app IN DATABASE scen RESET statement_timeout;")
    assert effective(export()) == ("4000", "user")
    cluster.psql("ALTER DATABASE scen RESET statement_timeout;")
    print("Configuration precedence and restoration passed", flush=True)


def logical(cluster):
    cluster.shell("initdb --no-locale -A trust -D /tmp/followup-sub >/dev/null && "
                  "pg_ctl -D /tmp/followup-sub -o '-p 5433 -k /tmp' -l /tmp/followup-sub.log -w start >/dev/null")
    cluster.psql("CREATE TABLE logical_items (id int PRIMARY KEY, value text); "
                 "INSERT INTO logical_items VALUES (1,'initial'); CREATE PUBLICATION pub FOR TABLE logical_items;")
    subscriber = "/usr/local/bin/psql-followup-sub"
    run(["docker", "exec", "-i", "-u", "root", cluster.name, "sh", "-c",
         f"cat > {subscriber} && chmod 755 {subscriber}"], sql='#!/bin/sh\nexec psql -h /tmp -p 5433 "$@"\n')
    def sub(sql, db="scen"):
        return run(["docker", "exec", "-i", cluster.name, subscriber, "-d", db, "-XqAt",
                    "-v", "ON_ERROR_STOP=1"], sql=sql).stdout.strip()

    sub("CREATE DATABASE scen;", "postgres")
    sub("CREATE TABLE logical_items (id int PRIMARY KEY, value text); "
        "CREATE SUBSCRIPTION followup CONNECTION 'host=/var/run/postgresql port=5432 dbname=scen user=postgres' "
        "PUBLICATION pub WITH (disable_on_error=true, streaming=off);")
    assert wait_until(lambda: sub("SELECT srsubstate FROM pg_subscription_rel") == "r", 30)
    assert sub("SELECT value FROM logical_items WHERE id=1") == "initial"
    assert any(r["enabled"] and r["pid"] for r in cluster.collect(subscriber)["sections"]["subscriptions"]["rows"])
    publisher_before = cluster.collect()
    cluster.psql("INSERT INTO logical_items SELECT i, md5(i::text) FROM generate_series(2,10001) i;")
    assert wait_until(lambda: sub("SELECT count(*) FROM logical_items") == "10001", 30)
    publisher = ANALYZE.analyze(publisher_before, cluster.collect())
    assert any(r["delta"].get("spill_bytes", 0) > 0 for r in publisher["deltas"]["replication_slots"]), publisher["deltas"]["replication_slots"]

    sub("ALTER SUBSCRIPTION followup DISABLE;")
    assert wait_until(lambda: sub("SELECT count(*) FROM pg_stat_subscription WHERE pid IS NOT NULL") == "0", 30)
    disabled = cluster.collect(subscriber)
    assert any(r["enabled"] is False and r["pid"] is None for r in disabled["sections"]["subscriptions"]["rows"])
    sub("INSERT INTO logical_items VALUES (20000,'conflict'); ALTER SUBSCRIPTION followup ENABLE;")
    cluster.psql("INSERT INTO logical_items VALUES (20000,'publisher');")
    assert wait_until(lambda: sub("SELECT subenabled FROM pg_subscription WHERE subname='followup'") == "f", 30)
    report = ANALYZE.analyze(disabled, cluster.collect(subscriber))
    assert any(r["delta"]["apply_error_count"] > 0 for r in report["deltas"]["subscription_stats"])
    assert report["current"]["subscription_relations"][0]["sync_state"] == "r"
    assert report["current"]["subscription_observed_at"]["subscriptions"]
    sub("DELETE FROM logical_items WHERE id=20000; ALTER SUBSCRIPTION followup ENABLE;")
    assert wait_until(lambda: sub("SELECT value FROM logical_items WHERE id=20000") == "publisher", 30)
    assert sub("SELECT count(*) FROM logical_items") == cluster.value("SELECT count(*) FROM logical_items")
    print("Logical replication initial copy, spill, disabled state and apply error passed", flush=True)


def replay(cluster, directory):
    script = ROOT / "skills/postgres-workload-validation/scripts/compare_runs.py"
    cluster.psql("CREATE TABLE replay_target (id int PRIMARY KEY, value int); INSERT INTO replay_target VALUES (1,0);")
    inputs = []
    for write in (False, True):
        sql = "UPDATE replay_target SET value=value+1 WHERE id=1;" if write else "SELECT value FROM replay_target WHERE id=1;"
        runs = []
        for index in range(2):
            prefix = f"replay-{write}-{index}"
            started_at, started = datetime.now(timezone.utc).isoformat(), time.monotonic()
            output = cluster.shell(f"""set -eu
cd /tmp
printf '%s\\n' '{sql}' > {prefix}.sql
pgbench -n -c 2 -t 5 -l --log-prefix={prefix} -f {prefix}.sql >&2
cat {prefix}.[0-9]*
""").stdout
            elapsed = time.monotonic() - started
            log = directory / (prefix + ".log")
            log.write_text(output)
            observed = cluster.value("SELECT value FROM replay_target WHERE id=1")
            assert observed == ("10" if write else "0")
            cluster.psql("UPDATE replay_target SET value=0 WHERE id=1;")
            runs.append({"started_at": started_at, "elapsed_seconds": elapsed, "complete": True,
                         "result_digest": observed, "pgbench_logs": [log.name],
                         "metrics": {"cpu_pct": 1, "io_read_bytes": 0, "wal_bytes": 0, "oom_kills": 0}})
        path = directory / f"replay-{write}.json"
        path.write_text(json.dumps({"conditions": {"fixture": "replay_target-v1", "write": write}, "runs": runs}))
        inputs.append(path)
    for path in inputs:
        result = json.loads(run([sys.executable, str(script), str(path), str(path), "--p95-ms", "1000",
                                 "--min-tps", "1", "--max-loss-rate", "0"]).stdout)
        assert result["decision"] == "accept_candidate", result
    print("Raw pgbench logs for read and write workloads passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"postgres:(?:18|19)[A-Za-z0-9_.-]*", args.image):
        parser.error("use a local postgres:18 or postgres:19 image")
    with tempfile.TemporaryDirectory(prefix="pgskills-followups-") as scratch:
        directory = Path(scratch)
        cluster = Cluster(args.image, directory, extra_args=("-c", "wal_level=logical", "-c", "logical_decoding_work_mem=64kB"))
        try:
            cluster.start()
            print(cluster.value("SELECT version()"), flush=True)
            dml_and_partition(cluster)
            config(cluster)
            logical(cluster)
            replay(cluster, directory)
        finally:
            cluster.stop()


if __name__ == "__main__":
    main()
