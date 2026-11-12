import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
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


class FakeProcess:
    """Stands in for the long-lived psql: prints the prepared lines, then reports an exit status."""

    def __init__(self, lines, returncode=0, stderr=""):
        self.stdin = io.StringIO()
        self.stdin.close = lambda: None  # Keep what was written readable after the sampler closes it.
        self.stdout = iter(lines)
        self.stderr = iter(stderr.splitlines(keepends=True))
        self.returncode, self.killed = returncode, False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


class Clock:
    """A manual clock: sleeping advances it, so the test observes the pacing without waiting."""

    def __init__(self):
        self.now, self.sleeps = 100.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def drive(lines, *, clock=None, returncode=0, stderr="", **overrides):
    process = FakeProcess([json.dumps(line) + "\n" if isinstance(line, dict) else line for line in lines],
                          returncode, stderr)
    clock = clock or Clock()
    result = MODULE.run(options(**overrides), popen=lambda *a, **k: process, clock=clock, sleep=clock.sleep)
    return result, process, clock


class SampleTests(unittest.TestCase):
    def test_client_paces_one_autocommit_statement_per_sample(self):
        (samples, error, pacing), process, clock = drive(SAMPLES)
        sent = process.stdin.getvalue()
        self.assertEqual((len(samples), error), (3, None))
        self.assertNotIn("BEGIN", sent)
        self.assertEqual(sent.count("FROM pg_catalog.pg_stat_activity AS a"), 3)
        # The server must not sleep for the sampler: that time would be charged to the workload.
        self.assertNotIn("pg_sleep", sent)
        self.assertEqual(clock.sleeps, [1.0, 1.0])
        self.assertEqual(pacing["late_samples"], 0)
        self.assertIn("default_transaction_read_only = on", sent)
        self.assertIn("statement_timeout = '5000ms'", sent)
        self.assertIn("idle_session_timeout = 0", sent)

    def test_marker_follows_the_first_keyword(self):
        # pg_stat_statements stores a statement from its first token: a leading comment is lost.
        self.assertTrue(MODULE.SAMPLE_SQL.startswith("SELECT " + MODULE.MARKER))
        self.assertNotIn(MODULE.MARKER, MODULE.setup_script(options()))

    def test_slow_sample_restarts_the_cadence_without_a_burst(self):
        class SlowClock(Clock):
            def __init__(self):
                super().__init__()
                self.reads = 0

            def __call__(self):
                # Every look at the clock costs 0.4s, so each sample overruns the 0.5s interval.
                self.reads += 1
                self.now += 0.4
                return self.now

        (samples, error, pacing), _, clock = drive(SAMPLES, clock=SlowClock(), interval=0.5)
        self.assertEqual(len(samples), 3)
        self.assertGreaterEqual(pacing["late_samples"], 1)
        self.assertTrue(all(value > 0 for value in clock.sleeps))

    def test_wait_profile_and_query_attribution(self):
        summary = MODULE.summarize(SAMPLES)
        self.assertEqual(summary["samples"], 3)
        self.assertEqual(summary["average_active_sessions"], 4 / 3)
        self.assertEqual(summary["peak_active_sessions"], 2)
        profile = {x["wait"]: x["samples"] for x in summary["wait_profile"]}
        self.assertEqual(profile, {"No reported wait event": 2, "IO:DataFileRead": 1, "Lock:transactionid": 1})
        classes = {x["wait_class"]: x["samples"] for x in summary["wait_class_profile"]}
        self.assertEqual(classes, {"No reported wait event": 2, "IO": 1, "Lock": 1})
        self.assertEqual(summary["by_query_id"][0]["query_id"], "42")
        self.assertEqual(summary["by_query_id"][0]["samples"], 3)
        self.assertEqual(summary["idle_in_transaction"]["max_xact_age_s"], 30.5)
        self.assertEqual(summary["background"][0]["wait"], "Timeout:VacuumDelay")

    def test_blocker_is_described_from_its_own_samples(self):
        blocker = MODULE.summarize(SAMPLES)["lock_blockers"][0]
        self.assertEqual((blocker["pid"], blocker["waiter_samples"], blocker["root_samples"]), (3, 1, 1))
        # PID 3 was seen idle in transaction one sample earlier: that is who holds the lock.
        self.assertEqual((blocker["last_state"], blocker["max_xact_age_s"]), ("idle in transaction", 30.5))
        self.assertFalse(blocker["prepared_transaction"])
        chained = MODULE.summarize([{"track_activities": "on", "sessions": [
            session(1, wait_type="Lock", wait="transactionid", blocking_pids=[2]),
            session(2, wait_type="Lock", wait="relation", blocking_pids=[0])]}])["lock_blockers"]
        by_pid = {x["pid"]: x for x in chained}
        self.assertEqual(by_pid[2]["root_samples"], 0)  # PID 2 blocks PID 1 but waits itself.
        self.assertTrue(by_pid[0]["prepared_transaction"] and by_pid[0]["root_samples"] == 1)

    def test_load_is_split_by_database_role_and_application(self):
        samples = [{"at": "t", "track_activities": "on", "client_backends": 12, "max_connections": 100, "sessions": [
            session(1, datname="shop", usename="api", application_name="checkout"),
            session(2, datname="shop", usename="api", application_name="checkout", wait_type="IO", wait="DataFileRead"),
            session(3, datname="shop", usename="batch", application_name=None)]}]
        summary = MODULE.summarize(samples)
        self.assertEqual(summary["by_database"][0]["name"], "shop")
        self.assertEqual({x["name"]: x["samples"] for x in summary["by_user"]}, {"api": 2, "batch": 1})
        self.assertEqual({x["name"]: x["samples"] for x in summary["by_application"]}, {"checkout": 2, "unknown": 1})
        self.assertAlmostEqual(summary["by_user"][0]["share"], 2 / 3)
        self.assertEqual(summary["connections"], {"average_client_backends": 12, "peak_client_backends": 12,
                                                  "max_connections": 100})

    def test_timeline_is_bounded_and_keeps_peaks(self):
        samples = [{"at": f"t{i}", "track_activities": "on",
                    "sessions": [session(n) for n in range(9 if i == 700 else 1)]} for i in range(1000)]
        timeline = MODULE.summarize(samples)["timeline"]
        self.assertLessEqual(len(timeline), MODULE.TIMELINE_BUCKETS)
        self.assertEqual(sum(x["samples"] for x in timeline), 1000)
        peak = max(timeline, key=lambda x: x["peak_active"])
        self.assertEqual(peak["peak_active"], 9)
        self.assertTrue(peak["from"] <= "t700" <= peak["to"])

    def test_partial_output_keeps_samples_and_sanitizes_error(self):
        lines = [json.dumps(SAMPLES[0]) + "\n", "\n", json.dumps(SAMPLES[1]) + "\n"]
        process = FakeProcess(lines, 1, "ERROR:  57014\nDETAIL: SECRET")
        with patch.object(MODULE.subprocess, "Popen", return_value=process), \
                patch.object(MODULE.time, "sleep"):
            report = MODULE.sample(options())
        self.assertEqual(report["summary"]["samples"], 2)
        self.assertEqual(report["error"], "sqlstate_57014")
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn("raw_samples", report)
        text = MODULE.markdown(report)
        self.assertIn("Wait profile", text)
        self.assertIn("Sampling stopped early: sqlstate_57014", text)
        self.assertIn("Busiest moments", text)

    def test_no_samples_is_an_error(self):
        process = FakeProcess([], 2, "FATAL:  28P01")
        with patch.object(MODULE.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(MODULE.SamplingError, "sqlstate_28P01"):
                MODULE.sample(options())

    def test_connection_failures_are_named_without_their_text(self):
        cases = {
            'psql: error: connection to server on socket "/run/s.PGSQL.5432" failed: FATAL:  database "shop" does not exist': "database_not_found",
            'psql: error: connection to server at "db.internal" (10.0.0.9), port 5432 failed: FATAL:  password authentication failed for user "api"': "authentication_failed",
            'psql: error: connection to server at "127.0.0.1", port 5432 failed: fe_sendauth: no password supplied': "password_required",
            'psql: error: connection to server at "127.0.0.1", port 1 failed: Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?': "connection_refused",
            "Error response from daemon: No such container: prod-db": "docker_container_not_found",
            "something unexpected": "psql_failed",
        }
        for stderr, expected in cases.items():
            process = FakeProcess([], 2, stderr)
            with patch.object(MODULE.subprocess, "Popen", return_value=process):
                with self.assertRaises(MODULE.SamplingError) as caught:
                    MODULE.sample(options())
            self.assertEqual(str(caught.exception), expected)

    def test_interrupt_reports_the_samples_already_taken(self):
        clock = Clock()

        def interrupted(seconds):
            if len(clock.sleeps) == 1:
                raise KeyboardInterrupt
            clock.sleep(seconds)

        process = FakeProcess([json.dumps(s) + "\n" for s in SAMPLES])
        samples, error, _ = MODULE.run(options(), popen=lambda *a, **k: process, clock=clock, sleep=interrupted)
        self.assertEqual((len(samples), error), (2, "interrupted"))

    def test_silent_psql_is_a_client_timeout(self):
        class Silent(FakeProcess):
            def __init__(self):
                super().__init__([])
                self.stdout = iter(lambda: None, 0)  # Never yields: psql is stuck.

        class Jump(Clock):
            def __call__(self):
                self.now += 30  # Each look at the clock burns the whole answer budget.
                return self.now

        with patch.object(MODULE, "_pump"):
            samples, error, _ = MODULE.run(options(), popen=lambda *a, **k: Silent(), clock=Jump(),
                                           sleep=lambda seconds: None)
        self.assertEqual((samples, error), ([], "client_timeout"))

    def test_cli_writes_a_new_report_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reports" / "samples.md"
            process = FakeProcess([json.dumps(s) + "\n" for s in SAMPLES])
            messages = io.StringIO()
            with patch.object(MODULE.subprocess, "Popen", return_value=process), patch.object(MODULE.time, "sleep"), \
                    contextlib.redirect_stderr(messages):
                code = MODULE.main(["--instance-id", "fixture", "--samples", "3", "--format", "markdown",
                                    "--output", str(output)])
                refused = MODULE.main(["--instance-id", "fixture", "--output", str(output)])
            self.assertEqual((code, refused), (0, 2))
            self.assertIn("## Wait classes", output.read_text(encoding="utf-8"))
            self.assertIn("obtained 3/3", messages.getvalue())
            self.assertIn("already exists", messages.getvalue())

    def test_interval_bounds(self):
        for value in ("0", "61"):
            self.assertRaises(argparse.ArgumentTypeError, MODULE.interval, value)


if __name__ == "__main__":
    unittest.main()
