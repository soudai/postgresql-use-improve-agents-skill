import importlib.util
from copy import deepcopy
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
        # Tiny values exercise a synthetic unit fixture, not a deployment benchmark.
        self.policy = {"schema_version": 1, "source": "synthetic unit fixture",
                       "representative_workload": True, "min_runs": 2,
                       "min_successful_events_per_run": 2, "min_elapsed_seconds": 1,
                       "max_relative_spread": 0.1}

    def observation(self, times, skipped=0, **changes):
        run = {"started_at": "2026-11-06T00:00:00Z", "elapsed_seconds": 1, "complete": True,
               "metrics": self.metrics, "result_digest": "same",
               "events": [{"status": "ok", "latency_ms": value} for value in times]
                         + [{"status": "skipped"}] * skipped}
        run.update(changes)
        return MODULE.summarize(run, Path("."), self.conditions)

    def comparison(self, before, after):
        def trials(run):
            second = deepcopy(run)
            second["started_at"] = "2026-11-06T00:00:02Z"
            return [run, second]
        return MODULE.compare((self.conditions, trials(before)),
                              (self.conditions, trials(after)), self.thresholds, {}, 90,
                              validation_policy=self.policy)

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


    def test_plan_cannot_claim_improvement_from_slo_compliance(self):
        ok = self.observation([1, 2])
        report = self.comparison(ok, ok)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["correctness"]["status"], "match")
        self.assertEqual(report["performance"]["status"], "slo_met_in_observed_runs")
        self.assertEqual(report["evidence_quality"]["status"], "sufficient_for_declared_plan")
        self.assertEqual(report["improvement"]["status"], "not_established")

    def test_declared_sampling_requirements_are_all_enforced(self):
        ok = self.observation([1, 2])
        for key, value in (("min_runs", 3), ("min_successful_events_per_run", 1000),
                           ("min_elapsed_seconds", 120), ("representative_workload", False)):
            with self.subTest(key=key):
                original = self.policy[key]
                self.policy[key] = value
                report = self.comparison(ok, ok)
                self.assertEqual(report["decision"], "investigate")
                self.assertEqual(report["correctness"]["status"], "match")
                self.policy[key] = original

    def test_unstable_trials_and_duplicate_observations_are_insufficient(self):
        first = self.observation([1, 2])
        second = self.observation([10, 20], started_at="2026-11-06T00:00:02Z")
        for group in ([first, second], [first, first]):
            with self.subTest(group=group):
                report = MODULE.compare((self.conditions, group), (self.conditions, group),
                                        self.thresholds, {}, validation_policy=self.policy)
                self.assertEqual(report["decision"], "investigate")

    def test_conditions_difference_does_not_establish_result_regression(self):
        before = self.observation([1, 2], result_digest="a")
        after = self.observation([1, 2], result_digest="b")
        report = MODULE.compare((self.conditions, [before]), ({"data": "different"}, [after]),
                                self.thresholds, {})
        self.assertEqual(report["correctness"]["status"], "unknown")
        self.assertTrue(report["correctness"]["observed_digest_difference"])
        self.assertNotIn("result mismatch", report["regressions"])

    def test_validation_policy_rejects_unsupported_and_nonfinite_values(self):
        for key, value in (("schema_version", 2), ("min_runs", True),
                           ("min_successful_events_per_run", 0), ("min_elapsed_seconds", 0),
                           ("max_relative_spread", float("nan")), ("source", ""),
                           ("representative_workload", "true")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                MODULE.validate_policy({**self.policy, key: value})
