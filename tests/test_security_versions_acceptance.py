"""Issue #30 acceptance: trust boundaries for release and component evidence."""
import unittest
from datetime import timedelta

from test_security import NOW, TODAY, contract, findings, make_report, observed, snapshot
from advisories import parse


def feed():
    release = """<table>
<tr><td>18</td><td>18.6</td><td>Yes</td><td>September 25, 2025</td><td>November 14, 2030</td></tr>
<tr><td>14</td><td>14.24</td><td>Yes</td><td>September 30, 2021</td><td>November 12, 2026</td></tr>
<tr><td>9.6</td><td>9.6.24</td><td>No</td><td>September 29, 2016</td><td>November 11, 2021</td></tr>
</table>"""
    security = """<table>
<tr><td>CVE-2000-1000</td><td>18,14</td><td>18.6, 14.24</td><td>core server</td><td>synthetic server exposure</td></tr>
<tr><td>CVE-2000-2000</td><td>18</td><td>18.6</td><td>client</td><td>synthetic client exposure</td></tr>
<tr><td>CVE-2000-3000</td><td>18</td><td>18.6</td><td>contrib module</td><td>synthetic extension exposure</td></tr>
</table>"""
    return parse(release, security, NOW)


def target(version="18.5", version_num=180005, distribution="upstream"):
    snap = snapshot()
    snap["identity"].update(version=version, version_num=version_num)
    plan = contract(snap)
    plan["distribution"] = {**observed(snap), "type": distribution}
    return snap, plan


class VersionAcceptance(unittest.TestCase):
    def test_missing_or_tampered_sources_never_establish_a_current_release(self):
        snap, plan = target("18.6", 180006)
        cases = [None]
        for key, value in (("urls", []), ("urls", ["https://example.invalid/security"]),
                           ("source", "mirror"), ("advisories", []), ("schema_version", 2)):
            item = feed()
            item[key] = value
            cases.append(item)
        for evidence in cases:
            with self.subTest(evidence=evidence):
                report = make_report(snap, plan, TODAY, evidence)
                self.assertEqual(findings(report, "RELEASE", "server")[0]["status"], "unknown")
                self.assertFalse(any(f["status"] == "pass" for f in findings(report, "CVE")))

    def test_stale_and_future_feeds_do_not_claim_current_security_evidence(self):
        snap, plan = target("18.6", 180006)
        for offset in (-8, 1):
            evidence = feed()
            evidence["retrieved_at"] = (NOW + timedelta(days=offset)).isoformat()
            report = make_report(snap, plan, TODAY, evidence)
            self.assertEqual(findings(report, "RELEASE", "server")[0]["status"], "unknown")

    def test_unrecognized_or_inconsistent_runtime_version_is_unknown(self):
        for version, number in (("AcmeDB 18", 180006), ("18.5", 180006), ("18.99", 180099)):
            with self.subTest(version=version):
                snap, plan = target(version, number)
                report = make_report(snap, plan, TODAY, feed())
                self.assertEqual(findings(report, "RELEASE", "server")[0]["status"], "unknown")

    def test_backported_vendor_build_and_upstream_minor_are_independent(self):
        snap, plan = target(distribution="vendor")
        plan["distribution"].update(build="18.5-vendor.7", cves={
            "CVE-2000-1000": {"state": "fixed", "source": "https://example.invalid/vendor/advisory"}})
        report = make_report(snap, plan, TODAY, feed())
        self.assertEqual(findings(report, "RELEASE", "server")[0]["status"], "finding")
        self.assertEqual(findings(report, "CVE", "CVE-2000-1000")[0]["status"], "pass")
        plan["distribution"].pop("build")
        self.assertEqual(findings(make_report(snap, plan, TODAY, feed()), "CVE", "CVE-2000-1000")[0]["status"], "unknown")

    def test_new_upstream_fixed_version_explicitly_resolves_a_previous_cve(self):
        snap, plan = target(distribution="vendor")
        plan["distribution"].update(build="18.5-vendor.1", cves={
            "CVE-2000-1000": {"state": "affected", "source": "https://example.invalid/vendor/advisory"}})
        before = make_report(snap, plan, TODAY, feed())
        old = findings(before, "CVE", "CVE-2000-1000")[0]
        fixed, fixed_plan = target("18.6", 180006)
        after = make_report(fixed, fixed_plan, TODAY, feed(), previous=before)
        self.assertIn(old["id"], after["comparison"]["resolved"])
        proof = findings(after, "CVE", "CVE-2000-1000")[0]
        self.assertEqual(proof["evidence"]["upstream_fixed_minor"], 6)
        self.assertIn("retrieved_at", proof["evidence"])
        self.assertIn("advisory", proof["evidence"])

    def test_server_feed_does_not_apply_client_or_extension_cves_to_server(self):
        snap, plan = target()
        report = make_report(snap, plan, TODAY, feed())
        cves = {f["subject"] for f in findings(report, "CVE")}
        self.assertIn("CVE-2000-1000", cves)
        self.assertNotIn("CVE-2000-2000", cves)
        self.assertNotIn("CVE-2000-3000", cves)
        self.assertEqual(findings(report, "COMPONENT", "clients and poolers")[0]["status"], "unknown")

    def test_community_eol_and_provider_support_are_separate(self):
        snap, plan = target("14.24", 140024, "managed")
        plan["distribution"]["extended_support"] = {
            "active": True, "source": "https://example.invalid/provider/support"}
        report = make_report(snap, plan, TODAY, feed())
        self.assertEqual(findings(report, "LIFECYCLE", "community")[0]["status"], "finding")
        self.assertEqual(findings(report, "LIFECYCLE", "provider")[0]["status"], "pass")
        self.assertGreater(findings(report, "LIFECYCLE", "community")[0]["evidence"]["days_remaining"], 0)
        snap, plan = target("9.6.24", 90624)
        report = make_report(snap, plan, TODAY, feed())
        self.assertLess(findings(report, "LIFECYCLE", "community")[0]["evidence"]["days_remaining"], 0)

    def test_preview_is_not_a_proposed_final_upgrade(self):
        for version in ("19rc1", "19beta2", "19devel"):
            snap, plan = target(version, 190000)
            report = make_report(snap, plan, TODAY, feed())
            result = findings(report, "RELEASE", "server")[0]
            self.assertEqual(result["status"], "finding")
            self.assertEqual(result["evidence"]["stage"], "preview")

    def test_unreviewed_database_extensions_and_clients_stay_unknown(self):
        snap, plan = target("18.6", 180006)
        snap["sections"]["extensions"] = {"status": "ok", "rows": [
            {"name": "ext_a", "version": "1.0"}, {"name": "ext_b", "version": "2.0"}]}
        snap["sections"]["databases"] = {"status": "ok", "rows": [{"name": "sample"}, {"name": "other_db"}]}
        plan["components"] = [{**observed(snap), "name": "ext_a", "version": "1.0", "type": "extension",
                               "state": "fixed", "source": "https://example.invalid/extension/advisory"}]
        report = make_report(snap, plan, TODAY, feed())
        self.assertEqual(findings(report, "COMPONENT", "ext_a")[0]["status"], "pass")
        self.assertEqual(findings(report, "COMPONENT", "ext_b")[0]["status"], "unknown")
        self.assertEqual(findings(report, "COMPONENT", "clients and poolers")[0]["status"], "unknown")
        self.assertEqual(findings(report, "COVERAGE", "other databases")[0]["status"], "unknown")
        plan["components"][0]["version"] = "1.1"
        self.assertEqual(findings(make_report(snap, plan, TODAY, feed()), "COMPONENT", "ext_a")[0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()

