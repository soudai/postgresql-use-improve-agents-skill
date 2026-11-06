# Review table design and refactoring candidates

[日本語](../ja/04-schema-review.md) · [Examples](README.md)

This is a synthetic scenario and an illustrative report, not a database assessment
or the exact output of a script.

## Scenario and supplied evidence

An order service wants to check integrity and whether separate business operations
share too much mutable state in `app.orders`. The supplied materials describe one
PostgreSQL 18 environment and the same deployment.

| Input supplied by the user | Relevant evidence |
| --- | --- |
| `orders-schema.json` | Snapshot v2; table, column and constraint collection completed without truncation; no inheritance children. No FK from `customer_id` to `app.customers(id)`. Its domain has NOT NULL, but `column_not_null=false`. |
| `orders-contract.json` | A sourced design contract: every order requires an existing customer; issued invoice facts are immutable, while order workflow state can change. Payment and shipping have separate writers and lifecycles. |
| `orders-evidence.json` | A scoped profile found 2 NULL customer IDs among 1,000 checked rows. A trace records an invoice-amount UPDATE after issue. Correlated payment/shipping traces show the same order key, blocker chain, transaction boundaries and a 600 ms wait. |
| `order-glossary.md` | The agreed column name is `shipping_status`; the catalog and application currently use `shiping_status`. |

The profile and traces cover 01:00–01:05 UTC on 2026-11-06 in this example.
They do not establish the frequency of problems outside that window.
The contract also documents that `cancelled_at` is absent until cancellation,
and that `email_opt_in` is an independent boolean preference.

These filenames are placeholders for your exports and design documents;
they are not bundled example assets. Preserve source references and timestamps
when preparing the contract and evidence.

## Prompt to copy

```text
$postgres-schema-review

Review the supplied orders-schema.json, orders-contract.json,
orders-evidence.json and order-glossary.md for app.orders.

Distinguish confirmed contract violations, refactoring candidates, missing
evidence and intentional exceptions. Check FK coverage, column versus domain
NOT NULL, naming, lifecycle, responsibility and finalized facts.
Do not treat all NULLs, booleans or mutable rows as defects.

Correlate payment/shipping writes with the supplied same-key blocker traces.
Group related lifecycle, responsibility and contention findings into a single
refactoring theme, keeping each piece of evidence. Compare splitting tables
with shortening transactions and changing update order.

Report scope, severity, confidence, evidence, proposed changes and verification.
Include compatibility, cardinality, RLS and rollback considerations.
Use the supplied evidence; return a report and a proposed migration plan.
```

## Illustrative result

| Finding | Assessment | Evidence and consequence |
| --- | --- | --- |
| Missing customer FK | `confirmed` | The complete constraint snapshot conflicts with the sourced local relationship contract. Orphan counts are still unknown. |
| Required customer ID | `confirmed` | Domain NOT NULL is not a column-wide guarantee; the scoped profile also contains 2 contract violations. |
| Invoice fact changed after issue | `confirmed` | The contract and observed post-issue UPDATE agree on the affected fact. Static UPDATE code alone would not establish this. |
| Mixed payment/shipping state | `candidate` | Separate writers and lifecycles plus the correlated 600 ms same-key wait support evaluating a split. They do not prove all waits originate here. |
| `shiping_status` spelling | `confirmed` | The glossary establishes the intended name; priority depends on compatibility impact. |

`cancelled_at`, `email_opt_in` and mutable current workflow state remain documented
exceptions to blanket NULL, flag and immutability rules. No redesign follows from
column count or an `updated_at` column alone.

A possible design separates issued invoice facts from mutable order state and
models payment attempts and shipments according to their actual cardinality.
One order may have multiple payment attempts or shipments; a one-to-one split is
not assumed. Correction events and retention rules need explicit agreement.

## Next verification steps

1. Check customer orphans, key types, referenced uniqueness, deletion behavior
   and any tenant key before planning the FK. Evaluate child-side indexing as a
   separate performance question.
2. Repair the 2 known NULLs and inspect the remaining requiredness scope. Plan
   column-level enforcement and validation for the actual server version;
   domain constraints and zero estimated NULLs do not replace this check.
3. Trace invoice corrections and all writers. Compare a shorter transaction
   with a split using equivalent operations, wait times, p95/p99 and retries.
4. Test cardinality, atomic business invariants, correction history and
   authorized/denied access in a disposable environment.
5. Plan compatible application releases and backfill. Preserve writes accepted
   by the new structure when evaluating rollback; verify constraints again
   after validation rather than treating an unvalidated migration as complete.

## References

- [Skill](../../skills/postgres-schema-review/SKILL.md)
- [Input formats and evidence scope](../../skills/postgres-schema-review/references/evidence.md)
- [Rules, counterexamples and design sources](../../skills/postgres-schema-review/references/rules.md)
- [Refactoring and verification](../../skills/postgres-schema-review/references/refactoring.md)
