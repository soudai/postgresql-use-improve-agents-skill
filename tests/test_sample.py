import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sample_activity", ROOT / "skills/postgres-performance-diagnostics/scripts/sample_activity.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def options(**overrides):
    values = {"instance_id": "fixture", "samples": 3, "interval": 1.0, "psql": "psql", "docker_container": None,
              "statement_timeout_ms": 5000, "lock_timeout_ms": 1000, "top": 20, "include_sessions": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def session(pid, state="active", wait_type=None, wait=None, query_id="42", **extra):
    row = {"pid": pid, "backend_type": "client backend", "state": state, "wait_event_type": wait_type,
           "wait_event": wait, "query_id": query_id, "datname": "app"}
    row.update(extra)
    return row


SAMPLES = [
    {"at": "t1", "track_activities": "on", "sessions": [session(1), session(2, wait_type="IO", wait="DataFileRead"),
                              session(3, "idle in transaction", "Client", "ClientRead", xact_age_s=30.5)]},
    {"at": "t2", "track_activities": "on", "sessions": [session(1), session(4, wait_type="Lock", wait="transactionid", query_id="7",
                                                   blocking_pids=[3]),
                              {"pid": 9, "backend_type": "autovacuum worker", "state": "active",
                               "wait_event_type": "Timeout", "wait_event": "VacuumDelay"}]},
    {"at": "t3", "track_activities": "on", "sessions": []},
]


class SampleTests(unittest.TestCase):
    def test_one_autocommit_statement_per_sample(self):
        text = MODULE.script(options())
        self.assertNotIn("BEGIN", text)
        self.assertEqual(text.count("pg_stat_activity"), 3)
        self.assertEqual(text.count("pg_sleep"), 2)
        self.assertIn("default_transaction_read_only = on", text)
        self.assertIn("statement_timeout = '5000ms'", text)
        # The sleep must fit inside the statement timeout.
        self.assertIn("statement_timeout = '31000ms'", MODULE.script(options(interval=30.0)))

    def test_wait_profile_and_query_attribution(self):
        summary = MODULE.summarize(SAMPLES)
        self.assertEqual(summary["samples"], 3)
        self.assertEqual(summary["average_active_sessions"], 4 / 3)
        self.assertEqual(summary["peak_active_sessions"], 2)
        profile = {x["wait"]: x["samples"] for x in summary["wait_profile"]}
        self.assertEqual(profile, {"No reported wait event": 2, "IO:DataFileRead": 1, "Lock:transactionid": 1})
        self.assertEqual(summary["by_query_id"][0]["query_id"], "42")
        self.assertEqual(summary["by_query_id"][0]["samples"], 3)
        self.assertEqual(summary["lock_blockers"], [{"pid": 3, "waiter_samples": 1}])
        self.assertEqual(summary["idle_in_transaction"]["max_xact_age_s"], 30.5)
        self.assertEqual(summary["background"][0]["wait"], "Timeout:VacuumDelay")

    def test_partial_output_keeps_samples_and_sanitizes_error(self):
        stdout = json.dumps(SAMPLES[0]) + "\n\n" + json.dumps(SAMPLES[1]) + "\n"
        response = subprocess.CompletedProcess([], 1, stdout, "ERROR:  57014\nDETAIL: SECRET")
        with patch.object(MODULE.subprocess, "run", return_value=response):
            report = MODULE.sample(options())
        self.assertEqual(report["summary"]["samples"], 2)
        self.assertEqual(report["error"], "sqlstate_57014")
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn("raw_samples", report)
        self.assertIn("Wait profile", MODULE.markdown(report))

    def test_no_samples_is_an_error(self):
        response = subprocess.CompletedProcess([], 2, "", "FATAL:  28P01")
        with patch.object(MODULE.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(MODULE.SamplingError, "sqlstate_28P01"):
                MODULE.sample(options())

    def test_interval_bounds(self):
        for value in ("0", "61"):
            self.assertRaises(argparse.ArgumentTypeError, MODULE.interval, value)


if __name__ == "__main__":
    unittest.main()
