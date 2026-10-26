import argparse
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("snapshot_collect", ROOT / "skills/postgres-performance-diagnostics/scripts/collect.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def options(**values):
    defaults = dict(psql="psql", docker_container=None, instance_id="test", include_query_text=False,
                    statement_timeout_ms=10000, lock_timeout_ms=1000, row_limit=2)
    defaults.update(values)
    return argparse.Namespace(**defaults)


class CollectTests(unittest.TestCase):
    def test_missing_extension_still_collects_builtin_evidence(self):
        manifest = json.loads((MODULE.SQL_DIR / "queries.json").read_text(encoding="utf-8"))
        required = {relation for item in manifest for relation in item["requires"]
                    if not relation.startswith("extension.")}
        metadata = {"server_version_num": 180006, "database_oid": 5,
            "postmaster_start": "2026-10-26T00:00:00Z", "in_recovery": False,
            "collected_at": "2026-10-26T01:00:00Z", "stats_visible": False,
            "extensions": [], "relations": [{"schema": value.split(".")[0], "name": value.split(".")[1]}
                                             for value in sorted(required)]}
        section = {"status": "ok", "rows": [], "truncated": False, "collected_at": metadata["collected_at"]}
        with patch.object(MODULE, "preflight", side_effect=lambda client: copy.deepcopy(metadata)), \
                patch.object(MODULE.Psql, "section", return_value=section):
            result = MODULE.collect(options())
        self.assertEqual(result["sections"]["pgss"]["status"], "unavailable")
        self.assertEqual(result["sections"]["tables"]["status"], "ok")
        self.assertIn("restricted_statistics_visibility", result["warnings"])

    def test_one_permission_error_does_not_discard_other_sections(self):
        manifest = [{"name": "database", "file": "database.sql", "requires": ["pg_catalog.pg_stat_database"]},
                    {"name": "io", "file": "io.sql", "requires": ["pg_catalog.pg_stat_io"]}]
        metadata = {"server_version_num": 190000, "database_oid": 5,
            "postmaster_start": "2026-10-26T00:00:00Z", "in_recovery": False,
            "collected_at": "2026-10-26T01:00:00Z", "stats_visible": True, "extensions": [],
            "relations": [{"schema": "pg_catalog", "name": "pg_stat_database"},
                          {"schema": "pg_catalog", "name": "pg_stat_io"}]}
        good = {"status": "ok", "rows": [], "truncated": False, "collected_at": metadata["collected_at"]}
        with patch.object(MODULE, "preflight", side_effect=lambda client: copy.deepcopy(metadata)), \
                patch.object(MODULE.json, "loads", return_value=manifest), \
                patch.object(MODULE.Psql, "section", side_effect=[good, MODULE.CollectionError("sqlstate_42501"), good]):
            result = MODULE.collect(options())
        self.assertEqual(result["sections"]["database"]["status"], "error")
        self.assertEqual(result["sections"]["io"]["status"], "ok")

    def test_command_never_uses_shell_or_connection_string(self):
        response = subprocess.CompletedProcess([], 0, '{"value":1}', "")
        with patch.object(MODULE.subprocess, "run", return_value=response) as run:
            self.assertEqual(MODULE.Psql(options()).run("SELECT 1"), {"value": 1})
        args, kwargs = run.call_args
        self.assertIn("-X", args[0])
        self.assertIn("--no-password", args[0])
        self.assertFalse(kwargs.get("shell", False))
        self.assertIn("BEGIN READ ONLY", kwargs["input"])
        self.assertIn("statement_timeout = '10000ms'", kwargs["input"])
        self.assertIn("lock_timeout = '1000ms'", kwargs["input"])

    def test_server_errors_do_not_leak_query_or_credentials(self):
        response = subprocess.CompletedProcess([], 1, "", "ERROR:  42501\nDETAIL: SECRET_QUERY password=SECRET_PASSWORD")
        with patch.object(MODULE.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(MODULE.CollectionError, "^sqlstate_42501$"):
                MODULE.Psql(options()).run("SELECT 1")

    def test_client_timeout_is_bounded_and_sanitized(self):
        with patch.object(MODULE.subprocess, "run", side_effect=subprocess.TimeoutExpired("SECRET", 10)):
            with self.assertRaisesRegex(MODULE.CollectionError, "^client_timeout$"):
                MODULE.Psql(options()).run("SELECT 1")

    def test_psql_noise_is_not_accepted_as_snapshot(self):
        response = subprocess.CompletedProcess([], 0, "startup noise\n{}", "")
        with patch.object(MODULE.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(MODULE.CollectionError, "invalid_psql_json"):
                MODULE.Psql(options()).run("SELECT 1")

    def test_row_limit_reports_truncation(self):
        client = MODULE.Psql(options())
        with patch.object(client, "run", return_value={"rows": [1, 2, 3], "collected_at": "now"}) as run:
            result = client.section("SELECT * FROM pg_stat_activity;", ("query", "conninfo"))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["rows"], [1, 2])
        self.assertIn("LIMIT 3", run.call_args.args[0])
        self.assertIn(" - 'query' - 'conninfo'", run.call_args.args[0])

    def test_schema_identifier_is_quoted(self):
        self.assertEqual(MODULE.quote_identifier('odd"schema'), '"odd""schema"')

    def test_docker_is_explicit_no_host_env_forwarding(self):
        with patch.object(MODULE.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            MODULE.Psql(options(docker_container="isolated-fixture")).run("SELECT 1")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["docker", "exec", "-i"])
        self.assertIn("isolated-fixture", command)
        self.assertNotIn("PGPASSWORD", " ".join(command))

    def test_positive_limits(self):
        for value in ("0", "-1"):
            self.assertRaises(argparse.ArgumentTypeError, MODULE.positive, value)


if __name__ == "__main__":
    unittest.main()
