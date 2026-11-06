# Assess security updates and upgrade readiness

[日本語](../ja/06-upgrade-readiness.md) · [Examples](README.md)

This is a synthetic review scenario and an illustrative report, not current
release guidance, a real vulnerability advisory or exact script output.
The versions below are comparison inputs, not recommended update targets.

## Scenario and supplied evidence

A service uses a vendor build and wants to distinguish upstream update status
from vendor security fixes. It also needs a recoverable major-version migration.
The evaluation time in this example is 2026-11-06T01:00:00Z.

| Input supplied by the user | Relevant evidence |
| --- | --- |
| `security-snapshot.json` | Target `node-a`, database `app`; `server_version=17.11` and `server_version_num=170011`. |
| `deployment-contract.json` | The observed running build is the fictional `17.11-example.7`. The distribution is vendor-managed, not asserted to be an unmodified upstream build. |
| `upstream-advisories.json` | For illustration, the supplied comparison data lists minor 17.12 and an advisory whose upstream fix is in 17.12. Actual use requires verified official source URLs, retrieval time and complete release/advisory records. |
| `vendor-patch-review.md` | A hypothetical scoped review matches that one advisory to a backport in the exact running build. No real vendor or source URL is supplied in this example. |
| `migration-rehearsal.md` | A logical migration rehearsal from 17 to 18 accepts 10 new orders on the new side after cutover. Those writes are absent on the old side; no reverse synchronization exists. |
| `recovery-contract.md` | Required RPO is 0 accepted orders lost. Required RTO is 30 minutes; restoration/replay time has not yet been measured. |

“Advisory A” below is a narrative label, not a real CVE or a valid substitute
for an advisory identifier in machine input. The vendor-backport conclusion is
conditional on authentic, scoped published evidence in an actual review.

These filenames are placeholders for your exports and review documents, not
bundled input files. Do not fabricate official or vendor evidence to reproduce
the example. An offline variant omits the current advisory input or has only
a cache retrieved 10 days before the evaluation time.

## Prompt to copy

```text
$postgres-security-review

Review the supplied security-snapshot.json, deployment-contract.json,
upstream-advisories.json, vendor-patch-review.md, migration-rehearsal.md
and recovery-contract.md at 2026-11-06T01:00:00Z.

Separate upstream minor freshness, community support, vendor support and each
advisory's applicability/fix status. Match any backport claim to the running
package/build and its published evidence; do not infer all fixes from one claim.
If evidence is missing or stale, preserve unknowns and list what to refresh.

Compare a supported minor update with a major migration using the actual
release notes, extension/client compatibility, downtime and recovery needs.
For the migration rehearsal, account for the 10 new-side orders absent from
the old side and the RPO 0 / RTO 30-minute requirements.

Return an evidence-based report and a staged change/recovery plan, including
stop conditions and acceptance tests. Do not treat switching the endpoint back
as a complete rollback after new-side writes.
```

## Illustrative result

| Question | Illustrative assessment |
| --- | --- |
| Is the upstream minor current? | In the supplied comparison, 17.11 is older than 17.12. Review the actual supported target and its release notes before scheduling an update. |
| Is advisory A fixed? | Conditionally fixed for the exact vendor build if the authentic published backport evidence matches. The older upstream minor alone does not contradict that scoped result. |
| Are all vulnerabilities addressed? | Unknown. One scoped backport conclusion does not cover other advisories, extensions, clients or configuration exposure. |
| What does the offline variant show? | Current release and advisory status are unknown when the feed is missing or 10 days old. No supplied evidence is not equivalent to “no vulnerabilities.” |
| Is endpoint-only rollback acceptable? | No. The 10 accepted orders would be absent on the old side, violating RPO 0. |
| Has recovery met the time budget? | Unknown. The 30-minute RTO needs a measured recovery rehearsal. |

A current feed and a verified backport can support different conclusions:
“review a newer upstream minor” and “this specific advisory is already fixed”
can both be true. Community support and any vendor extension of support need
their own dated evidence. A final minor release of an unsupported major is not
automatically a suitable destination.

## Next verification steps

1. Confirm running package/image/engine revision, version consistency and target
   identity. Refresh official release/security data and verify the vendor's
   exact advisory, affected component, build and support evidence.
2. Check candidate release notes and extension, driver, pooler and HA support.
   Select the actual supported update/migration route from these materials.
3. Restore a backup into a separate environment. Test data meaning, application
   and management connections, roles, ACLs, RLS and denied operations.
4. Before cutover, define write stop/drain, synchronization checks and stop
   conditions. Preserve all new-side accepted writes with a tested replay or
   reverse-synchronization strategy before considering a return.
5. After a failure, choose forward repair, restore/replay or resynchronization
   according to the migration method. For `pg_upgrade`, review the selected
   transfer mode: link and swap impose additional old-cluster reuse limits.
6. Rehearse the chosen recovery and account for all 10 orders. Measure whether
   it meets RPO 0 and RTO 30 minutes; rerun representative workload checks and
   retain dated evidence before declaring the change ready.

## References

- [Security review skill](../../skills/postgres-security-review/SKILL.md)
- [Evidence, advisory freshness and vendor contracts](../../skills/postgres-security-review/references/evidence.md)
- [Versions, migration and recovery](../../skills/postgres-security-review/references/changes.md)
- [Workload validation skill](../../skills/postgres-workload-validation/SKILL.md)
