"""Behavioral acceptance for authentication and runtime issues #26-#28."""
import unittest

from test_security import NOW, contract, findings, make_report, observed, snapshot
from report import render


class SecurityAuthAcceptanceTests(unittest.TestCase):
    def credentials(self, role, fmt=None, method="password", **changes):
        s = snapshot()
        r = {"oid": 1, "name": role, "login": True, "valid_until": None, **changes}
        s["sections"] = {"roles": {"status": "ok", "rows": [r]},
                         "settings": {"status": "ok", "rows": [{"name": "password_encryption", "setting": "scram-sha-256"}]},
                         "passwords": {"status": "error" if fmt is None else "ok",
                                       "rows": [] if fmt is None else [{"oid": 1, "format": fmt}]}}
        c = {**contract(s), "password_roles": {role: {"method": method}}}
        return s, c

    def test_password_modes_and_unknown_verifier_are_separate(self):
        for fmt, method, login, expected in (
                ("md5", "password", True, "finding"), ("none", "password", True, "finding"),
                ("scram", "password", True, "pass"), (None, "password", True, "unknown"),
                ("none", "external", True, "not_applicable"), ("none", "password", False, "not_applicable")):
            with self.subTest(fmt=fmt, method=method, login=login):
                s, c = self.credentials("app", fmt, method, login=login)
                self.assertEqual(findings(make_report(s, c, NOW), "PASSWORD", "app")[0]["status"], expected)

    def test_current_scram_setting_does_not_update_existing_md5(self):
        s, c = self.credentials("app", "md5")
        report = make_report(s, c, NOW)
        self.assertEqual(findings(report, "PASSWORD", "app")[0]["status"], "finding")
        self.assertEqual(findings(report, "CREDENTIAL_LIFECYCLE", "app")[0]["status"], "unknown")

    def test_scram_does_not_prove_strength_or_revocation(self):
        s, c = self.credentials("app", "scram")
        self.assertEqual(findings(make_report(s, c, NOW), "CREDENTIAL_LIFECYCLE", "app")[0]["status"], "unknown")
        spec = c["password_roles"]["app"]
        spec.update(verification=observed(s), lifecycle={"generated": True, "unique": True,
                                                       "default_removed": True, "revocation_available": False})
        self.assertEqual(findings(make_report(s, c, NOW), "CREDENTIAL_LIFECYCLE", "app")[0]["status"], "finding")
        spec["lifecycle"]["revocation_available"] = True
        self.assertEqual(findings(make_report(s, c, NOW), "CREDENTIAL_LIFECYCLE", "app")[0]["status"], "pass")

    def test_external_auth_and_root_named_role_do_not_imply_bad_password_or_os_root(self):
        s, c = self.credentials("root", "none", "external")
        c["runtime"] = {**observed(s), "process_uid": 999, "entrypoint_uid": 0}
        result = make_report(s, c, NOW)
        self.assertEqual(findings(result, "PASSWORD", "root")[0]["status"], "not_applicable")
        self.assertEqual(findings(result, "RUNTIME", "postgres process")[0]["status"], "pass")

    def test_runtime_controls_are_independent_of_rootless_claim(self):
        s = snapshot()
        c = {**contract(s), "runtime": {**observed(s), "process_uid": 999, "rootless": True,
                                      "unneeded_capabilities": True, "unneeded_host_mounts": True}}
        report = make_report(s, c, NOW)
        for item in ("unneeded_capabilities", "unneeded_host_mounts"):
            self.assertEqual(findings(report, "RUNTIME", item)[0]["status"], "finding")
        self.assertEqual(findings(report, "RUNTIME", "docker_socket")[0]["status"], "unknown")
        c["runtime"] = {**observed(s), "deployment": "managed"}
        report = make_report(s, c, NOW)
        self.assertEqual(findings(report, "RUNTIME", "provider-managed host")[0]["status"], "unknown")

    def test_windows_acl_observation_and_unobserved_access_are_distinct(self):
        s = snapshot()
        file = {"kind": "backup", "id": "backup", "platform": "windows"}
        c = {**contract(s), "runtime": {**observed(s), "process_uid": 999, "files": [file]}}
        self.assertEqual(findings(make_report(s, c, NOW), "FILE_ACCESS", "backup")[0]["status"], "unknown")
        for permitted, expected in ((True, "finding"), (False, "pass")):
            file["unauthorized_access"] = permitted
            self.assertEqual(findings(make_report(s, c, NOW), "FILE_ACCESS", "backup")[0]["status"], expected)

    def test_password_file_mode_owner_and_unknown_identity(self):
        s = snapshot()
        file = {"kind": "pgpass", "id": "client-password", "platform": "unix",
                "mode": "0600", "owner_uid": 999, "service_uid": 999}
        c = {**contract(s), "runtime": {**observed(s), "process_uid": 999, "files": [file]}}
        self.assertEqual(findings(make_report(s, c, NOW), "FILE_ACCESS", file["id"])[0]["status"], "pass")
        file["mode"] = "0640"
        self.assertEqual(findings(make_report(s, c, NOW), "FILE_ACCESS", file["id"])[0]["status"], "finding")
        file["mode"], file["owner_uid"] = "0600", 1000
        self.assertEqual(findings(make_report(s, c, NOW), "FILE_ACCESS", file["id"])[0]["status"], "finding")
        del file["service_uid"]
        self.assertEqual(findings(make_report(s, c, NOW), "FILE_ACCESS", file["id"])[0]["status"], "unknown")

    def test_secret_metadata_is_not_serialized_into_reports(self):
        secret = "auth_acceptance_synthetic_canary_75a2"
        s, c = self.credentials("app", "scram")
        s["sections"]["settings"]["rows"].append({"name": "archive_command", "setting": secret})
        s["sections"]["passwords"]["rows"][0]["verifier"] = secret
        c["runtime"] = {**observed(s), "process_uid": 999, "files": [
            {"kind": "backup", "id": "backup", "platform": "unix", "mode": "0644",
             "content": secret, "path": secret, "unauthorized_access": True}],
            "secret_locations": [{"kind": "dsn", "value": secret}]}
        report = make_report(s, c, NOW)
        for format_name in ("json", "markdown", "issue"):
            with self.subTest(format=format_name):
                self.assertNotIn(secret, render(report, format_name))


if __name__ == "__main__":
    unittest.main()

