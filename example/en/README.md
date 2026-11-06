# Prompts and example reports

[Repository guide](../../README.md) | [日本語](../ja/README.md)

Choose the case closest to your task. Replace the example target, goals and evidence with your own observations. Each page includes a prompt, a report and the next verification steps.

| Case | Skills | What the report demonstrates |
| --- | --- | --- |
| [01 — Slow query](01-slow-query.md) | Performance diagnostics | Row-estimate error, index hypotheses and verification before claiming a cause |
| [02 — Lock contention](02-lock-contention.md) | Performance diagnostics | Blocking-chain analysis and limits caused by incomplete activity visibility |
| [03 — Configuration review](03-configuration-review.md) | Configuration tuning, performance diagnostics | Requested checkpoints, JIT accounting and scoped comparison |
| [04 — Schema review](04-schema-review.md) | Schema review | Missing constraints, naming, lifecycle and contention evidence, with justified exceptions |
| [05 — Security review](05-security-review.md) | Security review | Role privileges, RLS routes, password uncertainty and effective TLS access |
| [06 — Upgrade readiness](06-upgrade-readiness.md) | Security review, workload validation | Upstream versus vendor evidence, unknown update status and recovery after writes |
| [07 — Workload validation](07-workload-validation.md) | Workload validation | Result mismatch remains actionable despite lower latency and missing metrics |

## How to use the examples

Cases 01–06 describe fictional observations and illustrative agent reports. Their filenames are placeholders for evidence you supply; they are not bundled input files, fixed output templates or measured production improvements.

Case 07 includes two small JSON files under [inputs/workload](../inputs/workload/) and a command to run from the repository root. Its result excerpt was checked against the actual script. The data is synthetic and too small for performance acceptance.

The examples use `$skill-name`; adapt the invocation if your agent uses another syntax. Provide the actual files mentioned in a prompt or replace the filenames with supplied observations. State what is known, what is missing and which changes are authorized. Never fill gaps with invented measurements.

The skill links on each page lead to the full workflow and reference material, currently primarily in Japanese.
