# Investigate a slow query and an index hypothesis

[日本語](../ja/01-slow-query.md) · [Examples](README.md)

This is a synthetic scenario and an illustrative report, not a live measurement
or the exact output of a command. The export filenames below are placeholders:
provide your own exports when using the prompt; these files are not bundled.

## Scenario and supplied evidence

An order-search API on PostgreSQL 18 misses its latency target during a busy
10-minute window. Start with the supplied statistics and saved execution plan.

| Evidence | Supplied value |
| --- | --- |
| Application latency | p95 420 ms; target p95 ≤ 200 ms, measured by application telemetry |
| Statistics window | 600 seconds, same instance, database, role and startup; no reset or entry eviction |
| Target SQL delta | 900 calls; `total_exec_time` 180,000 ms |
| All top-level SQL | `total_exec_time` delta 300,000 ms in the same window |
| Table and indexes | `public.orders`, 1,000,000 rows; only a primary-key index on `id` |
| Saved scan node | Seq Scan; estimated 100 rows; actual 20,000 rows; loops 1; 980,000 rows removed by filter |
| Plan provenance | Actual execution captured in staging for one representative tenant; generic/custom mode not recorded |
| Missing evidence | Other tenant distributions, statistics freshness, write load and index size estimate |

The supplied SQL shape is:

```sql
SELECT id, tenant_id, status, created_at
FROM public.orders
WHERE tenant_id = $1 AND status = 'pending'
ORDER BY created_at DESC;
```

There is no LIMIT or early-stopped scan in this saved plan. The API needs the
complete result for this example; changing result semantics requires a separate decision.

## Prompt

```text
Use $postgres-performance-diagnostics to investigate the order-search API.
I will provide before.json and after.json statistics exports, a saved plan.json,
schema.sql, and application latency measurements for the same 600-second window.
The evidence table and SQL above describe the supplied data.

Rank the SQL by interval impact. Separate observed facts from possible causes.
Explain the row-estimate error and whether an index is worth testing.
Treat the plan as one tenant's example and leave its generic/custom mode unknown.
Produce a report with priority, evidence, uncertainty, change cost, and a validation
plan. Use the supplied evidence first; do not run SQL or apply DDL in this task.
```

## Illustrative result

**Priority: investigate the order-search SQL.** It accounts for
180,000 / 300,000 = **60%** of observed top-level SQL execution time.
Its interval mean is 180,000 / 900 = **200 ms per call**. The separate application
p95 of 420 ms exceeds the target; SQL means do not establish request percentiles
or explain all of the API latency.

The scan returns **200 times** its estimated rows: 20,000 / 100.
It filters out 980,000 of 1,000,000 rows in this execution. This supports checking
statistics and access paths; it does not prove that every Seq Scan is inefficient.
The parameter marker `$1` does not establish a generic plan.

| Candidate | Basis and remaining uncertainty | Proposed check |
| --- | --- | --- |
| Statistics or data skew | Large estimate error; freshness and tenant/status distribution are unknown | Inspect column and extended statistics, ANALYZE history and representative tenants |
| B-tree on `(tenant_id, status, created_at DESC)` | Equality predicates and ordering match this shape; only `id` is indexed | Compare plans and total runtime in staging, including heap access and result transfer |
| Excessive result volume | This execution returns 20,000 rows | Confirm consumption and transfer cost before proposing pagination or fewer columns |

The index is a **test candidate**, with benefit unmeasured. Account for storage,
write amplification, WAL, and build load before selecting a deployment method.
Refreshing statistics or adding extended statistics is also conditional on the
inspection results. No production DDL or performance improvement is claimed.

## Next verification

1. Inspect the target relation and statistics under an appropriate role; distinguish
   missing visibility from missing statistics.
2. Compare common and skewed tenants with the same data, parameters and cache conditions.
   Record plan provenance and check result equivalence.
3. Measure API p95, throughput, SQL interval time, buffers and write/WAL cost before
   and after each isolated change. Define rollback if latency or writes regress.

See the [skill](../../skills/postgres-performance-diagnostics/SKILL.md),
[query and index guide](../../skills/postgres-performance-diagnostics/references/query-tuning.md),
[relation inspection SQL](../../skills/postgres-performance-diagnostics/sql/inspect_relation.sql),
and [validation guide](../../skills/postgres-performance-diagnostics/references/validation.md).
