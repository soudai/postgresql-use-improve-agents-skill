#!/usr/bin/env python3
"""Exercise the security skill against disposable PostgreSQL Docker containers."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import uuid

from scenarios import Cluster, ROOT, run

SCRIPTS = ROOT / "skills/postgres-security-review/scripts"
sys.path.insert(0, str(SCRIPTS))
from collect import Collector
from review import combine, make_report
from report import render

CANARY = "security_fixture_canary_6efbe11c"
SETUP = f"""
CREATE ROLE web LOGIN;
CREATE ROLE observer LOGIN;
CREATE ROLE proxy_role NOLOGIN;
CREATE ROLE admin_path NOLOGIN BYPASSRLS;
GRANT admin_path TO proxy_role WITH INHERIT FALSE, SET TRUE;
GRANT proxy_role TO web WITH INHERIT FALSE, SET TRUE;
CREATE ROLE owner NOLOGIN;
CREATE SCHEMA app AUTHORIZATION owner;
GRANT USAGE ON SCHEMA app TO web;
GRANT CREATE ON SCHEMA app TO web;
CREATE TABLE app.orders (tenant text NOT NULL, id int NOT NULL) PARTITION BY RANGE(id);
CREATE TABLE app.orders_first PARTITION OF app.orders FOR VALUES FROM (0) TO (10);
INSERT INTO app.orders VALUES ('a',1), ('b',2);
ALTER TABLE app.orders OWNER TO owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.orders TO web;
GRANT SELECT ON app.orders_first TO web;
CREATE POLICY tenant ON app.orders FOR ALL TO web USING
    (tenant=current_setting('app.tenant',true) AND '{CANARY}' <> '');
CREATE VIEW app.orders_view AS SELECT * FROM app.orders;
GRANT SELECT ON app.orders_view TO web;
CREATE TABLE app.lookup (val text);
INSERT INTO app.lookup VALUES ('safe');
CREATE FUNCTION app.read_lookup() RETURNS text LANGUAGE sql SECURITY DEFINER
    SET search_path TO app AS $$ SELECT val FROM lookup $$;
CREATE FUNCTION app.count_all_orders() RETURNS bigint LANGUAGE sql SECURITY DEFINER
    SET search_path TO app, pg_temp BEGIN ATOMIC SELECT count(*) FROM app.orders; END;
SET password_encryption='md5';
ALTER ROLE web PASSWORD '{CANARY}';
SET password_encryption='scram-sha-256';
ALTER ROLE observer PASSWORD '{CANARY}';
"""

FIX = f"""
ALTER TABLE app.orders ENABLE ROW LEVEL SECURITY;
ALTER VIEW app.orders_view SET (security_invoker=true);
REVOKE SELECT ON app.orders_first FROM web;
REVOKE proxy_role FROM web;
REVOKE CREATE ON SCHEMA app FROM web;
ALTER FUNCTION app.read_lookup() SET search_path TO app, pg_temp;
REVOKE EXECUTE ON FUNCTION app.count_all_orders() FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
SET password_encryption='scram-sha-256';
ALTER ROLE web PASSWORD '{CANARY}';
"""


def snapshot(cluster, role, privileged, limit=3000):
    # Replace only the test process's psql executable with a container-local client.
    with tempfile.TemporaryDirectory() as directory:
        psql = Path(directory) / "psql"
        psql.write_text("#!/bin/sh\nexec docker exec -i " + shlex.quote(cluster.name) +
                        " psql -U " + shlex.quote(role) + ' "$@"\n', encoding="utf-8")
        psql.chmod(0o700)
        previous = os.environ["PATH"]
        try:
            os.environ["PATH"] = directory + os.pathsep + previous
            result = Collector(3000).collect(cluster.name, "scen", limit, privileged)
        finally:
            os.environ["PATH"] = previous
    assert CANARY not in json.dumps(result), "credential or policy literal escaped"
    return result


def expect(report, rule, status, subject=None):
    if not any(f["rule"] == rule and f["status"] == status and
               (subject is None or f["subject"] == subject) for f in report["findings"]):
        raise AssertionError(f"expected {rule} {status} {subject}")


def access_checks(cluster):
    assert cluster.psql("BEGIN; SET LOCAL app.tenant='a'; SELECT id FROM app.orders; COMMIT;",
                        extra=("-U", "web")).strip() == "1"
    assert cluster.psql("SELECT id FROM app.orders;", extra=("-U", "web")).strip() == ""
    assert cluster.psql("""BEGIN; SET LOCAL app.tenant='b'; SELECT id FROM app.orders_view;
                          COMMIT; SELECT id FROM app.orders_view;""", extra=("-U", "web")).strip() == "2"
    # Check exit status through the actual login rather than relying on SELECT filtering.
    for statement in ("SELECT * FROM app.orders_first;",
                      "SET app.tenant='a'; INSERT INTO app.orders VALUES ('b',3);",
                      "SET app.tenant='a'; UPDATE app.orders SET tenant='b' WHERE id=1;"):
        result = run(["docker", "exec", "-i", cluster.name, "psql", "-XqAtw", "-U", "web",
                      "-v", "ON_ERROR_STOP=1"], sql=statement, check=False)
        assert result.returncode and ("permission denied" in result.stderr or "row-level security" in result.stderr)
    allowed = cluster.psql("""BEGIN; SET LOCAL app.tenant='a';
                               INSERT INTO app.orders VALUES ('a',3) RETURNING id;
                               DELETE FROM app.orders WHERE id=2 RETURNING id;
                               ROLLBACK;""", extra=("-U", "web"))
    assert allowed.strip() == "3"


def tls_checks(cluster):
    shell = r"""
set -eu
cd "$PGDATA"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
    -addext subjectAltName=DNS:localhost -keyout server.key -out server.crt >/dev/null 2>&1
chmod 600 server.key
printf '\nssl = on\n' >> postgresql.conf
"""
    cluster.shell(shell)
    cluster.value("SELECT pg_reload_conf()")
    deadline = time.monotonic() + 10
    while cluster.value("SHOW ssl") != "on":
        assert time.monotonic() < deadline, "TLS did not load"
        time.sleep(.2)
    cluster.shell(r"""
cd "$PGDATA"
cp pg_hba.conf pg_hba.before
printf 'local all all trust\nhostssl scen web 127.0.0.1/32 scram-sha-256\nhost all all all reject\n' > security-include.conf
printf 'include security-include.conf\n' > pg_hba.conf
""")
    # pg_hba_file_rules sees current contents before the settings are reloaded.
    changed = snapshot(cluster, "postgres", True)
    hba = changed["sections"]["hba"]
    assert hba["status"] == "ok" and [r["method"] for r in hba["rows"]] == ["trust", "scram-sha-256", "reject"], hba
    assert cluster.psql("SELECT 1", extra=("-h", "127.0.0.1", "-U", "web")).strip() == "1"
    cluster.value("SELECT pg_reload_conf()")
    cluster.shell(f"""
set -eu
umask 077
cd "$PGDATA"
printf '*:*:*:web:%s\\n' '{CANARY}' > test.pgpass
export PGPASSFILE="$PGDATA/test.pgpass" PGSSLMODE=verify-full PGSSLROOTCERT="$PGDATA/server.crt"
deadline=$((SECONDS+10))
until psql -XqAtw 'host=localhost hostaddr=127.0.0.1 dbname=scen user=web' -c 'SELECT 1' >/dev/null 2>&1; do
  test "$SECONDS" -lt "$deadline"
  sleep .2
done
if psql -XqAtw 'host=wrong.invalid hostaddr=127.0.0.1 dbname=scen user=web' -c 'SELECT 1' >/dev/null 2>&1; then
  exit 1
fi
if PGSSLMODE=disable psql -XqAtw -h 127.0.0.1 -U web -c 'SELECT 1' >/dev/null 2>&1; then
  exit 1
fi
printf 'invalid line\\n' >> security-include.conf
""")
    invalid = snapshot(cluster, "postgres", True)
    assert any(r["invalid"] for r in invalid["sections"]["hba"]["rows"])
    cluster.shell('cp "$PGDATA/pg_hba.before" "$PGDATA/pg_hba.conf"')
    cluster.value("SELECT pg_reload_conf()")
    deadline = time.monotonic() + 10
    while run(["docker", "exec", cluster.name, "psql", "-XqAtw", "-h", "127.0.0.1",
               "-U", "web", "-c", "SELECT 1"], check=False).returncode:
        assert time.monotonic() < deadline, "restored auth route did not reconnect"
        time.sleep(.2)


def review_scenario(cluster):
    cluster.psql(SETUP)
    initial = snapshot(cluster, "observer", False)
    for section in ("roles", "memberships", "acl", "relations", "policies", "settings", "transport"):
        assert initial["sections"][section]["status"] == "ok", (section, initial["sections"][section])
    assert snapshot(cluster, "observer", False, limit=1)["sections"]["roles"]["status"] == "truncated"
    restricted = snapshot(cluster, "observer", True)
    assert restricted["sections"]["passwords"]["reason"] == "sqlstate_42501"
    assert restricted["sections"]["hba"]["reason"] == "sqlstate_42501"
    initial = combine(initial, snapshot(cluster, "postgres", True))
    info = {"target": cluster.name, "database": "scen", "server_version": initial["identity"]["version"],
            "observed_at": datetime.now(timezone.utc).isoformat()}
    process_uid = int(cluster.shell("awk '/^Uid:/ {print $3}' /proc/1/status").stdout.strip())
    contract = {"target": cluster.name, "database": "scen",
                "runtime_roles": {"web": {"privileges": [
                    {"object": ["relation", "app", "orders_first", ""], "privilege": "SELECT", "allowed": False},
                    {"object": ["column", "app", "orders", "tenant"], "privilege": "UPDATE", "allowed": False}]}},
                "rls": [{"schema": "app", "table": "orders", "roles": {"web": ["select", "insert", "update", "delete"]}}],
                "deny_default_grants": [{"creator": "owner", "kind": "f", "grantee": "PUBLIC", "privilege": "EXECUTE"}],
                "restrict_public_routines": [["app", "count_all_orders", ""]],
                "routes": [{"id": "loopback", "role": "web", "database": "scen",
                            "address": "127.0.0.1", "transport": "plain"}],
                "password_roles": {"web": {"method": "password"}},
                "runtime": {**info, "process_uid": process_uid, "entrypoint_uid": 0, "privileged": False}}
    today = datetime.now(timezone.utc).date()
    before = make_report(initial, contract, today)
    for rule in ("ROLE", "RLS", "RLS_BYPASS", "RLS_PATH", "DEFINER", "ACL", "HBA", "PASSWORD", "DEFAULT_ACL", "PUBLIC_EXECUTE"):
        expect(before, rule, "finding")
    expect(before, "RUNTIME", "pass", "postgres process")
    assert cluster.psql("SELECT id FROM app.orders ORDER BY id", extra=("-U", "web")).strip() == "1\n2"
    assert cluster.psql("CREATE TEMP TABLE lookup(val text); INSERT INTO lookup VALUES ('shadow'); "
                        "SELECT app.read_lookup();", extra=("-U", "web")).strip() == "shadow"
    assert cluster.psql("SELECT app.count_all_orders();", extra=("-U", "web")).strip() == "2"
    cluster.psql(FIX)
    access_checks(cluster)
    assert run(["docker", "exec", cluster.name, "psql", "-XqAtw", "-U", "web", "-c", "SELECT app.count_all_orders()"],
               check=False).returncode
    assert cluster.psql("CREATE TEMP TABLE lookup(val text); INSERT INTO lookup VALUES ('shadow'); "
                        "SELECT app.read_lookup();", extra=("-U", "web")).strip() == "safe"
    cluster.psql("SET ROLE owner; CREATE FUNCTION app.private_call() RETURNS int LANGUAGE sql AS 'SELECT 1'; RESET ROLE;")
    assert run(["docker", "exec", cluster.name, "psql", "-XqAtw", "-U", "web", "-c", "SELECT app.private_call()"],
               check=False).returncode
    after = make_report(snapshot(cluster, "postgres", True), contract, today, previous=before)
    expect(after, "ROLE", "pass", "web")
    expect(after, "PUBLIC_EXECUTE", "pass")
    expect(before, "RLS_PATH", "finding", ["app", "orders", "web", ["routine", "app", "count_all_orders", ""]])
    expect(after, "PASSWORD", "pass", "web")
    expect(after, "CREDENTIAL_LIFECYCLE", "unknown", "web")
    expect(after, "RLS_POLICY", "unknown", ["app", "orders", "web", "update"])
    assert not any(f["rule"] in {"RLS", "RLS_BYPASS", "RLS_PATH", "DEFAULT_ACL"} and f["status"] == "finding"
                   for f in after["findings"])
    assert after["comparison"]["resolved"]
    for format_name in ("json", "markdown", "issue"):
        rendered = render(after, format_name)
        assert CANARY not in rendered
        if format_name == "issue":
            assert cluster.name not in rendered and "orders" not in rendered
    tls_checks(cluster)
    print("Security rules, RLS allow/deny, shadowing, SCRAM, TLS and HBA recovery:", initial["identity"]["version"])


def reject_tcp(cluster, configure=False):
    if configure:
        cluster.shell(r"""printf 'local all all trust\nhost all all all reject\n' > "$PGDATA/pg_hba.conf" """)
        cluster.value("SELECT pg_reload_conf()")
    deadline = time.monotonic() + 10
    while True:
        result = run(["docker", "exec", cluster.name, "psql", "-XqAtw", "-h", "127.0.0.1",
                      "-U", "web", "-c", "SELECT 1"], check=False)
        if result.returncode and "pg_hba.conf rejects connection" in result.stderr:
            return
        assert time.monotonic() < deadline, "HBA rejection did not take effect"
        time.sleep(.2)


def migration(source_image, destination_image, directory):
    source = Cluster(source_image, directory)
    dest = Cluster(destination_image, directory)
    try:
        source.start()
        source.psql(SETUP + FIX)
        reject_tcp(source, configure=True)
        roles = run(["docker", "exec", source.name, "pg_dumpall", "--roles-only", "--no-role-passwords"]).stdout
        source_version = source.value("SHOW server_version")
        dump = run(["docker", "exec", source.name, "pg_dump", "scen"]).stdout
        dest.start()
        # The destination already contains the bootstrap login. Preserve all other grants.
        dest.psql(roles.replace("CREATE ROLE postgres;\n", "", 1))
        dest.psql(dump)
        access_checks(dest)
        reject_tcp(dest, configure=True)
        assert dest.value("SELECT count(*) FROM app.orders") == "2"
        print("Logical backup, restore and major migration:", source_version, "→", dest.value("SHOW server_version"))
    finally:
        dest.stop()
        source.stop()


def minor_update(old, new):
    # Both images use the same major's PGDATA layout. Only a unique test volume is shared.
    volume = "pgskills-sec-" + uuid.uuid4().hex
    label = "codex.task=postgres-security-review"
    volume_id = run(["docker", "volume", "create", "--label", label, volume]).stdout.strip()
    if volume_id != volume:
        raise RuntimeError("unexpected test volume")
    containers = []
    try:
        versions = []
        for index, image in enumerate((old, new)):
            container = run(["docker", "run", "--pull=never", "--rm", "-d", "--network", "none",
                             "--mount", f"source={volume},target=/var/lib/postgresql",
                             "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=scen",
                             "-e", "POSTGRES_INITDB_ARGS=--locale=C", "-e", "PGUSER=postgres", "-e", "PGDATABASE=scen",
                             "--label", label, image]).stdout.strip()
            containers.append(container)
            deadline = time.monotonic() + 60
            address = ["-h", "127.0.0.1"] if index == 0 else []
            while run(["docker", "exec", container, "psql", "-XqAtw", *address, "-c", "SELECT 1"],
                      check=False).returncode:
                assert time.monotonic() < deadline, "minor test cluster did not start"
                time.sleep(.25)
            cluster = Cluster(image, None)
            cluster.name = container
            versions.append(int(cluster.value("SHOW server_version_num")))
            if index == 0:
                cluster.psql(SETUP + FIX)
            access_checks(cluster)
            reject_tcp(cluster, configure=index == 0)
            run(["docker", "stop", "-t", "30", container])
        assert versions[0] // 10000 == versions[1] // 10000 and versions[0] < versions[1]
        print("In-place minor update with the same data:", *versions)
    finally:
        for container in containers:
            run(["docker", "rm", "-f", container], check=False)
        run(["docker", "volume", "rm", volume])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--major-source")
    parser.add_argument("--minor-source")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        cluster = Cluster(args.image, Path(tmp))
        try:
            cluster.start()
            review_scenario(cluster)
        finally:
            cluster.stop()
        if args.major_source:
            migration(args.major_source, args.image, Path(tmp))
        if args.minor_source:
            minor_update(args.minor_source, args.image)


if __name__ == "__main__":
    main()
