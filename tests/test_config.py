import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("review_config", ROOT / "skills/postgres-config-tuning/scripts/review_config.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# PostgreSQL 18 defaults in pg_settings units.
DEFAULTS = {
    "shared_buffers": ("16384", "8kB", "postmaster"), "effective_cache_size": ("524288", "8kB", "user"),
    "work_mem": ("4096", "kB", "user"), "hash_mem_multiplier": ("2", None, "user"),
    "maintenance_work_mem": ("65536", "kB", "user"), "max_connections": ("100", None, "postmaster"),
    "autovacuum_max_workers": ("3", None, "sighup"), "checkpoint_completion_target": ("0.9", None, "sighup"),
    "checkpoint_timeout": ("300", "s", "sighup"), "max_wal_size": ("1024", "MB", "sighup"),
    "wal_compression": ("off", None, "superuser"), "random_page_cost": ("4", None, "user"),
    "effective_io_concurrency": ("16", None, "user"), "maintenance_io_concurrency": ("16", None, "user"),
    "io_method": ("worker", None, "postmaster"), "jit": ("on", None, "user"),
    "max_worker_processes": ("8", None, "postmaster"), "max_parallel_workers": ("8", None, "user"),
    "max_parallel_workers_per_gather": ("2", None, "user"), "autovacuum_vacuum_cost_delay": ("2", "ms", "sighup"),
    "autovacuum_vacuum_cost_limit": ("-1", None, "sighup"), "vacuum_cost_limit": ("200", None, "user"),
    "autovacuum_naptime": ("60", "s", "sighup"), "shared_preload_libraries": ("pg_stat_statements", None, "postmaster"),
    "compute_query_id": ("auto", None, "superuser"), "track_io_timing": ("on", None, "superuser"),
    "log_checkpoints": ("on", None, "sighup"), "log_lock_waits": ("on", None, "superuser"),
    "log_temp_files": ("0", "kB", "superuser"), "log_autovacuum_min_duration": ("600000", "ms", "sighup"),
    "log_min_duration_statement": ("1000", "ms", "superuser"), "track_activity_query_size": ("4096", "B", "postmaster"),
    "idle_in_transaction_session_timeout": ("300000", "ms", "user"), "statement_timeout": ("0", "ms", "user"),
    "max_prepared_transactions": ("0", None, "postmaster"), "temp_file_limit": ("10485760", "kB", "superuser"),
    "fsync": ("on", None, "sighup"), "full_page_writes": ("on", None, "sighup"),
    "synchronous_commit": ("on", None, "user"), "autovacuum": ("on", None, "sighup"),
    "track_counts": ("on", None, "superuser"), "huge_pages": ("try", None, "postmaster"),
}


def settings(**overrides):
    rows = []
    for name, (value, unit, context) in DEFAULTS.items():
        rows.append({"name": name, "setting": overrides.get(name, value), "unit": unit, "context": context,
                     "source": "default", "pending_restart": False})
    return {"kind": "pg_settings_export", "settings": rows}


def options(**overrides):
    values = {"ram_gb": None, "cpus": None, "storage": None, "workload": "mixed",
              "max_active_connections": None, "managed": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def run(data, report=None, **overrides):
    result = MODULE.review(MODULE.load_settings(data), options(**overrides), report)
    return result, {f["parameter"]: f for f in result["findings"]}


def interval(timed, requested, elapsed=3600, wal_rate=10 * 1024 ** 2, temp=0, jit=0):
    return {"deltas": {
        "checkpointer": [{"elapsed_seconds": elapsed, "delta": {"num_timed": timed, "num_requested": requested}}],
        "wal": [{"delta": {"wal_records": 1000, "wal_fpi": 100}, "per_second": {"wal_bytes": wal_rate}}],
        "database": [{"per_second": {"temp_bytes": temp}, "average_active_sessions": 3}],
        "pgss": [{"toplevel": True, "delta": {"total_exec_time": 1000, "jit_generation_time": jit, "jit_inlining_time": 0,
            "jit_optimization_time": 0, "jit_emission_time": 0}}]}}


class ConfigTests(unittest.TestCase):
    def test_unknown_inputs_are_not_guessed(self):
        result, found = run(settings())
        self.assertNotIn("shared_buffers", found)
        rules = {x["rule"] for x in result["not_evaluated"]}
        self.assertTrue({"memory", "connections", "planner_io", "max_wal_size"} <= rules)

    def test_well_tuned_defaults_produce_no_high_findings(self):
        data = settings(shared_buffers=str(16 * 1024 ** 3 // 8192), effective_cache_size=str(44 * 1024 ** 3 // 8192),
                        maintenance_work_mem=str(2 * 1024 ** 2), random_page_cost="1.1", huge_pages="on",
                        autovacuum_vacuum_cost_limit="1500", wal_compression="lz4", jit="off")
        result, found = run(data, interval(12, 0), ram_gb=64, cpus=8, storage="ssd", workload="oltp",
                            max_active_connections=40)
        self.assertEqual([f["parameter"] for f in result["findings"]], [])

    def test_data_safety_is_critical(self):
        _, found = run(settings(fsync="off", autovacuum="off"))
        self.assertEqual(found["fsync"]["severity"], "critical")
        self.assertEqual(found["autovacuum"]["severity"], "critical")

    def test_memory_sizing_and_units(self):
        _, found = run(settings(), ram_gb=64, cpus=16)
        self.assertEqual(found["shared_buffers"]["current"], "128MB")
        self.assertEqual(found["shared_buffers"]["recommended"], "16GB")
        self.assertEqual(found["shared_buffers"]["apply"], "restart")
        self.assertEqual(found["effective_cache_size"]["recommended"], "45GB")

    def test_managed_skips_shared_buffers(self):
        _, found = run(settings(), ram_gb=64, managed=True)
        self.assertNotIn("shared_buffers", found)

    def test_work_mem_overcommit_uses_active_connections(self):
        data = settings(work_mem=str(512 * 1024), max_connections="500")
        _, found = run(data, ram_gb=16)
        self.assertEqual(found["work_mem"]["severity"], "high")
        _, found = run(data, ram_gb=16, max_active_connections=4, parallel_workers_per_query=0)
        self.assertNotIn("work_mem", found)

    def test_zero_budget_has_no_numeric_recommendation(self):
        result, found = run(settings(shared_buffers=str(int(3.5 * 1024 ** 3 // 8192))),
                            ram_gb=16, memory_limit_gb=4, max_active_connections=100,
                            plan_shape_known=True)
        self.assertEqual(result["memory_budget"]["query_budget_bytes"], 0)
        self.assertIsNone(found["work_mem"]["recommended"])

    def test_evidence_target_mismatch_is_excluded(self):
        report = interval(4, 8)
        report.update(instance_id="other", database="postgres")
        result = MODULE.review(MODULE.load_settings(settings()), options(), report,
                               provenance={"instance_id": "local", "database": "postgres"})
        self.assertEqual(result["evidence"], {})
        self.assertTrue(any("instance_id" in warning for warning in result["warnings"]))
        self.assertFalse(any(f["parameter"] == "max_wal_size" for f in result["findings"]))

    def test_temp_spill_suggests_scoped_work_mem(self):
        _, found = run(settings(), interval(12, 0, temp=1024 ** 2), ram_gb=64, max_active_connections=20)
        self.assertEqual(found["work_mem"]["scope"], "role/session")

    def test_requested_checkpoints_require_cause_evidence(self):
        _, found = run(settings(), interval(4, 8))
        self.assertEqual(found["max_wal_size"]["severity"], "medium")
        self.assertIsNone(found["max_wal_size"]["recommended"])
        self.assertEqual(found["max_wal_size"]["evidence"]["cause"], "unknown")
        result, found = run(settings(), interval(1, 2, elapsed=600))
        self.assertNotIn("max_wal_size", found)
        self.assertIn("interval shorter", json.dumps(result["not_evaluated"]))

    def test_jit_evidence_and_storage_rules(self):
        _, found = run(settings(), interval(12, 0, jit=200), storage="ssd")
        self.assertEqual(found["jit"]["severity"], "medium")
        self.assertIsNone(found["jit"]["recommended"])
        self.assertEqual(found["random_page_cost"]["recommended"], "1.1")
        _, found = run(settings(), storage="hdd")
        self.assertNotIn("random_page_cost", found)

    def test_role_scoped_safety_nets(self):
        _, found = run(settings(idle_in_transaction_session_timeout="0", statement_timeout="30000"))
        self.assertEqual(found["idle_in_transaction_session_timeout"]["scope"], "role")
        self.assertEqual(found["statement_timeout"]["severity"], "info")

    def test_accepts_diagnostics_snapshot_and_cli(self):
        snapshot = {"sections": {"settings": {"rows": settings()["settings"]}}}
        self.assertIn("work_mem", MODULE.load_settings(snapshot))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "settings.json"
            source.write_text(json.dumps(settings(fsync="off")), encoding="utf-8")
            output = Path(directory) / "review.md"
            self.assertEqual(MODULE.main([str(source), "--output", str(output)]), 0)
            self.assertIn("| critical | fsync |", output.read_text(encoding="utf-8"))
        self.assertRaises(ValueError, MODULE.load_settings, {"unexpected": True})


if __name__ == "__main__":
    unittest.main()
