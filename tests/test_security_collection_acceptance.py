"""Security collection invariants from issue #23."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/postgres-security-review/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("security_collection_acceptance", SCRIPTS / "collect.py")
COLLECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECT)


class CollectionAcceptanceTests(unittest.TestCase):
    def test_original_settings_are_separate_from_collector_overrides(self):
        settings = [{"name": "statement_timeout", "setting": "7000", "unit": "ms",
                     "source": "database user"}]
        output = json.dumps(settings) + "\n" + json.dumps({"rows": [{"setting": "5000"}]}) + "\n"
        with patch.object(COLLECT.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=output, stderr="")):
            result = COLLECT.Collector(5000).query("SELECT 1")
        self.assertEqual(result["collection_settings"], settings)
        self.assertEqual(result["rows"], [{"setting": "5000"}])

    def test_error_only_preserves_sqlstate_not_server_secret(self):
        secret = "synthetic_canary_23"
        with patch.object(COLLECT.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=secret, stderr="ERROR: 42501 " + secret)):
            with self.assertRaisesRegex(COLLECT.CollectionError, "^sqlstate_42501$"):
                COLLECT.Collector(5000).query("SELECT 1")

    def test_timeout_and_invalid_output_do_not_expose_raw_values(self):
        secret = "synthetic_canary_23"
        with patch.object(COLLECT.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("psql", 10, output=secret, stderr=secret)):
            with self.assertRaisesRegex(COLLECT.CollectionError, "^client_timeout$"):
                COLLECT.Collector(5000).query("SELECT 1")
        for output in (secret, "[]\n" + secret, "[]\n[]\n", "{}\n{}\n"):
            with self.subTest(output=output), patch.object(COLLECT.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")):
                with self.assertRaisesRegex(COLLECT.CollectionError, "^invalid_query_output$"):
                    COLLECT.Collector(5000).query("SELECT 1")


    def test_partial_catalog_failure_keeps_successful_sections(self):
        identity = {"database": "app", "version_num": 180006, "session_role": "observer"}
        meta = {"identity": identity, "collected_at": "2026-11-06T00:00:00Z"}
        good = {"identity": identity, "observed_at": "2026-11-06T00:00:01Z", "rows": [{"name": "app"}]}
        with patch.object(COLLECT, "queries", return_value={"databases": "one", "roles": "two"}):
            collector = COLLECT.Collector(5000)
            with patch.object(collector, "query", side_effect=[meta, good, COLLECT.CollectionError("sqlstate_42703")]):
                result = collector.collect("node", "app", 2, False)
        self.assertEqual(result["sections"]["databases"]["status"], "ok")
        self.assertEqual(result["sections"]["roles"], {"status": "unavailable", "reason": "sqlstate_42703"})

    def test_changed_identity_is_not_combined_with_initial_scope(self):
        identity = {"database": "app", "version_num": 180006, "session_role": "observer"}
        meta = {"identity": identity, "collected_at": "2026-11-06T00:00:00Z"}
        changed = {"identity": {**identity, "session_role": "different"},
                   "observed_at": "2026-11-06T00:00:01Z", "rows": [{"name": "must-not-join"}]}
        with patch.object(COLLECT, "queries", return_value={"roles": "one"}):
            collector = COLLECT.Collector(5000)
            with patch.object(collector, "query", side_effect=[meta, changed]):
                result = collector.collect("node", "app", 2, False)
        self.assertEqual(result["sections"]["roles"], {"status": "unavailable", "reason": "target_changed"})
