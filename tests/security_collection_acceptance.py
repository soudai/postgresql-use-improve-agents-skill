#!/usr/bin/env python3
"""Exercise issue #23 collection bounds, source settings and secret containment."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from scenarios import Cluster
from security_scenarios import snapshot, make_report, render, CANARY

SETUP = r"""
CREATE ROLE limited_observer LOGIN;
CREATE ROLE inventory_credential LOGIN;
CREATE SCHEMA untrusted;
GRANT USAGE ON SCHEMA untrusted TO limited_observer;
CREATE FUNCTION untrusted.forbidden_eq(text,text) RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'application_operator_must_not_execute'; END $$;
CREATE OPERATOR untrusted.= (FUNCTION = untrusted.forbidden_eq, LEFTARG=text, RIGHTARG=text);
ALTER ROLE limited_observer SET search_path=untrusted,pg_catalog;
ALTER ROLE limited_observer SET statement_timeout='7s';
ALTER ROLE limited_observer SET lock_timeout='2s';
ALTER ROLE limited_observer SET TimeZone='Asia/Tokyo';
ALTER ROLE limited_observer SET password_encryption='md5';
CREATE DATABASE uncollected_database;
"""


def exercise(image):
    with tempfile.TemporaryDirectory(prefix="pgskills-security-collection-") as scratch:
        cluster = Cluster(image, Path(scratch))
        try:
            cluster.start()
            cluster.psql(SETUP)
            cluster.psql("ALTER ROLE inventory_credential PASSWORD '" + CANARY + "';")
            limited = snapshot(cluster, "limited_observer", False)
            observed = {row["name"]: row for row in limited["collection_settings"]}
            for key, value in (("statement_timeout", "7000"), ("lock_timeout", "2000"),
                               ("search_path", "untrusted, pg_catalog"), ("TimeZone", "Asia/Tokyo")):
                assert observed[key]["setting"] == value, observed
                assert observed[key]["source"] == "user", observed[key]
            encryption = next(r for r in limited["sections"]["settings"]["rows"]
                              if r["name"] == "password_encryption")
            assert encryption["setting"] == "md5" and encryption["source"] == "user", encryption
            assert all(s["status"] == "ok" for name, s in limited["sections"].items()
                       if name not in {"hba", "passwords"}), limited["sections"]
            assert limited["sections"]["passwords"]["status"] == "not_collected"
            restricted = snapshot(cluster, "limited_observer", True)
            assert restricted["sections"]["passwords"]["reason"] == "sqlstate_42501"
            assert restricted["sections"]["hba"]["reason"] == "sqlstate_42501"
            truncated = snapshot(cluster, "limited_observer", False, limit=1)
            assert truncated["sections"]["roles"]["status"] == "truncated"
            report = make_report(limited, {}, datetime.now(timezone.utc))
            assert any(f["rule"] == "COVERAGE" and f["subject"] == "other databases"
                       and f["status"] == "unknown" for f in report["findings"])
            assert not any(f["status"] == "pass" and f["rule"] in {"PASSWORD", "HBA"}
                           for f in report["findings"])
            for format_name in ("json", "markdown", "issue"):
                text = render(report, format_name)
                assert CANARY not in text
                assert "application_operator_must_not_execute" not in text
            # The collector's local overrides did not alter the login defaults.
            assert cluster.value("SELECT rolconfig::text FROM pg_roles WHERE rolname='limited_observer'").find("statement_timeout=7s") >= 0
            print("Collection/source/bounds/canary checks passed: " + cluster.value("SELECT version()"), flush=True)
        finally:
            cluster.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    exercise(args.image)


if __name__ == "__main__":
    main()
