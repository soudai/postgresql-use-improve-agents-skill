#!/usr/bin/env python3
"""Acceptance checks for role grants, tenant-context reuse and definer recovery.

Runs only in disposable network-isolated PostgreSQL containers with synthetic data.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from security_scenarios import Cluster, expect, make_report, run, snapshot


def login(cluster, role, sql):
    return cluster.psql(sql, extra=("-U", role)).strip()


def denied(cluster, role, sql):
    result = run(["docker", "exec", "-i", cluster.name, "psql", "-XqAtw", "-U", role,
                  "-v", "ON_ERROR_STOP=1"], sql=sql, check=False)
    assert result.returncode and ("permission denied" in result.stderr or
                                 "must be member" in result.stderr or
                                 "row-level security" in result.stderr), result.stderr


def report(cluster, contract):
    data = snapshot(cluster, "postgres", True)
    return make_report(data, {"target": data["target"], "database": "scen", **contract},
                       datetime.now(timezone.utc))


def role_checks(cluster):
    cluster.psql("""
CREATE ROLE acceptance_app LOGIN;
CREATE ROLE acceptance_other LOGIN;
CREATE ROLE acceptance_owner NOLOGIN;
CREATE ROLE acceptance_ops LOGIN CREATEDB;
CREATE ROLE acceptance_read NOLOGIN;
CREATE ROLE acceptance_middle NOLOGIN;
CREATE ROLE acceptance_elevated NOLOGIN CREATEDB;
GRANT acceptance_read TO acceptance_middle WITH INHERIT TRUE, SET FALSE;
GRANT acceptance_middle TO acceptance_app WITH INHERIT TRUE, SET FALSE;
GRANT acceptance_elevated TO acceptance_app WITH INHERIT TRUE, SET FALSE;
CREATE SCHEMA acceptance AUTHORIZATION acceptance_owner;
GRANT USAGE ON SCHEMA acceptance TO acceptance_app, acceptance_other;
SET ROLE acceptance_owner;
CREATE TABLE acceptance.readable(id int);
INSERT INTO acceptance.readable VALUES(7);
CREATE TABLE acceptance.columns(a int,b int);
INSERT INTO acceptance.columns VALUES(1,2);
CREATE FUNCTION acceptance.initial_public() RETURNS int LANGUAGE sql AS 'SELECT 3';
RESET ROLE;
GRANT SELECT ON acceptance.readable TO acceptance_read;
GRANT SELECT(a),UPDATE(a) ON acceptance.columns TO acceptance_app WITH GRANT OPTION;
""")
    contract = {"runtime_roles": {
        "acceptance_app": {"privileges": [
            {"object": ["relation", "acceptance", "readable", ""], "privilege": "SELECT", "allowed": True},
            {"object": ["column", "acceptance", "columns", "a"], "privilege": "UPDATE", "allowed": True},
            {"object": ["column", "acceptance", "columns", "b"], "privilege": "UPDATE", "allowed": False}]},
        "acceptance_ops": {"allow_flags": ["createdb"]}},
        "deny_default_grants": [{"creator": "acceptance_owner", "kind": "f",
                                "grantee": "PUBLIC", "privilege": "EXECUTE"}]}
    before = report(cluster, contract)
    expect(before, "ROLE", "pass", "acceptance_app")
    assert not any(f["rule"] == "ROLE" and f["status"] == "finding" and
                   f["subject"] == "acceptance_ops" for f in before["findings"])
    expect(before, "ACL", "finding", ["acceptance_app", ["column", "acceptance", "columns", "a"], "UPDATE"])
    expect(before, "DEFAULT_ACL", "finding")
    assert login(cluster, "acceptance_app", "SELECT id FROM acceptance.readable") == "7"
    assert login(cluster, "acceptance_app", "SELECT a FROM acceptance.columns") == "1"
    denied(cluster, "acceptance_app", "SELECT b FROM acceptance.columns")
    denied(cluster, "acceptance_app", "UPDATE acceptance.columns SET b=5")
    denied(cluster, "acceptance_app", "SET ROLE acceptance_read")
    denied(cluster, "acceptance_app", "SET ROLE acceptance_elevated")
    denied(cluster, "acceptance_app", "CREATE DATABASE should_not_exist")
    login(cluster, "acceptance_app",
          "GRANT UPDATE(a) ON acceptance.columns TO acceptance_other")
    assert cluster.value("SELECT has_column_privilege('acceptance_other','acceptance.columns','a','UPDATE')") == "t"
    login(cluster, "acceptance_other", "BEGIN; UPDATE acceptance.columns SET a=8; ROLLBACK")
    login(cluster, "acceptance_app",
          "REVOKE UPDATE(a) ON acceptance.columns FROM acceptance_other")
    # Null ACL uses PostgreSQL's actual PUBLIC function default.
    assert cluster.value("SELECT proacl IS NULL FROM pg_proc WHERE oid='acceptance.initial_public()'::regprocedure") == "t"
    assert login(cluster, "acceptance_other", "SELECT acceptance.initial_public()") == "3"
    cluster.psql("""
REVOKE GRANT OPTION FOR UPDATE(a) ON acceptance.columns FROM acceptance_app;
ALTER DEFAULT PRIVILEGES FOR ROLE acceptance_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE acceptance_owner IN SCHEMA acceptance
    GRANT SELECT,INSERT ON TABLES TO acceptance_app;
ALTER DEFAULT PRIVILEGES FOR ROLE acceptance_owner IN SCHEMA acceptance
    GRANT USAGE,SELECT ON SEQUENCES TO acceptance_app;
ALTER DEFAULT PRIVILEGES FOR ROLE acceptance_owner IN SCHEMA acceptance
    GRANT EXECUTE ON FUNCTIONS TO acceptance_app;
SET ROLE acceptance_owner;
CREATE SEQUENCE acceptance.next_id;
CREATE TABLE acceptance.new_items(id int DEFAULT nextval('acceptance.next_id'));
CREATE FUNCTION acceptance.new_read() RETURNS int LANGUAGE sql AS 'SELECT 9';
RESET ROLE;
""")
    after = report(cluster, contract)
    expect(after, "ACL", "pass", ["acceptance_app", ["column", "acceptance", "columns", "a"], "UPDATE"])
    expect(after, "DEFAULT_ACL", "pass")
    # GRANT without grant option can warn instead of raising an error; inspect its effect.
    login(cluster, "acceptance_app",
          "GRANT UPDATE(a) ON acceptance.columns TO acceptance_other")
    assert cluster.value("SELECT has_column_privilege('acceptance_other','acceptance.columns','a','UPDATE')") == "f"
    assert login(cluster, "acceptance_app", "INSERT INTO acceptance.new_items DEFAULT VALUES RETURNING id") == "1"
    assert login(cluster, "acceptance_app", "SELECT id FROM acceptance.new_items") == "1"
    assert login(cluster, "acceptance_app", "SELECT acceptance.new_read()") == "9"
    denied(cluster, "acceptance_other", "SELECT * FROM acceptance.new_items")
    denied(cluster, "acceptance_other", "SELECT nextval('acceptance.next_id')")
    denied(cluster, "acceptance_other", "SELECT acceptance.new_read()")
    # Restore only the fixture's prior column grant and verify its effective grant option.
    cluster.psql("GRANT UPDATE(a) ON acceptance.columns TO acceptance_app WITH GRANT OPTION")
    expect(report(cluster, contract), "ACL", "finding",
           ["acceptance_app", ["column", "acceptance", "columns", "a"], "UPDATE"])
    cluster.psql("REVOKE GRANT OPTION FOR UPDATE(a) ON acceptance.columns FROM acceptance_app")
    print("Role membership, column grants, PUBLIC defaults, new objects and scoped restoration passed", flush=True)


def tenant_checks(cluster):
    cluster.psql("""
CREATE TABLE acceptance.public_data(id int);
INSERT INTO acceptance.public_data VALUES(10);
CREATE TABLE acceptance.tenants(tenant text NOT NULL, id int);
INSERT INTO acceptance.tenants VALUES('a',1),('b',2);
CREATE TABLE acceptance.denied_tenants(id int);
INSERT INTO acceptance.denied_tenants VALUES(3);
ALTER TABLE acceptance.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE acceptance.denied_tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant ON acceptance.tenants TO acceptance_app
    USING(tenant=current_setting('acceptance.tenant',true));
GRANT SELECT ON acceptance.public_data TO acceptance_app,acceptance_other;
GRANT SELECT,INSERT,UPDATE,DELETE ON acceptance.tenants TO acceptance_app;
GRANT SELECT ON acceptance.tenants,acceptance.denied_tenants TO acceptance_other,acceptance_app;
""")
    contract = {"rls": [
        {"schema": "acceptance", "table": "tenants",
         "roles": {"acceptance_app": ["select","insert","update","delete"]}},
        {"schema": "acceptance", "table": "denied_tenants", "roles": {"acceptance_app": ["select"]}}]}
    result = report(cluster, contract)
    assert not any(isinstance(f["subject"], list) and "public_data" in f["subject"]
                   and f["rule"] == "RLS" for f in result["findings"])
    expect(result, "RLS_POLICY", "unknown", ["acceptance","tenants","acceptance_app","update"])
    expect(result, "RLS_POLICY", "finding", ["acceptance","denied_tenants","acceptance_app","select"])
    assert login(cluster, "acceptance_app", "SELECT * FROM acceptance.public_data") == "10"
    assert login(cluster, "acceptance_other", "SELECT * FROM acceptance.tenants") == ""
    assert login(cluster, "acceptance_app", "SELECT * FROM acceptance.denied_tenants") == ""
    # All three requests run on one connection, like a reused pool backend.
    output = login(cluster, "acceptance_app", """
BEGIN; SET LOCAL acceptance.tenant='a'; SELECT tenant FROM acceptance.tenants; COMMIT;
BEGIN; SET LOCAL acceptance.tenant='b'; SELECT tenant FROM acceptance.tenants; COMMIT;
SELECT count(*) FROM acceptance.tenants;
""")
    assert output == "a\nb\n0", output
    denied(cluster, "acceptance_app", "SET acceptance.tenant='a'; INSERT INTO acceptance.tenants VALUES('b',3)")
    denied(cluster, "acceptance_app", "SET acceptance.tenant='a'; UPDATE acceptance.tenants SET tenant='b' WHERE id=1")
    assert login(cluster, "acceptance_app",
                 "SET acceptance.tenant='a'; DELETE FROM acceptance.tenants WHERE id=2 RETURNING id") == ""
    print("Declared RLS scope, default deny and same-connection A/B/missing context passed", flush=True)


def definer_checks(cluster):
    cluster.psql("""
SET ROLE acceptance_owner;
CREATE TABLE acceptance.lookup(value text);
INSERT INTO acceptance.lookup VALUES('trusted');
RESET ROLE;
CREATE FUNCTION acceptance.lookup_value() RETURNS text LANGUAGE sql SECURITY DEFINER
  SET search_path TO public, acceptance, pg_temp AS 'SELECT value FROM lookup';
CREATE FUNCTION acceptance.qualified_value() RETURNS text LANGUAGE sql SECURITY DEFINER
  SET search_path TO acceptance, pg_temp AS 'SELECT value FROM acceptance.lookup';
GRANT CREATE ON SCHEMA public TO PUBLIC;
""")
    contract = {"runtime_roles": {"acceptance_app": {}},
                "restrict_public_routines": [["acceptance", "lookup_value", ""]]}
    before = report(cluster, contract)
    expect(before, "DEFINER", "finding", ["routine","acceptance","lookup_value","","acceptance_app","acceptance_app"])
    expect(before, "PUBLIC_EXECUTE", "finding", ["routine","acceptance","lookup_value",""])
    expect(before, "DEFINER", "unknown", ["routine","acceptance","qualified_value","","acceptance_app","acceptance_app"])
    # Explicitly qualified public routines remain usable without blanket PUBLIC revocation.
    assert login(cluster, "acceptance_other", "SELECT acceptance.qualified_value()") == "trusted"
    original = cluster.value("""SELECT json_build_object('owner',proowner,'acl',proacl,'config',proconfig)::text
                               FROM pg_proc WHERE oid='acceptance.lookup_value()'::regprocedure""")
    assert login(cluster, "acceptance_app", "SELECT acceptance.lookup_value()") == "trusted"
    login(cluster, "acceptance_app", "CREATE TABLE public.lookup(value text); INSERT INTO public.lookup VALUES('shadow')")
    assert login(cluster, "acceptance_app", "SELECT acceptance.lookup_value()") == "shadow"
    cluster.psql("""
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER FUNCTION acceptance.lookup_value() OWNER TO acceptance_owner;
ALTER FUNCTION acceptance.lookup_value() SET search_path TO acceptance, pg_temp;
REVOKE EXECUTE ON FUNCTION acceptance.lookup_value() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION acceptance.lookup_value() TO acceptance_app;
COMMIT;
""")
    fixed = report(cluster, contract)
    expect(fixed, "PUBLIC_EXECUTE", "pass", ["routine","acceptance","lookup_value",""])
    expect(fixed, "DEFINER", "unknown", ["routine","acceptance","lookup_value","","acceptance_app","acceptance_app"])
    assert login(cluster, "acceptance_app", "SELECT acceptance.lookup_value()") == "trusted"
    denied(cluster, "acceptance_other", "SELECT acceptance.lookup_value()")
    denied(cluster, "acceptance_app", "CREATE TABLE public.unexpected(id int)")
    # Isolated recovery exercise: reconstruct prior owner/ACL/path exactly, then re-harden.
    cluster.psql("""
ALTER FUNCTION acceptance.lookup_value() OWNER TO postgres;
ALTER FUNCTION acceptance.lookup_value() SET search_path TO public, acceptance, pg_temp;
REVOKE EXECUTE ON FUNCTION acceptance.lookup_value() FROM acceptance_app;
GRANT EXECUTE ON FUNCTION acceptance.lookup_value() TO PUBLIC;
GRANT CREATE ON SCHEMA public TO PUBLIC;
""")
    restored = cluster.value("""SELECT json_build_object('owner',proowner,'acl',proacl,'config',proconfig)::text
                               FROM pg_proc WHERE oid='acceptance.lookup_value()'::regprocedure""")
    # NULL ACL and its explicit built-in equivalent have the same effective privileges.
    assert cluster.value("""SELECT proowner='postgres'::regrole AND
      proconfig=ARRAY['search_path=public, acceptance, pg_temp'] AND
      has_function_privilege('acceptance_other','acceptance.lookup_value()','EXECUTE')
      FROM pg_proc WHERE oid='acceptance.lookup_value()'::regprocedure""") == "t", (original, restored)
    assert login(cluster, "acceptance_app", "SELECT acceptance.lookup_value()") == "shadow"
    cluster.psql("""
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER FUNCTION acceptance.lookup_value() OWNER TO acceptance_owner;
ALTER FUNCTION acceptance.lookup_value() SET search_path TO acceptance, pg_temp;
REVOKE EXECUTE ON FUNCTION acceptance.lookup_value() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION acceptance.lookup_value() TO acceptance_app;
""")
    assert login(cluster, "acceptance_app", "SELECT acceptance.lookup_value()") == "trusted"
    print("Legacy public schema ACL, definer shadowing, safe public routine and scoped recovery passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pgskills-access-acceptance-") as temporary:
        cluster = Cluster(args.image, Path(temporary))
        try:
            cluster.start()
            print(cluster.value("SELECT version()"), flush=True)
            role_checks(cluster)
            tenant_checks(cluster)
            definer_checks(cluster)
        finally:
            cluster.stop()


if __name__ == "__main__":
    main()
