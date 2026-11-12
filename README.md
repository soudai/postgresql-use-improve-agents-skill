# PostgreSQL Diagnostics and Review Skills

[English](README.md) | [日本語](README_ja.md)

Five agent skills for investigating PostgreSQL performance, reviewing configuration, schema design and security, and validating changes under a representative workload. Each skill turns observations into findings, missing evidence, proposed changes, and verification steps.

Start with the [English examples](example/en/README.md) or [日本語の利用例](example/ja/README.md). Skill instructions and detailed reference documents are currently primarily in Japanese; the use-case examples are available in both languages.

## Choose a skill

| Task | Skill | Main inputs |
| --- | --- | --- |
| Slow SQL, waits, I/O, locks, vacuum, WAL or replication | [postgres-performance-diagnostics](skills/postgres-performance-diagnostics/SKILL.md) | Two statistics snapshots, wait samples, plans, logs or application traces |
| Memory, connections, checkpoints, autovacuum, planner or JIT settings | [postgres-config-tuning](skills/postgres-config-tuning/SKILL.md) | Current settings, available resources, workload and interval report |
| Missing constraints, naming, lifecycle, responsibilities or contention-prone tables | [postgres-schema-review](skills/postgres-schema-review/SKILL.md) | Catalog snapshot, business contract and operational evidence |
| Roles, RLS, authentication, TLS, runtime privileges or security updates | [postgres-security-review](skills/postgres-security-review/SKILL.md) | Catalog snapshot, access contract and evidence for each connection route |
| Correctness, latency, throughput and resource use before and after a change | [postgres-workload-validation](skills/postgres-workload-validation/SKILL.md) | Repeated run logs, matching conditions, SLOs and a validation policy |

Use the skills together when needed: diagnose a symptom, prepare a scoped change, then validate it. A schema or security review can also start independently from an existing snapshot.

## Try an offline example

Python 3.10 or later is sufficient for this example. Use `python3` in place of `python` if that is your interpreter's command name. From the repository root, run:

```text
python skills/postgres-workload-validation/scripts/compare_runs.py example/inputs/workload/baseline.json example/inputs/workload/candidate.json --p95-ms 20 --min-tps 4 --max-loss-rate 0 --metric-max oom_kills=0
```

The JSON result includes `"decision": "rollback_candidate"`: the candidate returns a different result, even though its synthetic latency values are lower. Missing resource metrics and insufficient trials remain visible in the report. The command analyzes local files and does not connect to PostgreSQL or perform a rollback.

These tiny fixtures demonstrate the decision logic; they are not benchmark measurements or sufficient evidence of performance. See the [complete prompt and result](example/en/07-workload-validation.md).

## Use a skill with your agent

### Claude Code plugin

This repository is also a Claude Code plugin marketplace. In Claude Code, run:

```text
/plugin marketplace add soudai/postgresql-use-improve-agents-skill
/plugin install postgresql-skills@postgresql-use-improve-agents-skill
```

All five skills are installed together. Claude selects a skill from its description, or you can name it explicitly, for example `/postgresql-skills:postgres-performance-diagnostics`. Update with `/plugin marketplace update postgresql-use-improve-agents-skill`. To try a local checkout without installing, start Claude Code with `claude --plugin-dir /path/to/postgresql-use-improve-agents-skill`.

The bundled scripts need Python 3.10 or later on the machine where Claude Code runs, and `psql` (or `--docker-container`) for collectors that connect to PostgreSQL.

### Other agents

Copy the complete folder for each skill you need from `skills/` into your agent's configured skill discovery directory. Keep its `SKILL.md`, `scripts/`, `sql/`, `references/` and `assets/` together where present. Check existing installations before replacing them. The discovery path and invocation syntax depend on your agent.

The examples use `$skill-name` invocation. For an agent that accepts it:

```text
Use $postgres-performance-diagnostics to review before.json and after.json.
The affected API has a p95 target of 200 ms. Rank candidates by user impact,
separate observations from hypotheses, and explain which additional evidence
is needed before proposing a change. Include verification and rollback steps.
```

`before.json` and `after.json` in this prompt are files you supply. Except for the workload fixtures above, the example filenames describe inputs to prepare; they are not bundled datasets.

## Examples by use case

Each page contains context, a copyable prompt, an example report and the next verification steps. Cases 01–06 use fictional observations and illustrative agent reports, rather than exact CLI output.

| Use case | English | Japanese |
| --- | --- | --- |
| Slow query, row estimates and index hypotheses | [01](example/en/01-slow-query.md) | [01](example/ja/01-slow-query.md) |
| Blocking chains and incomplete activity visibility | [02](example/en/02-lock-contention.md) | [02](example/ja/02-lock-contention.md) |
| Checkpoints, JIT and scoped configuration trials | [03](example/en/03-configuration-review.md) | [03](example/ja/03-configuration-review.md) |
| Constraints, naming and table lifecycle | [04](example/en/04-schema-review.md) | [04](example/ja/04-schema-review.md) |
| Least privilege, RLS, passwords and TLS | [05](example/en/05-security-review.md) | [05](example/ja/05-security-review.md) |
| Security update evidence and upgrade readiness | [06](example/en/06-upgrade-readiness.md) | [06](example/ja/06-upgrade-readiness.md) |
| Correctness regression despite lower latency | [07](example/en/07-workload-validation.md) | [07](example/ja/07-workload-validation.md) |

## Collect and analyze your own evidence

The following commands run from the repository root. Offline analyzers use Python 3.10+ and the standard library. Collecting from a live database additionally requires a compatible `psql` client and access to the target. SQL-level cumulative statistics use `pg_stat_statements` when available; unavailable sections are reported as limitations.

Configure the connection with libpq settings such as `PGSERVICE` and an existing password file. Use a stable, nonsecret instance label. For the performance collector:

```text
python skills/postgres-performance-diagnostics/scripts/collect.py --instance-id prod-primary --output snapshots/before.json
```

After a representative workload interval, collect from the same target and analyze the difference:

```text
python skills/postgres-performance-diagnostics/scripts/collect.py --instance-id prod-primary --output snapshots/after.json
python skills/postgres-performance-diagnostics/scripts/analyze.py snapshots/before.json snapshots/after.json --format json --output reports/interval.json --top 20
```

Restart, statistics reset, failover, role changes and workload changes can invalidate a comparison. Use fresh comparable snapshots when needed. For collection permissions, limits, wait sampling, plans and logs, see the [collection guide](skills/postgres-performance-diagnostics/references/collection.md) and [diagnostic workflow](skills/postgres-performance-diagnostics/SKILL.md).

To review configuration, collect settings using the application's connection role and database. Replace these example resource values with measured values for your environment:

```text
psql -X -qAt --set=instance_id=prod-primary --file skills/postgres-config-tuning/sql/settings.sql > snapshots/settings.json
python skills/postgres-config-tuning/scripts/review_config.py snapshots/settings.json --ram-gb 64 --cpus 16 --storage ssd --workload oltp --max-active-connections 80 --interval-report reports/interval.json --output reports/config.md
```

Omit unknown resource inputs; use `--managed` for a managed service. The JSON interval report above provides observations for the review. See [apply, verify and roll back](skills/postgres-config-tuning/references/apply-and-verify.md) for change planning.

Other inputs and workflows:

| Workflow | Preparation |
| --- | --- |
| Schema review | Follow [collection and input formats](skills/postgres-schema-review/references/evidence.md); adapt the [contract](skills/postgres-schema-review/assets/contract.example.json) and [evidence](skills/postgres-schema-review/assets/evidence.example.json) examples to your business rules. |
| Security review | Follow [collection and access contracts](skills/postgres-security-review/references/evidence.md); collect separately for each database and identify missing routes, runtime facts and vendor update evidence. |
| Workload validation | Follow [replay and comparison](skills/postgres-workload-validation/references/replay.md); define representative conditions and an evidence policy before collecting repeated runs. |

Each script exposes `--help`. Filenames in commands other than the bundled offline example are outputs to collect or inputs you provide. Security collectors require the output parent directories to exist and refuse to overwrite existing files.

## Interpret the report

- Missing, stale, truncated or inaccessible evidence is reported as unknown or limited. It is not equivalent to zero load, no finding or a pass.
- A sequential scan, nullable column, Boolean flag or mutable table needs context. A finding should connect an observed structure to a business invariant or measured behavior.
- Requested checkpoints alone do not establish WAL pressure. JIT compilation share alone does not justify disabling JIT.
- RLS flags, SCRAM verifier format and file modes alone do not prove effective isolation, password strength or authorized access.
- Workload report v2 separates correctness, performance, evidence quality and improvement. `accept_candidate` requires an explicit `--policy` and its evidence conditions; meeting SLOs alone does not establish a causal improvement.

The collectors primarily read catalogs and statistics. Plans using `EXPLAIN ANALYZE` execute the target SQL, and workload replay can write data. Apply DDL, configuration changes, restarts, session termination or replay only within the authorized scope, with concrete verification and recovery steps.

Avoid putting credentials in commands, prompts or tracked files. Identifiers, SQL, plans and logs can also contain sensitive information; limit collection and sharing to what the task requires. `snapshots/` and `reports/` are ignored by Git, but that does not restrict filesystem access.

## Compatibility and validation

Performance diagnostics, configuration tuning and schema review target PostgreSQL 18 and 19. Recorded integration runs include PostgreSQL 18.6 and 19 RC1; this is a tested-build record, not a statement about the latest release. Validate the exact server, extensions and managed-service behavior you use. The security collector supports version-dependent catalog collection on PostgreSQL 14+ and attempts identity/version collection on older releases; this does not imply complete coverage of every version.

Activity samples and schema snapshots use schema version 2. Legacy inputs may lack evidence needed for a complete assessment. See [wait-event interpretation](skills/postgres-performance-diagnostics/references/wait-events.md), [schema input compatibility](skills/postgres-schema-review/references/evidence.md) and [workload report fields](skills/postgres-workload-validation/references/replay.md).

Run the unit tests from the repository root:

```text
python -m unittest discover -s tests -v
```

For integration testing, Docker and the selected official image must already be available locally:

```text
python tests/integration.py --image postgres:18-bookworm
```

The integration runner uses disposable containers with synthetic data, no network, no published host ports and no host data mounts. It does not pull images automatically. Detailed scenarios, prerequisites and recorded results are in [performance validation](skills/postgres-performance-diagnostics/references/validation.md), [security validation](skills/postgres-security-review/references/validation.md) and [schema refactoring validation](skills/postgres-schema-review/references/refactoring.md).

## Repository layout

```text
.claude-plugin/          Claude Code plugin and marketplace manifests
skills/                  Five self-contained skills, scripts and references
example/en/              English prompts and example reports
example/ja/              Japanese prompts and example reports
example/inputs/workload/ Runnable synthetic comparison inputs
tests/                   Unit tests and isolated PostgreSQL scenarios
README.md                English guide
README_ja.md             Japanese guide
```

## License

[MIT License](LICENSE)
