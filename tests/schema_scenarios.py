#!/usr/bin/env python3
"""Exercise schema review against a disposable local PostgreSQL 18/19 container."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile

from scenarios import Cluster, ROOT, run, wait_until

SKILL = ROOT / "skills" / "postgres-schema-review"
COLLECT = SKILL / "scripts" / "collect.py"
REVIEW = SKILL / "scripts" / "review.py"
CANARY = "schema-review-literal-canary-7f240f"


def now():
    return datetime.now(timezone.utc).isoformat()


def collect(cluster, directory, name, *options):
    path = directory / (name + ".json")
    run([sys.executable, str(COLLECT), "--docker-container", cluster.name,
         "--schema", "public", "--output", str(path), *options])
    text = path.read_text(encoding="utf-8")
    assert CANARY not in text, "collector leaked a row value or expression"
    return json.loads(text), path


def review(snapshot, contract, evidence, directory, name):
    contract_path, evidence_path = directory / (name + "-contract.json"), directory / (name + "-evidence.json")
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    path = directory / (name + "-review.json")
    command = [sys.executable, str(REVIEW), str(snapshot), "--contract", str(contract_path),
               "--evidence", str(evidence_path), "--format", "json", "--output", str(path)]
    run(command)
    text = path.read_text(encoding="utf-8")
    assert CANARY not in text
    markdown = directory / (name + "-review.md")
    command[-3:] = ["markdown", "--output", str(markdown)]
    run(command)
    assert markdown.read_text(encoding="utf-8").strip()
    return json.loads(text)


def findings(report, relation, rule=None, status=None):
    return [f for f in report["findings"] if f["relation"] == relation
            and (rule is None or f["rule"] == rule) and (status is None or f["status"] == status)]


def setup(cluster):
    cluster.psql("""
CREATE TABLE customers (tenant_id int NOT NULL, id int NOT NULL UNIQUE, PRIMARY KEY (tenant_id, id));
INSERT INTO customers VALUES (1, 1);
CREATE TABLE orders_missing (id int PRIMARY KEY, tenant_id int NOT NULL, customer_id int NOT NULL);
INSERT INTO orders_missing VALUES (1, 1, 1);
CREATE TABLE orders_wrong (id int PRIMARY KEY, tenant_id int NOT NULL,
                           customer_id int NOT NULL REFERENCES customers(id));
INSERT INTO orders_wrong VALUES (1, 1, 1);
CREATE TABLE orders_unvalidated (id int PRIMARY KEY, tenant_id int NOT NULL, customer_id int NOT NULL);
INSERT INTO orders_unvalidated VALUES (1, 1, 1);
ALTER TABLE orders_unvalidated ADD CONSTRAINT expected_fk
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id) NOT VALID;
CREATE TABLE full_link (id int PRIMARY KEY, tenant_id int NOT NULL, customer_id int,
    FOREIGN KEY (tenant_id,customer_id) REFERENCES customers(tenant_id,id) MATCH FULL);
INSERT INTO full_link VALUES (1,1,1);
CREATE TABLE nullable_link (id int PRIMARY KEY, customer_id int REFERENCES customers(id));
INSERT INTO nullable_link VALUES (1, NULL);
CREATE TABLE nullable_check (id int PRIMARY KEY, amount int CHECK (amount > 0));
INSERT INTO nullable_check VALUES (1, NULL);
CREATE TABLE flags_ok (id int PRIMARY KEY, is_active boolean NOT NULL, is_featured boolean NOT NULL);
INSERT INTO flags_ok VALUES (1, true, false);
CREATE TABLE "Order.Items" ("Item.ID" int PRIMARY KEY, "User.ID" int);
CREATE DOMAIN required_integer AS integer NOT NULL;
CREATE TABLE domain_values (id int PRIMARY KEY, required_value required_integer);
CREATE DOMAIN checked_integer AS integer CHECK(VALUE IS NOT NULL);
CREATE DOMAIN nested_checked_integer AS checked_integer;
CREATE TABLE checked_domain_values (id int PRIMARY KEY, required_value nested_checked_integer);
INSERT INTO checked_domain_values VALUES (1,7);
INSERT INTO domain_values VALUES (1, 7);
-- A typed NULL bypasses a domain conversion check, unlike a column constraint.
INSERT INTO domain_values VALUES (2, (SELECT required_value FROM domain_values WHERE false));
CREATE TABLE required_unvalidated (id int PRIMARY KEY, required_value int);
INSERT INTO required_unvalidated VALUES (1, NULL);
ALTER TABLE required_unvalidated ADD CONSTRAINT required_value_nn NOT NULL required_value NOT VALID;
CREATE TABLE external_refs (id int PRIMARY KEY, external_customer_id text);
INSERT INTO external_refs VALUES (1, NULL);
CREATE TABLE expression_values (id int PRIMARY KEY, marker text DEFAULT '""" + CANARY + """',
                                CHECK (marker IS DISTINCT FROM '""" + CANARY + """'));
INSERT INTO expression_values VALUES (1, 'ordinary');
CREATE INDEX expression_idx ON expression_values ((length('""" + CANARY + """' || marker)));
ANALYZE;
CREATE ROLE schema_observer LOGIN;
""")


def relation_contract(relation):
    return {"relation": relation, "source": "integration/schema-contract", "kind": "entity",
            "relationships": [{"columns": ["tenant_id", "customer_id"],
                               "referenced_relation": "public.customers",
                               "referenced_columns": ["tenant_id", "id"],
                               "enforce": True, "allow_null": False, "match_type": "s",
                               "delete_action": "a", "update_action": "a",
                               "source": "integration/customer-tenant-invariant"}]}


def catalog_and_constraints(cluster, directory):
    snapshot, path = collect(cluster, directory, "before")
    rows = lambda name: snapshot["sections"][name]["rows"]
    assert any(r["relation"] == 'public."Order.Items"' for r in rows("tables")), rows("tables")
    assert any(r["relation"] == 'public."Order.Items"' and r["name"] == "User.ID" for r in rows("columns"))
    assert any(r["relation"] == "public.domain_values" and r["name"] == "required_value" and r["domain_not_null"] and not r["column_not_null"]
               for r in rows("columns"))
    assert any(r["relation"] == "public.orders_unvalidated" and r["type"] == "f"
               and not r["validated"] and r["enforced"] for r in rows("constraints"))
    assert snapshot["schema_version"] == 2
    assert any(r["relation"] == "public.required_unvalidated" and r["type"] == "n"
               and r["columns"] == ["required_value"] and not r["validated"] and r["enforced"]
               for r in rows("constraints"))
    assert cluster.value("SELECT count(*) FROM domain_values WHERE required_value IS NULL") == "1"
    assert cluster.value("SELECT count(*) FROM required_unvalidated WHERE required_value IS NULL") == "1"
    for statement in (
        "INSERT INTO domain_values VALUES (3, NULL);",
        "INSERT INTO required_unvalidated VALUES (2, NULL);",
        "ALTER TABLE required_unvalidated VALIDATE CONSTRAINT required_value_nn;"):
        denied = run(["docker", "exec", "-i", cluster.name, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1"],
                     sql=statement, check=False)
        assert denied.returncode != 0, statement
    tables = [relation_contract("public." + name)
              for name in ("orders_missing", "orders_wrong", "orders_unvalidated")]
    full_contract = relation_contract("public.full_link")
    full_contract["relationships"][0]["match_type"] = "f"
    full_contract["required_columns"] = ["tenant_id","customer_id"]
    tables.append(full_contract)
    for values in ("(2,1,NULL)", "(2,NULL,NULL)"):
        rejected = run(["docker","exec","-i",cluster.name,"psql","-XqAt","-v","ON_ERROR_STOP=1"],
                       sql="INSERT INTO full_link VALUES "+values+";", check=False)
        assert rejected.returncode != 0, "MATCH FULL/NOT NULL failed to reject NULL reference"
    tables.extend([
        {"relation": "public.nullable_link", "source": "integration/optional-customer",
         "relationships": [{"columns": ["customer_id"], "referenced_relation": "public.customers",
                             "referenced_columns": ["id"], "enforce": True, "allow_null": True,
                             "source": "integration/optional-customer"}]},
        {"relation": "public.nullable_check", "source": "integration/amount-required",
         "required_columns": ["amount"]},
        {"relation": "public.flags_ok", "source": "integration/independent-flags",
         "flags": [{"columns": ["is_active"], "meaning": "boolean"},
                   {"columns": ["is_featured"], "meaning": "boolean"}]},
        {"relation": "public.domain_values", "source": "integration/required-domain",
         "required_columns": ["required_value"]},
        {"relation": "public.required_unvalidated", "source": "integration/required-unvalidated",
         "required_columns": ["required_value"]},
        {"relation": "public.external_refs", "source": "integration/external-id-required",
         "relationships": [{"columns": ["external_customer_id"], "enforce": False, "allow_null": False}]},
        {"relation": "public.checked_domain_values", "source": "integration/required-domain-check",
         "required_columns": ["required_value"]},
    ])
    contract = {"schema_version": 1, "tables": tables}
    evidence = {"schema_version": 1, "profiles": [{
        "relation": "public.nullable_check", "kind": "null", "columns": ["amount"],
        "violations": int(cluster.value("SELECT count(*) FROM nullable_check WHERE amount IS NULL")),
        "rows_checked": int(cluster.value("SELECT count(*) FROM nullable_check")),
        "scope": "all synthetic nullable_check rows", "complete": True,
        "source": "integration/required-amount-profile", "observed_at": now()}]}
    for table, column in (("domain_values", "required_value"),
                          ("required_unvalidated", "required_value"),
                          ("external_refs", "external_customer_id")):
        evidence["profiles"].append({
            "relation": "public." + table, "kind": "null", "columns": [column],
            "violations": int(cluster.value(f"SELECT count(*) FROM {table} WHERE {column} IS NULL")),
            "rows_checked": int(cluster.value(f"SELECT count(*) FROM {table}")),
            "scope": "all synthetic rows in public." + table, "complete": True,
            "source": "integration/null-profile/" + table, "observed_at": now()})
    report = review(path, contract, evidence, directory, "before")
    for relation in ("orders_missing", "orders_wrong", "orders_unvalidated"):
        assert findings(report, "public." + relation, "schema.fk", "confirmed"), report
    assert findings(report, "public.nullable_check", "schema.null", "confirmed"), report
    assert not findings(report, "public.nullable_link", "schema.fk"), report
    assert not findings(report, "public.flags_ok", "schema.flag"), report
    for table in ("domain_values", "required_unvalidated", "external_refs"):
        null_findings = findings(report, "public." + table, "schema.null")
        assert len(null_findings) == 2 and all(f["status"] == "confirmed" for f in null_findings), report
        assert any("集計" in f["summary"] for f in null_findings), report
    assert any("未検証" in f["summary"]
               for f in findings(report, "public.required_unvalidated", "schema.null")), report
    assert findings(report, "public.external_refs", "schema.fk", "exception"), report
    assert findings(report, "public.checked_domain_values", "schema.null", "candidate"), report
    assert not findings(report, "public.checked_domain_values", "schema.null", "confirmed"), report
    rejected_domain = run(["docker","exec","-i",cluster.name,"psql","-XqAt","-v","ON_ERROR_STOP=1"],
                          sql="INSERT INTO checked_domain_values VALUES (2,NULL);",check=False)
    assert rejected_domain.returncode != 0
    assert not findings(report, "public.full_link", "schema.fk"), report
    assert not findings(report, "public.full_link", "schema.null"), report

    cluster.psql("""
ALTER TABLE orders_missing ADD CONSTRAINT expected_fk
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id) NOT VALID;
ALTER TABLE orders_wrong ADD CONSTRAINT expected_fk
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id) NOT VALID;
""")
    intermediate, intermediate_path = collect(cluster, directory, "not-valid")
    intermediate_report = review(intermediate_path, contract, evidence, directory, "not-valid")
    assert findings(intermediate_report, "public.orders_missing", "schema.fk"), intermediate_report
    cluster.psql("""
ALTER TABLE orders_missing VALIDATE CONSTRAINT expected_fk;
ALTER TABLE orders_wrong VALIDATE CONSTRAINT expected_fk;
ALTER TABLE orders_unvalidated VALIDATE CONSTRAINT expected_fk;
UPDATE nullable_check SET amount = 1 WHERE amount IS NULL;
ALTER TABLE nullable_check ALTER COLUMN amount SET NOT NULL;
UPDATE domain_values SET required_value = 7 WHERE required_value IS NULL;
ALTER TABLE domain_values ALTER COLUMN required_value SET NOT NULL;
UPDATE required_unvalidated SET required_value = 7 WHERE required_value IS NULL;
ALTER TABLE required_unvalidated VALIDATE CONSTRAINT required_value_nn;
UPDATE external_refs SET external_customer_id = 'customer-1' WHERE external_customer_id IS NULL;
ALTER TABLE external_refs ALTER COLUMN external_customer_id SET NOT NULL;
""")
    # Check the tenant mismatch is rejected by the added composite constraint.
    denied = run(["docker", "exec", "-i", cluster.name, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1"],
                 sql="INSERT INTO orders_missing VALUES (2, 2, 1);", check=False)
    assert denied.returncode != 0
    assert cluster.value("SELECT count(*) FROM orders_missing") == "1"
    _, repaired_path = collect(cluster, directory, "repaired")
    repaired_evidence = {"schema_version": 1}
    repaired = review(repaired_path, contract, repaired_evidence, directory, "repaired")
    for relation in ("orders_missing", "orders_wrong", "orders_unvalidated"):
        assert not findings(repaired, "public." + relation, "schema.fk"), repaired
    assert not findings(repaired, "public.nullable_check", "schema.null"), repaired
    for table in ("domain_values", "required_unvalidated", "external_refs"):
        assert not findings(repaired, "public." + table, "schema.null"), repaired
    assert findings(repaired, "public.external_refs", "schema.fk", "exception"), repaired
    denied_typed_null = run(
        ["docker", "exec", "-i", cluster.name, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1"],
        sql="INSERT INTO domain_values VALUES (3,(SELECT required_value FROM domain_values WHERE false));",
        check=False)
    assert denied_typed_null.returncode != 0
    print("Catalog, quoted names, domain NULL, NOT VALID, external requiredness and FK repair passed",
          flush=True)
    return contract


def bounded_and_low_privilege(cluster, directory, contract):
    bounded, path = collect(cluster, directory, "bounded", "--row-limit", "1")
    assert any(s.get("truncated") for s in bounded["sections"].values()), bounded
    report = review(path, contract, {"schema_version": 1}, directory, "bounded")
    assert report["coverage"]
    # With constraints cut short, an unseen FK must not become a confirmed omission.
    assert not [f for f in report["findings"] if f["rule"] == "schema.fk" and f["status"] == "confirmed"], report

    wrapper = "/usr/local/bin/schema-observer-psql"
    run(["docker", "exec", "-i", "-u", "root", cluster.name, "sh", "-c",
         "cat > " + wrapper + " && chmod 755 " + wrapper],
        sql='#!/bin/sh\nexec psql -U schema_observer "$@"\n')
    snapshot, low_path = collect(cluster, directory, "observer", "--psql", wrapper)
    assert snapshot["metadata"].get("session_role", snapshot["metadata"].get("effective_role")) == "schema_observer"
    stats = snapshot["sections"]["statistics"]
    assert not [r for r in stats.get("rows", []) if r["relation"] == "public.nullable_check"], stats
    low = review(low_path, contract, {"schema_version": 1}, directory, "observer")
    assert low["coverage"]
    print("Collection bounds and unprivileged statistics coverage passed", flush=True)


def contention(cluster, directory):
    cluster.psql("""
CREATE TABLE mixed_orders (id int PRIMARY KEY, paid_at timestamptz, shipped_at timestamptz);
INSERT INTO mixed_orders VALUES (1, NULL, NULL);
CREATE TABLE order_payments (order_id int PRIMARY KEY, paid_at timestamptz);
CREATE TABLE order_shipments (order_id int PRIMARY KEY, shipped_at timestamptz);
INSERT INTO order_payments VALUES (1, NULL);
INSERT INTO order_shipments VALUES (1, NULL);
""")
    payment, shipment = cluster.session("schema_payment"), cluster.session("schema_shipment")
    try:
        payment.send("BEGIN; UPDATE mixed_orders SET paid_at = clock_timestamp() WHERE id = 1;")
        shipment.send("BEGIN; UPDATE mixed_orders SET shipped_at = clock_timestamp() WHERE id = 1;")
        pay_pid, ship_pid = payment.pid(), shipment.pid()
        blocking_sql = f"SELECT {pay_pid} = ANY(pg_blocking_pids({ship_pid}))"
        assert wait_until(lambda: cluster.value(blocking_sql) == "t", timeout=10)
        observed_at = now()
        wait_ms = float(cluster.value(f"SELECT extract(epoch FROM clock_timestamp()-query_start)*1000 "
                                      f"FROM pg_stat_activity WHERE pid={ship_pid}"))
        assert wait_ms > 0
        _, path = collect(cluster, directory, "contention")
        contract = {"schema_version": 1, "tables": [{
            "relation": "public.mixed_orders", "source": "integration/order-lifecycle", "kind": "entity",
            "column_groups": [
                {"name": "payment", "columns": ["paid_at"], "lifecycle": "payment",
                 "writers": ["payment"], "created_by": "payment accepted",
                 "finalized_by": "payment confirmed", "retention": "7 years"},
                {"name": "shipment", "columns": ["shipped_at"], "lifecycle": "shipment",
                 "writers": ["shipment"], "created_by": "shipment accepted",
                 "finalized_by": "shipment confirmed", "retention": "3 years"}]}]}
        evidence = {"schema_version": 1, "writes": [
            {"relation": "public.mixed_orders", "operation": "UPDATE", "columns": [column],
             "business_operation": operation, "after_finalization": False,
             "source": "integration/session-" + operation, "observed_at": observed_at}
            for operation, column in (("payment", "paid_at"), ("shipment", "shipped_at"))],
            "contention": [{"relation": "public.mixed_orders", "operations": ["payment", "shipment"],
                           "same_key": True, "wait_ms": wait_ms, "blocker_chain": True,
                           "transaction_boundary": True, "source": "integration/two-session-wait",
                           "observed_at": observed_at}]}
        report = review(path, contract, evidence, directory, "contention")
        assert findings(report, "public.mixed_orders", "schema.contention", "confirmed"), report
        payment.send("COMMIT;")
        assert wait_until(lambda: cluster.value(blocking_sql) == "f", timeout=10)
        shipment.send("COMMIT;")

        payment.send("BEGIN; UPDATE order_payments SET paid_at = clock_timestamp() WHERE order_id = 1;")
        shipment.send("BEGIN; UPDATE order_shipments SET shipped_at = clock_timestamp() WHERE order_id = 1;")
        assert wait_until(lambda: cluster.value(
            f"SELECT state='idle in transaction' FROM pg_stat_activity WHERE pid={ship_pid}") == "t", timeout=10)
        assert cluster.value(blocking_sql) == "f"
        payment.send("COMMIT;")
        shipment.send("COMMIT;")
        assert cluster.value("SELECT paid_at IS NOT NULL FROM order_payments WHERE order_id=1") == "t"
        assert cluster.value("SELECT shipped_at IS NOT NULL FROM order_shipments WHERE order_id=1") == "t"
        print("Observed same-row contention and independent updates after splitting passed", flush=True)
    finally:
        payment.send("ROLLBACK;", pause=0)
        shipment.send("ROLLBACK;", pause=0)
        payment.close()
        shipment.close()



def inheritance_scope(cluster, directory):
    cluster.psql("""
CREATE TABLE inherited_nn(value int NOT NULL NO INHERIT);
CREATE TABLE inherited_nn_child() INHERITS(inherited_nn);
INSERT INTO inherited_nn_child VALUES(NULL);
CREATE TABLE inherited_pk(value int PRIMARY KEY);
CREATE TABLE inherited_pk_child() INHERITS(inherited_pk);
INSERT INTO inherited_pk VALUES(1);
INSERT INTO inherited_pk_child VALUES(1);
CREATE TABLE inherited_full(a int NOT NULL, b int,
    PRIMARY KEY(a), FOREIGN KEY(a,b) REFERENCES customers(tenant_id,id) MATCH FULL);
CREATE TABLE inherited_full_child() INHERITS(inherited_full);
INSERT INTO inherited_full_child VALUES(1,NULL);
""")
    snapshot, path = collect(cluster, directory, "inheritance")
    for name in ("inherited_nn", "inherited_pk", "inherited_full"):
        assert any(r["relation"] == "public." + name and r["has_inheritance_children"]
                   for r in snapshot["sections"]["tables"]["rows"])
    assert any(r["relation"] == "public.inherited_nn" and r["type"] == "n" and r["no_inherit"]
               for r in snapshot["sections"]["constraints"]["rows"])
    # PK uniqueness is parent-local, while its column NOT NULL still propagates.
    assert cluster.value("SELECT count(*) FROM inherited_pk WHERE value=1") == "2"
    rejected = run(["docker", "exec", "-i", cluster.name, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1"],
                   sql="INSERT INTO inherited_pk_child VALUES(NULL)", check=False)
    assert rejected.returncode != 0
    contract = {"schema_version": 1, "tables": []}
    evidence = {"schema_version": 1, "profiles": []}
    for table, column in (("inherited_nn", "value"), ("inherited_full", "b")):
        assert cluster.value(f"SELECT count(*) FROM ONLY {table} WHERE {column} IS NULL") == "0"
        assert cluster.value(f"SELECT count(*) FROM {table} WHERE {column} IS NULL") == "1"
        contract["tables"].append({"relation": "public." + table, "required_columns": [column],
                                   "source": "integration/inherited-row-requiredness"})
        evidence["profiles"].append({
            "relation": "public." + table, "kind": "null", "columns": [column],
            "violations": 1, "rows_checked": 1, "scope": "parent SELECT including inheritance children",
            "includes_children": True, "complete": True,
            "source": "integration/parent-null-count", "observed_at": now()})
    result = review(path, contract, evidence, directory, "inheritance")
    for spec in contract["tables"]:
        found = findings(result, spec["relation"], "schema.null")
        assert any(f["status"] == "needs_evidence" and "集計" not in f["summary"] for f in found)
        assert any(f["status"] == "confirmed" and "集計" in f["summary"] for f in found)
    for profile in evidence["profiles"]:
        del profile["includes_children"]
    unknown = review(path, contract, evidence, directory, "inheritance-unknown-scope")
    assert all(f["status"] == "needs_evidence" for f in unknown["findings"])
    print("Parent-local NOT NULL/PK/MATCH FULL and inherited NULL scope passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"postgres:(?:18|19)[A-Za-z0-9_.-]*", args.image):
        parser.error("use a local postgres:18 or postgres:19 image")
    with tempfile.TemporaryDirectory(prefix="pgskills-schema-test-") as scratch:
        directory = Path(scratch)
        cluster = Cluster(args.image, directory)
        try:
            cluster.start()
            print(cluster.value("SELECT version()"), flush=True)
            setup(cluster)
            contract = catalog_and_constraints(cluster, directory)
            bounded_and_low_privilege(cluster, directory, contract)
            contention(cluster, directory)
            inheritance_scope(cluster, directory)
        finally:
            cluster.stop()


if __name__ == "__main__":
    main()
