#!/usr/bin/env python3
"""Check security counterexamples against a disposable, network-isolated PostgreSQL."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time

from security_scenarios import Cluster, expect, make_report, run, snapshot

PASSWORD = "synthetic-regression-password"


def capture(cluster):
    s = snapshot(cluster, "postgres", True)
    return s, {"target": s["target"], "database": s["identity"]["database"]}


def child_statuses(report, effective="web"):
    return {f["subject"][-1]: f["status"] for f in report["findings"]
            if f["rule"] == "RLS_PATH" and isinstance(f["subject"], list)
            and "child" in f["subject"] and f["subject"][-2] == effective}


def rls_checks(cluster):
    cluster.psql("""
CREATE ROLE web LOGIN;
CREATE ROLE child_owner NOLOGIN;
CREATE SCHEMA app;
GRANT USAGE ON SCHEMA app TO web, child_owner;
CREATE TABLE app.parent(tenant text, id int) PARTITION BY RANGE(id);
CREATE TABLE app.child PARTITION OF app.parent FOR VALUES FROM(0) TO(10);
INSERT INTO app.parent VALUES('a',1),('b',2);
ALTER TABLE app.parent ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.child ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant ON app.parent TO web USING(tenant='a');
CREATE POLICY unrestricted ON app.child TO web USING(true);
GRANT SELECT, INSERT, UPDATE, DELETE ON app.parent,app.child TO web;
""")
    s, c = capture(cluster)
    c["rls"] = [{"schema": "app", "table": "parent",
                 "roles": {"web": ["select", "insert", "update", "delete"]}}]
    report = make_report(s, c, datetime.now(timezone.utc))
    assert set(child_statuses(report).values()) == {"finding"}
    assert cluster.psql("SELECT tenant FROM app.parent ORDER BY tenant", extra=("-U", "web")).strip() == "a"
    assert cluster.psql("SELECT tenant FROM app.child ORDER BY tenant", extra=("-U", "web")).strip() == "a\nb"
    cluster.psql("CREATE POLICY narrow ON app.child AS RESTRICTIVE TO web USING(tenant='a')")
    report = make_report(capture(cluster)[0], c, datetime.now(timezone.utc))
    assert set(child_statuses(report).values()) == {"unknown"}
    assert cluster.psql("SELECT tenant FROM app.child", extra=("-U", "web")).strip() == "a"
    assert cluster.psql("UPDATE app.child SET id=id WHERE tenant='b' RETURNING tenant",
                        extra=("-U", "web")).strip() == ""
    assert cluster.psql("DELETE FROM app.child WHERE tenant='b' RETURNING tenant",
                        extra=("-U", "web")).strip() == ""
    for statement in ("INSERT INTO app.child VALUES('b',3)",
                      "UPDATE app.child SET tenant='b' WHERE tenant='a'"):
        result = run(["docker", "exec", "-i", cluster.name, "psql", "-XqAtw", "-U", "web",
                      "-v", "ON_ERROR_STOP=1"], sql=statement, check=False)
        assert result.returncode and "row-level security" in result.stderr
    cluster.psql("""
ALTER TABLE app.child OWNER TO child_owner;
GRANT child_owner TO web WITH INHERIT FALSE, SET TRUE;
""")
    report = make_report(capture(cluster)[0], c, datetime.now(timezone.utc))
    assert set(child_statuses(report, "child_owner").values()) == {"finding"}
    assert cluster.psql("SET ROLE child_owner; SELECT tenant FROM app.child ORDER BY tenant",
                        extra=("-U", "web")).strip() == "a\nb"
    cluster.psql("ALTER TABLE app.child FORCE ROW LEVEL SECURITY")
    report = make_report(capture(cluster)[0], c, datetime.now(timezone.utc))
    assert set(child_statuses(report, "child_owner").values()) == {"pass"}
    assert cluster.psql("SET ROLE child_owner; SELECT tenant FROM app.child",
                        extra=("-U", "web")).strip() == ""
    cluster.psql("ALTER ROLE child_owner BYPASSRLS")
    report = make_report(capture(cluster)[0], c, datetime.now(timezone.utc))
    assert set(child_statuses(report, "child_owner").values()) == {"finding"}
    assert cluster.psql("SET ROLE child_owner; SELECT tenant FROM app.child ORDER BY tenant",
                        extra=("-U", "web")).strip() == "a\nb"


def key_checks(cluster):
    s, c = capture(cluster)
    uid = int(cluster.shell("id -u").stdout)
    gid = int(cluster.shell("id -g").stdout)
    info = {**c, "server_version": s["identity"]["version"], "observed_at": datetime.now(timezone.utc).isoformat()}
    file = {"id": "key", "kind": "tls_key", "platform": "unix", "mode": "0640", "owner_uid": 0}
    c["runtime"] = {**info, "process_uid": uid, "files": [file]}
    expect(make_report(s, c, datetime.now(timezone.utc)), "FILE_ACCESS", "unknown", "key")
    # The generated disposable key never leaves the network-isolated container.
    cluster.shell(r"""
set -eu
cd "$PGDATA"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
    -keyout server.key -out server.crt >/dev/null 2>&1
chmod 600 server.key
""")
    cluster.shell('cp "$PGDATA/server.key" /tmp/rootkey; chown 0:0 /tmp/rootkey', user="root")
    for mode in ("0600", "0640"):
        cluster.shell("chmod " + mode + " /tmp/rootkey", user="root")
        readable = cluster.shell("test -r /tmp/rootkey", check=False).returncode == 0
        assert readable is False
        file.update(mode=mode, owner_gid=0, service_readable=readable, unauthorized_access=False)
        expect(make_report(s, c, datetime.now(timezone.utc)), "FILE_ACCESS", "finding", "key")
    cluster.shell('chown 0:' + str(gid) + ' "$PGDATA/server.key"; chmod 640 "$PGDATA/server.key"', user="root")
    file.update(mode="0640", owner_gid=gid,
                service_readable=cluster.shell('test -r "$PGDATA/server.key"', check=False).returncode == 0)
    assert file["service_readable"]
    # Give the synthetic unrelated identity the key's group: mode 0640 is no boundary against it.
    # Parent data directory may deny traversal, so test a disposable key copy in /tmp.
    cluster.shell('cp "$PGDATA/server.key" /tmp/sharedkey; chown 0:' + str(gid) +
                  ' /tmp/sharedkey; chmod 640 /tmp/sharedkey', user="root")
    shared = run(["docker", "exec", "-u", "1234:" + str(gid), cluster.name,
                  "test", "-r", "/tmp/sharedkey"], check=False)
    assert shared.returncode == 0
    file["unauthorized_access"] = True
    expect(make_report(s, c, datetime.now(timezone.utc)), "FILE_ACCESS", "finding", "key")
    denied = run(["docker", "exec", "-u", "1234:1234", cluster.name,
                  "test", "-r", "/tmp/sharedkey"], check=False)
    assert denied.returncode != 0
    file["unauthorized_access"] = False  # Synthetic unrelated reader now uses its own group.
    expect(make_report(s, c, datetime.now(timezone.utc)), "FILE_ACCESS", "pass", "key")
    cluster.shell(r"""printf '\nssl = on\n' >> "$PGDATA/postgresql.conf" """)
    cluster.value("SELECT pg_reload_conf()")
    deadline = time.monotonic() + 10
    while cluster.value("SHOW ssl") != "on":
        assert time.monotonic() < deadline, "root/group key did not enable TLS"
        time.sleep(.2)
    assert cluster.psql("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()",
                        extra=("-h", "127.0.0.1")).strip() == "t"


def password_checks(cluster):
    cluster.psql("SET password_encryption='scram-sha-256'; ALTER ROLE web PASSWORD '" + PASSWORD + "'")
    expiry = cluster.value("SELECT date_trunc('day',now())::text")
    cluster.psql("ALTER ROLE web VALID UNTIL '" + expiry + "'")
    s, c = capture(cluster)
    c["password_roles"] = {"web": {"method": "password"}}
    now = datetime.now(timezone.utc)
    expect(make_report(s, c, now), "PASSWORD", "finding", "web")
    expect(make_report(s, c, now.date()), "PASSWORD", "unknown", "web")
    cluster.shell(r"""printf 'local all all trust\nhost all all 127.0.0.1/32 scram-sha-256\n' > "$PGDATA/pg_hba.conf" """)
    cluster.value("SELECT pg_reload_conf()")
    command = ["docker", "exec", "-e", "PGPASSWORD=" + PASSWORD, cluster.name, "psql", "-XqAtw",
               "-h", "127.0.0.1", "-U", "web", "-c", "SELECT 1"]
    deadline = time.monotonic() + 10
    while True:
        expired = run(command, check=False)
        if expired.returncode:
            assert "password authentication failed" in expired.stderr
            break
        assert time.monotonic() < deadline, "SCRAM expiry test did not reject a fresh connection"
        time.sleep(.2)
    cluster.psql("ALTER ROLE web VALID UNTIL 'infinity'")
    assert run(command).stdout.strip() == "1"
    expect(make_report(capture(cluster)[0], c, datetime.now(timezone.utc)), "PASSWORD", "pass", "web")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        cluster = Cluster(args.image, Path(directory))
        try:
            cluster.start()
            rls_checks(cluster)
            key_checks(cluster)
            password_checks(cluster)
            print("Child RLS, effective key access/TLS, timestamp expiry:", cluster.value("SHOW server_version"))
        finally:
            cluster.stop()


if __name__ == "__main__":
    main()
