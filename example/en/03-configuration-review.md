# Review checkpoint and JIT settings without guessing values

[日本語](../ja/03-configuration-review.md) · [Examples](README.md)

This is a synthetic scenario and an illustrative report, not a live measurement
or the exact output of a command. The export filenames below are placeholders:
provide your own exports when using the prompt; these files are not bundled.

## Scenario and supplied evidence

A PostgreSQL 18 service with mixed transactional and reporting work has variable
response times. The team asks whether to enlarge WAL settings or disable JIT.

| Evidence | Supplied value |
| --- | --- |
| Available resources | 64 GiB RAM, 16 vCPU, SSD; actual concurrent queries not yet measured |
| Settings | `max_wal_size=1GB`, `checkpoint_timeout=300s`, `jit=on` |
| Worker ceilings | `max_parallel_workers=32`, `max_worker_processes=64`; actual worker use unknown |
| Statistics window | 1,800 seconds; same instance/startup, complete counters and no reset |
| Checkpoint deltas | `num_requested=9`, `num_timed=1` |
| Operations history | Three manual CHECKPOINTs; checkpoint reason logs unavailable |
| Top-level SQL delta | `total_exec_time=100,000 ms` across the observed SQL entries |
| JIT deltas for those entries | generation 4,000 ms; inlining 1,000 ms; optimization 2,000 ms; emission 1,000 ms |
| Nested JIT measurement | deform 3,000 ms, already included in generation |
| Missing evidence | WAL/FPI volume, retained WAL, disk headroom, recovery target, per-query JIT impact |

The settings export was taken under the application's role and database. Its
source and override metadata are included; no pending restart is reported.

## Prompt

```text
Use $postgres-config-tuning to review checkpoint and JIT settings.
I will provide settings.json, a performance interval report interval.json, and
operations notes matching the 1,800-second window. Their contents are described
in the evidence table above.

Explain which changes are supported and which need more evidence. Check the
checkpoint causes and JIT time decomposition. Distinguish configured worker
ceilings from observed concurrency. Leave unsupported recommended values null.
Produce current values, priorities, evidence, scope, risks, verification and
rollback plans. Prepare a review only; do not apply settings in this task.
```

## Illustrative result

**Investigate checkpoint causes and run a scoped JIT comparison.** The supplied
counters do not establish that either a larger WAL target or JIT off is better.

| Item | Current value | Recommended value | Evidence and next action |
| --- | --- | --- | --- |
| `max_wal_size` | 1GB | Undetermined (`null`) | 9 / (9 + 1) = 90% requested checkpoints; correlate causes with logs |
| `jit` | on | Undetermined (`null`) | Compilation accounts for 8% of top-level SQL execution time; compare on/off for representative queries |
| Parallel workers | ceiling 32; process ceiling 64 | Undetermined (`null`) | A ceiling above 16 vCPU does not establish concurrent CPU contention |
| Memory allocation | Actual concurrent queries unknown | Not evaluated | Obtain workload concurrency and memory-consuming plan nodes before sizing |

The 1,800-second window exceeds three times `checkpoint_timeout`, but length alone
does not identify the checkpoint causes. Three requests are explained by manual
operations; the remaining causes are unconfirmed. Requested checkpoints alone do
not prove WAL pressure. `max_wal_size` is a soft checkpoint target, not a disk cap.

JIT compilation is **4,000 + 1,000 + 2,000 + 1,000 = 8,000 ms**,
or **8%** of 100,000 ms. Adding deform again would incorrectly produce 11%.
The review's 5% trigger starts an investigation; it does not measure net loss.
JIT may reduce execution time enough to offset compilation on reporting queries.

## Next verification

1. Match checkpoint reason logs and operations records to the same window. Collect
   WAL/FPI rate, retained WAL, disk headroom and recovery requirements before sizing.
2. For representative transactional and reporting queries, compare JIT on/off in
   isolated staging sessions with the same data, parameters, prepared-plan behavior,
   cache and concurrent load. Repeat trials and include compilation in total time.
3. Record total latency, variance, throughput and impact on reporting queries. Adopt
   a role/session change only if the agreed targets improve without material regression.
4. Observe actual worker usage and OS CPU/run queue before changing worker limits.
   Measure concurrent memory demand separately; do not infer it from connection ceilings.

A session-scoped JIT trial ends by restoring the captured setting or closing the
trial session. Any later persistent change needs its own application and rollback
plan, with the original value, source and role/database override recorded.
No before/after improvement has been measured in this example.

See the [skill](../../skills/postgres-config-tuning/SKILL.md),
[parameter guide](../../skills/postgres-config-tuning/references/parameters.md),
[configuration reviewer](../../skills/postgres-config-tuning/scripts/review_config.py),
and [apply/verify guide](../../skills/postgres-config-tuning/references/apply-and-verify.md).
