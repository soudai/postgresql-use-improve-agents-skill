"""Regression cases where metadata formerly implied more security than it proved."""
import unittest
from datetime import datetime, timezone

from test_security import TODAY, contract, findings, make_report, observed, snapshot


def rls_fixture():
    s = snapshot()
    rows = {
        "roles": [{"oid": 1, "name": "web"}, {"oid": 2, "name": "owner"},
                  {"oid": 3, "name": "proxy"}],
        "memberships": [],
        "acl": [{"kind": "schema", "schema": "app", "name": "app", "subname": "",
                 "owner": 2, "grantee": 1, "privilege": "USAGE"},
                {"kind": "relation", "schema": "app", "name": "child", "subname": "",
                 "owner": 2, "grantee": 1, "privilege": "SELECT"}],
        "relations": [{"oid": oid, "schema": "app", "name": name, "kind": kind, "owner": 2,
                       "rls": True, "force": False}
                      for oid, name, kind in ((10, "parent", "p"), (11, "child", "r"))],
        "policies": [policy(10), policy(11, using_true=True)],
        "partitions": [{"parent": 10, "child": 11}],
        "view_edges": [], "routine_edges": [], "routines": [], "policy_routines": [],
    }
    s["sections"] = {key: {"status": "ok", "rows": value} for key, value in rows.items()}
    c = {**contract(s), "rls": [{"schema": "app", "table": "parent", "roles": {"web": ["select"]}}]}
    return s, c


def policy(relation, **changes):
    return {"relation": relation, "roles": [1], "command": "*", "permissive": True,
            "has_using": True, "using_true": False, "has_check": False, "check_true": False,
            "session_setting": False, **changes}


def child_findings(s, c):
    return [f for f in findings(make_report(s, c, TODAY), "RLS_PATH")
            if isinstance(f["subject"], list) and "child" in f["subject"]]


class TestSecurityRegressions(unittest.TestCase):
    def test_enabled_child_unfiltered_policy_is_a_separate_exposure(self):
        s, c = rls_fixture()
        result = child_findings(s, c)
        self.assertEqual([f["status"] for f in result], ["finding"])
        self.assertTrue(result[0]["source"].endswith("/ddl-inherit.html"))

    def test_child_restrictive_predicate_is_unknown_not_a_confirmed_leak(self):
        s, c = rls_fixture()
        s["sections"]["policies"]["rows"].append(policy(11, permissive=False))
        self.assertEqual([f["status"] for f in child_findings(s, c)], ["unknown"])

    def test_child_policy_must_apply_to_operation_and_effective_role(self):
        s, c = rls_fixture()
        s["sections"]["policies"]["rows"][1]["command"] = "a"
        self.assertEqual([f["status"] for f in child_findings(s, c)], ["pass"])
        s["sections"]["policies"]["rows"][1].update(command="r", roles=[3])
        self.assertEqual([f["status"] for f in child_findings(s, c)], ["pass"])

    def test_child_owner_and_set_role_bypass_are_reported(self):
        s, c = rls_fixture()
        s["sections"]["memberships"]["rows"].append(
            {"member": 1, "role": 2, "inherit_option": False, "set_option": True})
        # Owner has table access through its materialized owner ACL, as in collection.
        s["sections"]["acl"]["rows"].extend(
            [{**acl, "grantee": 2} for acl in s["sections"]["acl"]["rows"]])
        result = child_findings(s, c)
        self.assertTrue(any("owner" in f["subject"] and f["status"] == "finding" for f in result))
        s["sections"]["relations"]["rows"][1]["force"] = True
        owner = [f for f in child_findings(s, c) if "owner" in f["subject"]]
        self.assertEqual([f["status"] for f in owner], ["pass"])
        s["sections"]["roles"]["rows"][1]["bypassrls"] = True
        self.assertEqual([f["status"] for f in child_findings(s, c) if "owner" in f["subject"]], ["finding"])

    def test_child_without_schema_usage_has_no_direct_route(self):
        s, c = rls_fixture()
        s["sections"]["acl"]["rows"] = [a for a in s["sections"]["acl"]["rows"] if a["kind"] != "schema"]
        self.assertEqual(child_findings(s, c), [])

    def test_tls_key_requires_effective_readability_and_approved_readers(self):
        s = snapshot()
        file = {"id": "key", "kind": "tls_key", "platform": "unix", "mode": "0640",
                "owner_uid": 0, "owner_gid": 999}
        c = {**contract(s), "runtime": {**observed(s), "process_uid": 999, "files": [file]}}
        self.assertEqual(findings(make_report(s, c, TODAY), "FILE_ACCESS", "key")[0]["status"], "unknown")
        file.update(service_readable=True, unauthorized_access=False)
        self.assertEqual(findings(make_report(s, c, TODAY), "FILE_ACCESS", "key")[0]["status"], "pass")
        file["unauthorized_access"] = True
        self.assertEqual(findings(make_report(s, c, TODAY), "FILE_ACCESS", "key")[0]["status"], "finding")

    def test_tls_key_unknown_platform_cannot_bypass_mode_checks(self):
        s = snapshot()
        file = {"id": "key", "kind": "tls_key", "platform": "linux", "mode": "0644",
                "owner_uid": 999, "service_readable": True, "unauthorized_access": False}
        c = {**contract(s), "runtime": {**observed(s), "process_uid": 999, "files": [file]}}
        self.assertEqual(findings(make_report(s, c, TODAY), "FILE_ACCESS", "key")[0]["status"], "unknown")

    def test_tls_root_owned_key_unreadable_by_service_is_a_finding(self):
        for mode in ("0600", "0640"):
            with self.subTest(mode=mode):
                s = snapshot()
                c = {**contract(s), "runtime": {**observed(s), "process_uid": 999, "files": [{
                    "id": "key", "kind": "tls_key", "platform": "unix", "mode": mode, "owner_uid": 0,
                    "service_readable": False, "unauthorized_access": False}]}}
                self.assertEqual(findings(make_report(s, c, TODAY), "FILE_ACCESS", "key")[0]["status"], "finding")

    def test_password_validity_uses_instants_and_date_only_is_uncertain(self):
        s = snapshot()
        s["sections"] = {
            "roles": {"status": "ok", "rows": [{"oid": 1, "name": "web", "login": True,
                                               "valid_until": "2026-11-06T09:00:00+09:00"}]},
            "passwords": {"status": "ok", "rows": [{"oid": 1, "format": "scram"}]}}
        c = {**contract(s), "password_roles": {"web": {"method": "password"}}}
        cases = ((TODAY, "unknown"),
                 (datetime(2026, 11, 6, 1, tzinfo=timezone.utc), "finding"),
                 (datetime(2026, 11, 6, 0, tzinfo=timezone.utc), "pass"))
        for at, status in cases:
            with self.subTest(at=at):
                self.assertEqual(findings(make_report(s, c, at), "PASSWORD", "web")[0]["status"], status)

    def test_password_validity_date_boundary_is_utc(self):
        s = snapshot()
        s["sections"] = {
            "roles": {"status": "ok", "rows": [{"oid": 1, "name": "web", "login": True,
                                               "valid_until": "2026-11-06T01:00:00+09:00"}]},
            "passwords": {"status": "ok", "rows": [{"oid": 1, "format": "scram"}]}}
        c = {**contract(s), "password_roles": {"web": {"method": "password"}}}
        self.assertEqual(findings(make_report(s, c, TODAY), "PASSWORD", "web")[0]["status"], "finding")

    def test_naive_evaluation_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            make_report(snapshot(), {}, datetime(2026, 11, 6, 9))


if __name__ == "__main__":
    unittest.main()
