"""Counterexamples found by the independent skill audit."""
import argparse
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
def load(name, relative):
    spec=importlib.util.spec_from_file_location(name,ROOT/relative)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
CONFIG=load("audit_fix_config","skills/postgres-config-tuning/scripts/review_config.py")
WORK=load("audit_fix_work","skills/postgres-workload-validation/scripts/compare_runs.py")


class DiagnosticEvidenceTests(unittest.TestCase):
    def test_requested_checkpoints_do_not_establish_wal_cause(self):
        settings={"max_wal_size":{"setting":"1024","unit":"MB"},"checkpoint_timeout":{"setting":"300","unit":"s"}}
        args=argparse.Namespace(ram_gb=None,memory_limit_gb=None)
        engine=CONFIG.Review(settings,args,{"checkpoints_timed":0,"checkpoints_requested":3,
                                          "interval_seconds":1800,"wal_bytes_per_s":1})
        engine.wal()
        found=next(f for f in engine.findings if f["parameter"]=="max_wal_size")
        self.assertIsNone(found["recommended"])
        self.assertTrue(found["evidence"]["requested"])

    def test_deform_is_a_subset_of_generation(self):
        report={"deltas":{"pgss":[{"toplevel":True,"delta":{
            "total_exec_time":100,"jit_generation_time":4,"jit_deform_time":3,
            "jit_inlining_time":0,"jit_optimization_time":0,"jit_emission_time":0}}]}}
        ev=CONFIG.evidence_from_report(report)
        self.assertEqual(ev["jit_share"],.04)
        engine=CONFIG.Review({"jit":{"setting":"on"}},argparse.Namespace(ram_gb=None,workload="mixed"),ev)
        engine.jit()
        self.assertFalse(engine.findings)

    def test_missing_jit_counters_are_not_zero_cost(self):
        report = {"deltas": {"pgss": [{"toplevel": True, "delta": {"total_exec_time": 100}}]}}
        self.assertNotIn("jit_share", CONFIG.evidence_from_report(report))
        engine = CONFIG.Review({"jit": {"setting": "on"}},
                               argparse.Namespace(ram_gb=None, workload="oltp"), {})
        engine.jit()
        self.assertTrue(engine.findings)
        self.assertIsNone(engine.findings[0]["recommended"])

    def sample(self, digest="same", started="2026-11-06T00:00:00Z", metrics=None):
        metrics={"cpu_pct":1,"io_read_bytes":0,"wal_bytes":0,"oom_kills":0} if metrics is None else metrics
        return WORK.summarize({"started_at":started,"elapsed_seconds":.001,"complete":True,
                              "result_digest":digest,"events":[{"status":"ok","latency_ms":.1}],
                              "metrics":metrics},Path("."),{"fixture":"same"})

    def comparison(self, before, after):
        return WORK.compare(({"fixture":"same"},before),({"fixture":"same"},after),
                            {"p95_ms":100,"completed_tps":1,"loss_rate":.01},{})

    def test_result_mismatch_survives_too_few_runs(self):
        result=self.comparison([self.sample()],[self.sample("different")])
        self.assertIn("result mismatch",result["regressions"])
        self.assertNotEqual(result["decision"],"accept_candidate")

    def test_one_event_repeated_is_not_acceptance_evidence(self):
        a=self.sample()
        b=self.sample(started="2026-11-06T00:00:01Z")
        result=self.comparison([a,b],[a,b])
        self.assertNotEqual(result["decision"],"accept_candidate")

    def test_missing_metrics_do_not_hide_result_failure(self):
        a=self.sample()
        b=self.sample(started="2026-11-06T00:00:01Z")
        bad=self.sample("different",metrics={})
        bad2=self.sample("different",started="2026-11-06T00:00:01Z",metrics={})
        result=self.comparison([a,b],[bad,bad2])
        self.assertIn("result mismatch",result["regressions"])
        self.assertEqual(result["decision"],"rollback_candidate")
        self.assertTrue(result["missing"])


if __name__=="__main__":
    unittest.main()
