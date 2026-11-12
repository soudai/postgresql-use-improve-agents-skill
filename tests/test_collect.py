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

    def test_marker_follows_the_first_keyword_of_the_select_only(self):
        # pg_stat_statements 18/19 stores a statement from its first token: a leading comment is lost.
        self.assertEqual(MODULE.mark("\nSELECT jsonb_build_object('a', 1)"),
                         "SELECT /* postgres-performance-diagnostics */ jsonb_build_object('a', 1)")
        self.assertRaises(ValueError, MODULE.mark, "   ")
        response = subprocess.CompletedProcess([], 0, "{}", "")
        with patch.object(MODULE.subprocess, "run", return_value=response) as run:
            MODULE.Psql(options()).run("SELECT 1")
        sent = run.call_args.kwargs["input"]
        self.assertIn("SELECT " + MODULE.MARKER + " 1;", sent)
        # BEGIN/SET/COMMIT share their query ID with the application's statements: never tag them.
        self.assertEqual(sent.count(MODULE.MARKER), 1)
        pgss = (MODULE.SQL_DIR / "pgss.sql").read_text(encoding="utf-8")
        self.assertIn("NOT LIKE '%" + MODULE.MARKER + "%'", pgss)

    def test_failures_without_sqlstate_are_named_not_quoted(self):
        cases = {
            'psql: error: connection to server on socket "/run/postgresql/.s.PGSQL.5432" failed: FATAL:  database "shop_prod" does not exist': "database_not_found",
            'psql: error: connection to server on socket "/run/postgresql/.s.PGSQL.5432" failed: FATAL:  role "monitor" does not exist': "role_not_found",
            'psql: error: connection to server at "10.1.2.3", port 5432 failed: FATAL:  password authentication failed for user "monitor"': "authentication_failed",
            'psql: error: connection to server at "10.1.2.3", port 5432 failed: fe_sendauth: no password supplied': "password_required",
            'psql: error: connection to server at "10.1.2.3", port 5432 failed: FATAL:  no pg_hba.conf entry for host "10.9.9.9", user "monitor", database "shop", no encryption': "rejected_by_pg_hba",
            'psql: error: connection to server at "10.1.2.3", port 5432 failed: Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?': "connection_refused",
            'psql: error: connection to server at "10.1.2.3", port 5432 failed: timeout expired': "connection_timeout",
            'psql: error: could not translate host name "db.internal" to address: Name or service not known': "unknown_host",
            'psql: error: connection to server on socket "/tmp/.s.PGSQL.5432" failed: No such file or directory\n\tIs the server running locally and accepting connections on that socket?': "socket_not_found",
            'psql: error: connection to server at "10.1.2.3", port 5432 failed: FATAL:  sorry, too many clients already': "too_many_connections",
            'psql: error: connection to server at "10.1.2.3", port 5432 failed: FATAL:  the database system is starting up': "server_not_accepting_connections",
            'psql: error: definition of service "prod" not found': "service_not_found",
            "Error response from daemon: No such container: prod-db": "docker_container_not_found",
            "Error response from daemon: container 0123abcd is not running": "docker_container_not_running",
            "ERROR:  42501\nDETAIL: SECRET": "sqlstate_42501",
            "": "psql_failed",
        }
        for stderr, expected in cases.items():
            with self.subTest(expected=expected):
                response = subprocess.CompletedProcess([], 2, "", stderr)
                with patch.object(MODULE.subprocess, "run", return_value=response):
                    with self.assertRaises(MODULE.CollectionError) as caught:
                        MODULE.Psql(options()).run("SELECT 1")
                # Only the fixed token: no host, role, database or socket path.
                self.assertEqual(str(caught.exception), expected)

    def collect_with(self, section_results, manifest, **overrides):
        metadata = {"server_version_num": 180006, "database_oid": 5,
            "postmaster_start": "2026-10-26T00:00:00Z", "in_recovery": False,
            "collected_at": "2026-10-26T01:00:00Z", "stats_visible": True, "extensions": [],
            "relations": [{"schema": value.split(".")[0], "name": value.split(".")[1]}
                          for item in manifest for value in item["requires"]]}
        with patch.object(MODULE, "preflight", side_effect=lambda client: copy.deepcopy(metadata)), \
                patch.object(MODULE.json, "loads", return_value=manifest), \
                patch.object(MODULE.Psql, "section", side_effect=section_results) as section:
            return MODULE.collect(options(**overrides)), section

    def test_lock_timeout_is_retried_once_and_recorded(self):
        manifest = [{"name": "tables", "file": "tables.sql", "requires": ["pg_catalog.pg_stat_user_tables"]},
                    {"name": "indexes", "file": "indexes.sql", "requires": ["pg_catalog.pg_stat_user_indexes"]}]
        good = lambda: {"status": "ok", "rows": [], "truncated": False, "collected_at": "2026-10-26T01:00:00Z"}
        locked = MODULE.CollectionError("sqlstate_55P03")
        result, section = self.collect_with([good(), locked, good(), locked, locked], manifest)
        self.assertEqual(result["sections"]["tables"]["status"], "ok")
        self.assertEqual(result["sections"]["tables"]["retried_after"], "sqlstate_55P03")
        self.assertEqual(result["sections"]["indexes"]["reason"], "sqlstate_55P03")
        self.assertIn("lock_timeout", result["sections"]["indexes"]["hint"])
        self.assertEqual(section.call_count, 5)  # settings + two attempts for each section, no more.

    def test_index_definitions_are_computed_only_on_request(self):
        manifest = [{"name": "indexes", "file": "indexes.sql", "requires": ["pg_catalog.pg_stat_user_indexes"]}]
        good = {"status": "ok", "rows": [], "truncated": False, "collected_at": "2026-10-26T01:00:00Z"}
        for include, expected in ((False, "WHEN false AND"), (True, "WHEN true AND")):
            _, section = self.collect_with([copy.deepcopy(good), copy.deepcopy(good)], manifest,
                                           include_query_text=include)
            sql = section.call_args_list[1].args[0]
            # pg_get_indexdef() takes a table lock; with false the planner never calls it.
            self.assertIn(expected, sql)
            self.assertNotIn("__INCLUDE_TEXT__", sql)

    def test_manifest_files_exist_and_guard_blocking_functions(self):
        manifest = json.loads((MODULE.SQL_DIR / "queries.json").read_text(encoding="utf-8"))
        self.assertEqual(len({item["name"] for item in manifest}), len(manifest))
        for item in manifest:
            self.assertTrue((MODULE.SQL_DIR / item["file"]).is_file(), item["file"])
        for name in ("tables.sql", "indexes.sql"):
            sql = (MODULE.SQL_DIR / name).read_text(encoding="utf-8")
            # Size functions queue behind ACCESS EXCLUSIVE; each call must sit behind the busy check.
            self.assertIn("AccessExclusiveLock", sql)
            for call in ("pg_relation_size(", "pg_table_size(", "pg_indexes_size(", "pg_total_relation_size(",
                         "pg_get_indexdef("):
                for line in sql.splitlines():
                    if call in line and not line.lstrip().startswith("--"):
                        self.assertIn("THEN", line, f"{name}: unguarded {call}")
        for name in ("activity.sql", "locks.sql"):
            sql = (MODULE.SQL_DIR / name).read_text(encoding="utf-8")
            self.assertIn("NOT l.granted", sql)  # pg_blocking_pids() only for sessions that wait.


if __name__ == "__main__":
    unittest.main()
