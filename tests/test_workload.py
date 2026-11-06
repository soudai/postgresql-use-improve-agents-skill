import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_runs", ROOT / "skills/postgres-workload-validation/scripts/compare_runs.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        self.conditions = {"cache": "warm", "data": "same"}
        self.metrics = {"cpu_pct": 20, "io_read_bytes": 10, "wal_bytes": 0, "oom_kills": 0,
                        "load_generator_cpu_pct": 20}
        self.thresholds = {"p95_ms": 50, "completed_tps": 1, "loss_rate": 0.1}

    def observation(self, times, skipped=0, **changes):
        run = {"started_at": "2026-11-06T00:00:00Z", "elapsed_seconds": 1, "complete": True,
               "metrics": self.metrics, "result_digest": "same",
               "events": [{"status": "ok", "latency_ms": value} for value in times]
                         + [{"status": "skipped"}] * skipped}
        run.update(changes)
        return MODULE.summarize(run, Path("."), self.conditions)

    def comparison(self, before, after):
        return MODULE.compare((self.conditions, [before, before]),
                              (self.conditions, [after, after]), self.thresholds, {}, 90)

    def test_improved_mean_does_not_hide_worse_tail(self):
        baseline = self.observation([20] * 100)
        candidate = self.observation([1] * 94 + [70] * 6)
        report = self.comparison(baseline, candidate)
        self.assertEqual(report["decision"], "rollback_candidate")
        self.assertLess(report["median_change"]["mean_ms"], 0)
        self.assertGreater(report["median_change"]["p95_ms"], 0)

    def test_skipped_is_loss_even_with_fast_successes(self):
        baseline = self.observation([10] * 10)
        candidate = self.observation([1] * 8, skipped=2)
        self.assertEqual(candidate["offered_tps"], 10)
        self.assertEqual(candidate["completed_tps"], 8)
        self.assertEqual(self.comparison(baseline, candidate)["decision"], "rollback_candidate")

    def test_missing_baseline_or_generator_saturation_requires_investigation(self):
        ok = self.observation([1, 2])
        no_metrics = self.observation([1, 2], metrics={})
        saturated = self.observation([1, 2], metrics={**self.metrics, "load_generator_cpu_pct": 99})
        self.assertEqual(self.comparison(ok, ok)["decision"], "accept_candidate")
        self.assertEqual(self.comparison(no_metrics, ok)["decision"], "investigate")
        self.assertEqual(self.comparison(ok, saturated)["decision"], "investigate")

    def test_pgbench_preserves_skipped_lag_and_retry_counts(self):
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            (directory / "test.log").write_text(
                "0 0 1000 0 1700000000 100 20 2\n0 1 skipped 0 1700000000 100 20000 0\n",
                encoding="utf-8")
            run = {"started_at": "2023-11-14T22:13:20Z", "elapsed_seconds": 1, "complete": True,
                   "pgbench_logs": ["test.log"]}
            result = MODULE.summarize(run, directory, {"rate": 10, "max_tries": 3})
            self.assertEqual(result["counts"], {"ok": 1, "skipped": 1})
            self.assertEqual(result["schedule_lag_p95_ms"], 20)
            self.assertEqual(result["retries"], 2)
