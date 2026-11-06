# 07. Workload validation: reject a faster-looking candidate with different results

[日本語](../ja/07-workload-validation.md) · [Use-case index](README.md)

This example uses synthetic functional fixtures to demonstrate the comparison script's decisions. It runs offline with Python 3.10+ and does not connect to a database. The data is not a measured benchmark or evidence of a performance improvement.

## Scenario and inputs

Suppose a SQL change affects an order summary. [baseline.json](../inputs/workload/baseline.json) and [candidate.json](../inputs/workload/candidate.json) contain matching comparison conditions and one one-second trial with four successful events per variant. Every number was created for this example.

| Item | Baseline | Candidate |
| --- | --- | --- |
| Successful event latencies | 12, 14, 16, 18 ms | 6, 7, 8, 9 ms |
| Result check value | `orders=4;total=100` | `orders=4;total=99` |
| DB and OS metrics | CPU, read I/O, WAL, OOM | CPU and read I/O only |
| Independent trials | 1 | 1 |
| Validation policy | None | None |

The `conditions.server` value is `synthetic-no-database` and `conditions.cache` is `synthetic`. They do not describe a real server version or cache state. `complete: true` means that the synthetic event array contains all four events.

## Example prompt

Use the repository root as the working directory and submit:

```text
$postgres-workload-validation

Compare example/inputs/workload/baseline.json with
example/inputs/workload/candidate.json.
These are synthetic fixtures for checking the comparison logic.
No database connection or workload replay is needed.

The example limits are successful p95 <= 20 ms, completed TPS >= 4,
a combined failed, timed-out, and skipped event rate of 0, and OOM kills <= 0.
Run the existing compare_runs.py and report correctness, performance,
evidence_quality, and improvement separately.
Preserve the result mismatch despite missing metrics or insufficient trials.
Do not claim a performance improvement or production acceptance.
Explain the additional validation needed.
```

## Reproduce offline

Run from the repository root. These limits are for this functional example; they are not operational recommendations.

```sh
python3 skills/postgres-workload-validation/scripts/compare_runs.py \
  example/inputs/workload/baseline.json \
  example/inputs/workload/candidate.json \
  --p95-ms 20 --min-tps 4 --max-loss-rate 0 --metric-max oom_kills=0
```

If your Python 3 command is named `python`, replace `python3` with `python`. The command writes JSON to standard output. It deliberately omits `--policy`, so the validation plan needed for an acceptance decision remains missing.

## Example result

The following fields are selected from the actual CLI output.

```json
{
  "schema_version": 2,
  "decision": "rollback_candidate",
  "correctness": {
    "status": "mismatch",
    "observed_digest_difference": true,
    "conditions_comparable": true
  },
  "performance": {
    "status": "slo_met_in_observed_runs",
    "violations": []
  },
  "evidence_quality": {
    "status": "insufficient"
  },
  "improvement": {
    "status": "not_established"
  },
  "missing": [
    "2 or more runs required per variant",
    "validation policy missing: sample size, observation period, variability and representative workload",
    "candidate 1: missing DB or OS metrics: oom_kills, wal_bytes",
    "candidate 1: missing oom_kills"
  ],
  "regressions": [
    "result mismatch"
  ]
}
```

| Assessment | Interpretation |
| --- | --- |
| correctness | The result check values differ under matching input conditions: a result mismatch. |
| performance | The candidate's calculated p95 is 9 ms, completed TPS is 4, and loss rate is 0. These satisfy the supplied limits for this array. |
| evidence_quality | Evidence is insufficient: one trial per variant, no validation policy, and missing candidate WAL and OOM metrics. |
| improvement | Not established. The baseline p95 is 18 ms, but four synthetic events cannot support a performance improvement claim. |

The decision is `rollback_candidate`. Missing measurements do not erase the observed result mismatch. The script only emits JSON; it does not roll back SQL or configuration changes. Equal minimum, median, and maximum values from one trial do not establish stability across trials.

## Next steps

1. Investigate the result mismatch and check results against the expected behavior. Do not merely replace the check values with matching strings.
2. Establish a representative workload using real SQL, parameter distributions, read/write proportions, and initial data. Define the test environment, write scope, and stop conditions.
3. Document the rationale for trial count, successful event count, observation period, and acceptable variability in a validation plan, and pass it with `--policy`. The lowest supported values do not guarantee sufficient evidence.
4. Collect DB, OS, and load-generator metrics over the same observation window; leave missing values missing instead of filling them with zero. Repeat independent trials and investigate effects on other SQL and differences unrelated to the change.

Workflow and input format: [postgres-workload-validation](../../skills/postgres-workload-validation/SKILL.md) · [Workload design and execution](../../skills/postgres-workload-validation/references/replay.md) · [Comparison script](../../skills/postgres-workload-validation/scripts/compare_runs.py)
