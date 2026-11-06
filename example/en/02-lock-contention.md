# Trace blocking sessions with incomplete visibility

[日本語](../ja/02-lock-contention.md) · [Examples](README.md)

This is a synthetic scenario and an illustrative report, not a live measurement
or the exact output of a command. The export filenames below are placeholders:
provide your own exports when using the prompt; these files are not bundled.

## Scenario and supplied evidence

Order updates stall intermittently on PostgreSQL 18. Activity was sampled
60 times at a nominal one-second interval during the incident. The collector's
`track_activities` was on, but some other sessions' state/type was hidden.

| Evidence | Supplied value |
| --- | --- |
| Observed active session-samples | 180 across 60 samples |
| Observed waits | 144 `Lock:transactionid`; 36 `No reported wait event` |
| Unknown session-samples | 120; state/backend type not visible |
| Snapshot root | PID 4101: `idle in transaction`, transaction age 180 seconds, no blocking PID |
| Direct blockers in the same snapshot | 4201 → [4101], 4202 → [4101], 4301 → [4201] |
| Snapshot identity | Backend start times recorded; the four PIDs belong to distinct current sessions |
| Missing evidence | SQL text, earlier transaction statements, application trace and OS CPU metrics |

Arrows in the blockers row mean **waiter → blockers**, as returned by
`pg_blocking_pids()`. The blocker may have acquired locks in an earlier statement.

## Prompt

```text
Use $postgres-performance-diagnostics to investigate intermittent order-update stalls.
I will provide samples.json from activity sampling and incident.json containing the
same-time activity/lock snapshot and pg_blocking_pids() results.
The evidence table above describes the supplied data.

Reconstruct the root blocker, direct and indirect waiters, and chain depth.
Explain how hidden sessions affect AAS and the denominator of wait percentages.
Separate facts from hypotheses and identify the next evidence needed to find the
transaction boundary responsible for the blocking. Provide a report and a scoped
verification plan. Do not cancel or terminate sessions in this task.
```

## Illustrative result

**Prioritize the transaction owned by PID 4101.** In this snapshot it is the
root blocker for **three waiting sessions**, with a maximum chain depth of **two**.
The graph below uses the opposite direction: **blocker → waiter**.

```text
4101 (idle in transaction, age 180 s)
├── 4201
│   └── 4301
└── 4202
```

The transaction remains open while the application is idle. This is evidence of
a held transaction and blocking, not proof of which earlier SQL acquired each lock.
The snapshot alone does not establish a deadlock or a table-design defect.

| Metric | Interpretation |
| --- | --- |
| `activity_coverage` | `incomplete`, because some sessions' activity is hidden |
| `average_active_sessions` / `peak_active_sessions` | `null`; full activity cannot be evaluated |
| `observed_average_active_sessions` | 180 / 60 = **3.0**, a lower bound for the sampled scope |
| Lock wait share | 144 / 180 = **80% of observed active session-samples** |
| Remaining observed share | 36 / 180 = **20%**, with no reported wait event; this does not prove CPU saturation |

The 120 unknown session-samples cannot be classified as idle, background or zero
load. The 80% is not a whole-server wait share. PID 4101's idle time is reported
separately from active-session time; a blocker need not consume much DB time.

## Next verification

1. Re-sample with an already authorized monitoring role and check whether visibility
   becomes complete. Keep sampling frequency bounded and compare with normal periods.
2. Match backend start times, request traces and transaction histories. Identify work
   between BEGIN and COMMIT, including external calls and exception paths.
3. Test shorter transactions or corrected commit/rollback paths on the affected flow.
   Evaluate role-specific timeouts against legitimate long-running work.
4. Compare lock-wait duration, chain depth, transaction duration, API latency and retry
   errors under equivalent load. Record rollback for the application or timeout change.

Session termination would require a separate operational decision about the owning
request and rollback cost. Cancelling the current query alone may leave an idle
transaction and its locks in place.

See the [skill](../../skills/postgres-performance-diagnostics/SKILL.md),
[wait-event guide](../../skills/postgres-performance-diagnostics/references/wait-events.md),
[locking guide](../../skills/postgres-performance-diagnostics/references/locking.md),
and [activity sampler](../../skills/postgres-performance-diagnostics/scripts/sample_activity.py).
