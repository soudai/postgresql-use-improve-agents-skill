import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/postgres-security-review/scripts"))

from access import Access, search_path
from advisories import parse
from environment import file_rule
from report import Review, render
from review import combine, make_report

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
TODAY = NOW.date()


def snapshot():
    return {"target": "staging-private", "schema_version": 1, "collected_at": NOW.isoformat(),
            "identity": {"database": "sample", "database_oid": 41, "version": "17.9", "version_num": 170009,
                         "postmaster_started": NOW.isoformat(), "in_recovery": False, "endpoint": None, "port": None},
            "sections": {}}


def contract(s):
    return {"target": s["target"], "database": s["identity"]["database"]}


def observed(s):
    return {**contract(s), "server_version": s["identity"]["version"], "observed_at": NOW.isoformat()}


def findings(report, rule, subject=None):
    return [f for f in report["findings"] if f["rule"] == rule and (subject is None or f["subject"] == subject)]


class TestSecurity(unittest.TestCase):
    def test_postgres_membership_and_public_are_distinct_paths(self):
        s = snapshot()
        s["sections"] = {
            "roles": {"status": "ok", "rows": [
                {"oid": 1, "name": "app"}, {"oid": 2, "name": "access"}, {"oid": 3, "name": "bypass"}]},
            "memberships": {"status": "ok", "rows": [
                {"role": 2, "member": 1, "inherit_option": True, "set_option": False},
                {"role": 3, "member": 2, "inherit_option": True, "set_option": True}]},
            "acl": {"status": "ok", "rows": [
                {"kind": "relation", "schema": "app", "name": "orders", "owner": 3,
                 "grantee": 0, "grantor": 3, "is_grantable": False, "privilege": "SELECT"}]},
        }
        access = Access(Review(s, {}))
        self.assertEqual(access.walk(1, "set_option"), {1: [1]})
        self.assertEqual(access.walk(1, "inherit_option")[3], [1, 2, 3])
        self.assertTrue(access.can(["relation", "app", "orders", ""], 1, "SELECT"))

    def test_partial_tls_evidence_is_unknown(self):
        s = snapshot()
        c = contract(s)
        route = {"id": "app", "require_tls": True, "require_identity_verification": True,
                 "verification": observed(s), "verified": {"tls": True}}
        c["routes"] = [route]
        self.assertEqual(findings(make_report(s, c, TODAY), "TLS")[0]["status"], "unknown")
        route["verified"]["plaintext_accepted"] = True
        self.assertEqual(findings(make_report(s, c, TODAY), "TLS")[0]["status"], "finding")
        route["verified"].update(plaintext_accepted=False, valid_identity_accepted=True, invalid_identity_rejected=True)
        self.assertEqual(findings(make_report(s, c, TODAY), "TLS")[0]["status"], "pass")

    def test_hba_ipv6_order_and_unresolved_early_rule(self):
        route = {"transport": "tls", "address": "2001:db8::5", "database": "sample", "role": "app"}
        base = {"type": "host", "databases": ["sample"], "users": ["app"], "invalid": False, "method": "trust"}
        v4 = {**base, "address": "0.0.0.0", "netmask": "0.0.0.0"}
        v6 = {**base, "address": "2001:db8::", "netmask": "ffff:ffff::"}
        reject = {**base, "address": "all", "method": "reject"}
        self.assertEqual(file_rule([v4, v6, reject], route)[0], v6)
        self.assertEqual(file_rule([reject, v6], route)[0], reject)
        self.assertIsNone(file_rule([{**v6, "users": ["+group"]}, reject], route)[0])

    def test_version_evidence_requires_running_build_and_separates_component(self):
        s = snapshot()
        c = contract(s)
        c["distribution"] = {**observed(s), "type": "vendor", "cves": {
            "CVE-2000-1000": {"state": "fixed", "source": "https://example.invalid/advisory"}}}
        versions = "<table><tr><td>17</td><td>17.11</td><td>Yes</td><td>September 26, 2024</td><td>November 8, 2029</td></tr></table>"
        security = """<table>
        <tr><td>CVE-2000-1000</td><td>17</td><td>17.11</td><td>core server</td><td>synthetic</td></tr>
        <tr><td>CVE-2000-2000</td><td>17</td><td>17.11</td><td>client</td><td>synthetic</td></tr>
        </table>"""
        feed = parse(versions, security, NOW)
        self.assertEqual(findings(make_report(s, c, TODAY, feed), "CVE", "CVE-2000-1000")[0]["status"], "unknown")
        c["distribution"]["build"] = "17.9-vendor.12"
        result = make_report(s, c, TODAY, feed)
        finding = findings(result, "CVE", "CVE-2000-1000")[0]
        self.assertEqual(finding["status"], "pass")
        self.assertEqual(finding["evidence"]["build"], "17.9-vendor.12")
        self.assertFalse(findings(result, "CVE", "CVE-2000-2000"))
        self.assertNotIn("staging-private", render(result, "issue"))
        self.assertNotIn("17.9-vendor.12", render(result, "issue"))
        self.assertEqual(findings(make_report(s, c, date(2026, 11, 15), feed), "RELEASE")[0]["status"], "unknown")
        s["identity"].update(version="19rc1", version_num=190000)
        self.assertEqual(findings(make_report(s, c, TODAY, feed), "RELEASE")[0]["evidence"]["stage"], "preview")

    def test_supplement_rejects_different_cluster_or_database(self):
        s = snapshot()
        other = snapshot()
        other["identity"]["postmaster_started"] = "2026-11-05T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "target mismatch"):
            combine(s, other)

    def test_no_dropped_coverage_is_called_resolved(self):
        s = snapshot()
        c = contract(s)
        c["runtime"] = {**observed(s), "process_uid": 0}
        before = make_report(s, c, TODAY)
        f = findings(before, "RUNTIME", "postgres process")[0]
        self.assertEqual(f["status"], "finding")
        after = make_report(s, contract(s), TODAY, previous=before)
        self.assertIn(f["id"], after["comparison"]["unverified"])
        self.assertEqual(after["comparison"]["resolved"], [])

    def test_root_entrypoint_and_root_owned_tls_key_with_restricted_group(self):
        s = snapshot()
        c = contract(s)
        c["runtime"] = {**observed(s), "process_uid": 999, "entrypoint_uid": 0, "rootless": True,
                        "privileged": True, "files": [{"id": "key", "kind": "tls_key",
                        "platform": "unix", "mode": "0640", "owner_uid": 0,
                        "owner_gid": 999, "service_readable": True, "unauthorized_access": False}]}
        result = make_report(s, c, TODAY)
        self.assertEqual(findings(result, "RUNTIME", "postgres process")[0]["status"], "pass")
        self.assertEqual(findings(result, "RUNTIME", "privileged")[0]["status"], "finding")
        self.assertEqual(findings(result, "FILE_ACCESS", "key")[0]["status"], "pass")
        c["runtime"]["files"][0]["unauthorized_access"] = True
        self.assertEqual(findings(make_report(s, c, TODAY), "FILE_ACCESS", "key")[0]["status"], "finding")

    def test_postgresql_search_path_quoted_schema(self):
        self.assertEqual(search_path('"a,b", "Mixed""Quote", APP, pg_temp'),
                         ["a,b", 'Mixed"Quote', "app", "pg_temp"])
        self.assertIsNone(search_path("app,"))
