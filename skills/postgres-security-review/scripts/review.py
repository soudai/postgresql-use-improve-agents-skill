#!/usr/bin/env python3
"""Compare a PostgreSQL security snapshot with an explicit access contract."""
import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from access import Access, definer, rls, roles
from environment import auth, components, observed_date, runtime, versions
from report import Review, render, write_new

SOURCES = {
    "ROLE": "https://www.postgresql.org/docs/current/role-membership.html",
    "OWNER": "https://www.postgresql.org/docs/current/ddl-priv.html",
    "ADMIN": "https://www.postgresql.org/docs/current/role-membership.html",
    "ACL": "https://www.postgresql.org/docs/current/ddl-priv.html",
    "DEFAULT_ACL": "https://www.postgresql.org/docs/current/sql-alterdefaultprivileges.html",
    "RLS": "https://www.postgresql.org/docs/current/ddl-rowsecurity.html",
    "RLS_BYPASS": "https://www.postgresql.org/docs/current/ddl-rowsecurity.html",
    "RLS_PATH": "https://www.postgresql.org/docs/current/sql-createview.html",
    "RLS_POLICY": "https://www.postgresql.org/docs/current/sql-createpolicy.html",
    "PUBLIC_EXECUTE": "https://www.postgresql.org/docs/current/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY",
    "DEFINER": "https://www.postgresql.org/docs/current/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY",
    "HBA": "https://www.postgresql.org/docs/current/auth-pg-hba-conf.html",
    "ACCESS_ROUTE": "https://www.postgresql.org/docs/current/auth-pg-hba-conf.html",
    "TLS": "https://www.postgresql.org/docs/current/libpq-ssl.html",
    "PASSWORD": "https://www.postgresql.org/docs/current/auth-password.html",
    "CREDENTIAL_LIFECYCLE": "https://www.postgresql.org/docs/current/passwordcheck.html",
    "RUNTIME": "https://www.postgresql.org/docs/current/server-start.html",
    "FILE_ACCESS": "https://www.postgresql.org/docs/current/ssl-tcp.html",
    "SECRET_LOCATION": "https://www.postgresql.org/docs/current/libpq-envars.html",
    "RELEASE": "https://www.postgresql.org/support/versioning/",
    "LIFECYCLE": "https://www.postgresql.org/support/versioning/",
    "CVE": "https://www.postgresql.org/support/security/",
    "COMPONENT": "https://www.postgresql.org/support/security/",
    "COVERAGE": "https://www.postgresql.org/docs/current/catalogs.html",
}


def combine(snapshot, supplement):
    if snapshot["schema_version"] != 1 or supplement["schema_version"] != 1:
        raise ValueError("unrecognized snapshot format")
    keys = {"database", "version", "version_num", "database_oid", "postmaster_started", "in_recovery",
            "endpoint", "port"}
    if snapshot["target"] != supplement["target"] or any(
        snapshot["identity"][key] != supplement["identity"][key] for key in keys
    ):
        raise ValueError("supplement target mismatch")
    first = datetime.fromisoformat(snapshot["collected_at"])
    second = datetime.fromisoformat(supplement["collected_at"])
    if abs((first - second).total_seconds()) > 3600:
        raise ValueError("supplement capture windows differ")
    sections = snapshot["sections"].copy()
    for name in ("hba", "passwords"):
        if supplement["sections"].get(name, {}).get("status") == "ok":
            sections[name] = supplement["sections"][name]
    return {**snapshot, "sections": sections,
            "supplement": {"collected_at": supplement["collected_at"],
                           "effective_role": supplement["identity"]["effective_role"]}}


def make_report(snapshot, contract, today, advisories=None, previous=None):
    if snapshot.get("schema_version") != 1 or not isinstance(snapshot["identity"]["version_num"], int):
        raise ValueError("invalid snapshot")
    if contract and (contract.get("target") != snapshot["target"] or
                     contract.get("database") != snapshot["identity"]["database"]):
        raise ValueError("contract scope mismatch")
    evaluated_at = today
    if isinstance(evaluated_at, datetime):
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("evaluation timestamp requires a timezone")
        evaluated_at = evaluated_at.astimezone(timezone.utc)
        today = evaluated_at.date()
    review = Review(snapshot, contract)
    review.today = today
    review.as_of = evaluated_at
    review.advisories = advisories
    stale = not 0 <= (today - observed_date(snapshot["collected_at"])).days <= 7
    if stale:
        review.missing("COVERAGE", "snapshot", "Fresh, same-target security evidence",
                       "Snapshot is historical or dated in the future.")
    for section, info in sorted(snapshot["sections"].items()):
        if info["status"] != "ok":
            review.missing("COVERAGE", section, "Complete section", f"Collection status: {info['status']}.")
    databases = [r["name"] for r in review.rows("databases") if r["name"] != review.scope["database"]]
    if databases:
        review.missing("COVERAGE", "other databases", "Separate review per database",
                       f"{len(databases)} other connectable databases are outside this database snapshot.")
    access = Access(review)
    for check in (roles, rls, definer, auth):
        check(review, access)
    runtime(review)
    versions(review)
    components(review)
    exceptions = {e["id"]: e for e in contract.get("exceptions", [])}
    for key, f in review.findings.items():
        f.setdefault("source", SOURCES[f["rule"]])
        f["evaluated_on"] = evaluated_at.isoformat()
        f["server_version"] = snapshot["identity"]["version"]
        if stale and f["status"] == "pass":
            f["status"] = "unknown"
            f["confidence"] = "historical"
        if key in exceptions and f["status"] == "finding":
            exception = exceptions[key]
            f["exception"] = {k: exception[k] for k in ("owner", "reason", "expires")}
            f["exception"]["active"] = date.fromisoformat(exception["expires"]) >= today
    report = {"schema_version": 1, "scope": review.scope,
              "identity": snapshot["identity"],
              "coverage": {k: v["status"] for k, v in snapshot["sections"].items()},
              "findings": sorted(review.findings.values(), key=lambda x: (x["status"], x["id"]))}
    if "supplement" in snapshot:
        report["supplement"] = snapshot["supplement"]
    if previous is not None:
        if any(previous["scope"][k] != review.scope[k] for k in ("target", "database")):
            raise ValueError("previous report scope mismatch")
        if any(previous["identity"][k] != report["identity"][k]
               for k in ("database_oid", "endpoint", "port", "in_recovery")):
            raise ValueError("previous report target identity mismatch")
        if datetime.fromisoformat(previous["scope"]["collected_at"]) > datetime.fromisoformat(review.scope["collected_at"]):
            raise ValueError("previous report is newer")
        old = {f["id"] for f in previous["findings"] if f["status"] == "finding"}
        new = {f["id"] for f in report["findings"] if f["status"] == "finding"}
        confirmed = {f["id"] for f in report["findings"] if f["status"] == "pass"}
        report["comparison"] = {"new": sorted(new - old), "persisting": sorted(new & old),
                                "resolved": sorted(old & confirmed), "unverified": sorted(old - new - confirmed)}
    return report


def evaluation_time(value):
    """Accept a UTC calendar day or an ISO 8601 instant with an explicit offset."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("evaluation timestamp requires a timezone")
        return instant.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--supplement", type=Path)
    parser.add_argument("--advisories", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--as-of", type=evaluation_time,
                        help="ISO timestamp with timezone (default: current UTC time), or UTC date")
    parser.add_argument("--format", choices=("markdown", "json", "issue"), default="markdown")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    def read(path):
        return json.loads(path.read_text(encoding="utf-8")) if path else None

    try:
        snapshot = read(args.snapshot)
        if args.supplement:
            snapshot = combine(snapshot, read(args.supplement))
        report = make_report(snapshot, read(args.contract) or {}, args.as_of or datetime.now(timezone.utc),
                             read(args.advisories), read(args.previous))
        write_new(args.output, render(report, args.format))
    except (OSError, ValueError, AttributeError, KeyError, TypeError, IndexError):
        parser.exit(2, "Invalid, incompatible or unreadable evidence, or output already exists. No report written.\n")


if __name__ == "__main__":
    main()
