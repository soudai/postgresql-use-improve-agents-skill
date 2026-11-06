"""Acceptance cases for partial role capability evidence."""
import unittest

from test_security import TODAY, contract, findings, make_report, snapshot

FLAGS = ("superuser", "bypassrls", "createrole", "createdb", "replication")


def role_snapshot(**flags):
    s = snapshot()
    s["sections"] = {
        "roles": {"status": "ok", "rows": [{"oid": 1, "name": "reader", "login": True, **flags}]},
        "memberships": {"status": "ok", "rows": []},
        "acl": {"status": "ok", "rows": []},
    }
    c = {**contract(s), "runtime_roles": {"reader": {}}}
    return s, c


class RoleEvidenceAcceptanceTests(unittest.TestCase):
    def test_missing_capability_attributes_are_unknown(self):
        s, c = role_snapshot()
        self.assertEqual(findings(make_report(s, c, TODAY), "ROLE", "reader")[0]["status"], "unknown")

    def test_explicit_false_capabilities_establish_limited_pass(self):
        s, c = role_snapshot(**dict.fromkeys(FLAGS, False))
        self.assertEqual(findings(make_report(s, c, TODAY), "ROLE", "reader")[0]["status"], "pass")

    def test_observed_danger_is_retained_despite_other_missing_flags(self):
        s, c = role_snapshot(superuser=True)
        result = findings(make_report(s, c, TODAY), "ROLE", "reader")[0]
        self.assertEqual(result["status"], "finding")
        self.assertIn("unverified_attributes", result["evidence"])
        self.assertTrue(result["evidence"]["unverified_attributes"])

    def test_switchable_role_missing_flags_cannot_be_a_pass(self):
        s, c = role_snapshot(**dict.fromkeys(FLAGS, False))
        s["sections"]["roles"]["rows"].append({"oid": 2, "name": "target"})
        s["sections"]["memberships"]["rows"] = [{
            "member": 1, "role": 2, "grantor": 1, "inherit_option": False,
            "set_option": True, "admin_option": False}]
        self.assertEqual(findings(make_report(s, c, TODAY), "ROLE", "reader")[0]["status"], "unknown")

    def test_inherit_only_role_does_not_inherit_special_attributes(self):
        s, c = role_snapshot(**dict.fromkeys(FLAGS, False))
        s["sections"]["roles"]["rows"].append({"oid": 2, "name": "ordinary", "superuser": True})
        s["sections"]["memberships"]["rows"] = [{
            "member": 1, "role": 2, "grantor": 1, "inherit_option": True,
            "set_option": False, "admin_option": False}]
        self.assertEqual(findings(make_report(s, c, TODAY), "ROLE", "reader")[0]["status"], "pass")

    def test_absent_switchable_role_row_is_unknown(self):
        s, c = role_snapshot(**dict.fromkeys(FLAGS, False))
        s["sections"]["memberships"]["rows"] = [{
            "member": 1, "role": 2, "grantor": 1, "inherit_option": False,
            "set_option": True, "admin_option": False}]
        self.assertEqual(findings(make_report(s, c, TODAY), "ROLE", "reader")[0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
