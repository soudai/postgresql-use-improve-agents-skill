# Review application roles, RLS and authentication

[日本語](../ja/05-security-review.md) · [Examples](README.md)

This is a synthetic scenario and an illustrative report. The observations below
are invented for explanation; they are not live checks or exact script output.

## Scenario and supplied evidence

A multi-tenant service needs tenant isolation for `app.orders` and its partition
`app.orders_2026_11`. The access contract separates runtime, migration and
administration. All supplied observations identify `node-a`, database `app` and
2026-11-06T01:00:00Z.

| Input supplied by the user | Relevant evidence |
| --- | --- |
| `security-snapshot.json` | The relevant role, membership, ACL, policy and partition sections are complete. Runtime role `app_web` can SET ROLE to `ops_debug`, which has BYPASSRLS. |
| `access-contract.json` | Runtime tenant CRUD is allowed; administration and BYPASSRLS are not. Direct child access must preserve the same tenant boundary as parent access. |
| `policy-review.md` | Parent access uses a tenant filter. The child has RLS enabled, schema/table grants permit direct SELECT by `app_web`, and its only applicable SELECT policy is permissive `USING (true)`. No restrictive SELECT policy applies. |
| `runtime-observations.json` | Unix PostgreSQL process UID is 999. The TLS key is owned by UID 0 with mode 0640, but effective groups, ACLs, parent-path access and authorized readers have not been checked. |
| `credential-review.md` | `app_web` uses SCRAM storage and has a timezone-qualified expiry of 2026-12-01T00:00:00Z. Generation, uniqueness and revocation evidence are missing. |

`policy-review.md` is a private, scoped administrator review; the normal collector
does not export arbitrary policy expressions. Its summary supplies the missing
policy meaning. Tenant CRUD and TLS connection tests have not yet run.

These filenames are placeholders for user-provided exports and contracts, not
bundled assets. Do not include passwords, verifiers, private keys or secret DSNs.

## Prompt to copy

```text
$postgres-security-review

Review the supplied security-snapshot.json, access-contract.json,
policy-review.md, runtime-observations.json and credential-review.md.
The target is node-a / app; evaluate at 2026-11-06T01:00:00Z.

Check app_web's effective privileges and SET ROLE routes. Review parent access
and direct access to app.orders_2026_11 separately, including applicable policies,
ACLs, owner/FORCE and BYPASSRLS. Distinguish a verified configuration issue from
an observed tenant-data disclosure.

Assess the TLS key using the real process identity and effective access.
Separate SCRAM format, authentication method, expiry and credential lifecycle;
do not infer password strength from its verifier format.

Produce a private report with findings, unknowns and evidence. Propose scoped
role/policy changes, staging acceptance checks and recovery conditions that
preserve a working administration route. Use the supplied evidence and prepare
a reviewable change plan.
```

## Illustrative result

| Item | Result | Reason and next action |
| --- | --- | --- |
| Runtime role boundary | `finding` | The observed SET ROLE path reaches an unapproved BYPASSRLS role. Identify users of that membership and plan its scoped removal or separation. |
| Direct child SELECT | `finding` | Enabled RLS does not provide the required tenant restriction on this route. Grants and the applicable policy permit unrestricted SELECT; actual cross-tenant row visibility remains to be tested. |
| TLS key access | `unknown` | Root ownership and 0640 can be valid, but these metadata do not prove service readability or exclude unauthorized readers. |
| Credential lifecycle | `unknown` | SCRAM format and the unexpired timestamp are known. Password generation, uniqueness and revocation readiness remain unverified. |
| TLS connection boundary | `unknown` | Key metadata does not demonstrate hostname/CA validation or refusal of plaintext connections. |

The running PostgreSQL process is non-root in the supplied observation.
A root-owned server key does not mean the server runs as root.
No claim is made that a weak password or a data disclosure has been observed.

The proposed remediation separates runtime and administrative roles and narrows
the direct-child route according to the access contract. A permissive policy
containing `true` would need a different assessment if an applicable restrictive
policy constrained it; arbitrary predicates may require further source review.

## Next verification steps

1. Preserve existing role memberships, owners, grants and policies. Check
   migration, monitoring and recovery dependencies before a scoped change.
2. In a disposable environment, test tenants A/B using the application login,
   SET ROLE behavior and pool. Test permitted CRUD, cross-tenant denial, missing
   tenant context and reuse of the same connection from A to B.
3. Repeat the checks through the parent, direct child, views and routines the
   application uses. Also exercise the newly restricted SET ROLE route.
4. Confirm the service's effective UID/groups, key ownership, ACLs and parent
   paths without reading the key. Record `service_readable=true` and
   `unauthorized_access=false` only after those checks support them.
5. Check credential lifecycle through the secret-management process. Test old
   credential rejection and new credential success when rotation is planned.
6. Verify valid CA/hostname connections, invalid identity and plaintext rejection,
   service restart and the retained administration route. Recollect evidence;
   recovery must not silently restore the exposure that prompted the change.

## References

- [Skill](../../skills/postgres-security-review/SKILL.md)
- [Collection and access contracts](../../skills/postgres-security-review/references/evidence.md)
- [Privileges and RLS](../../skills/postgres-security-review/references/access.md)
- [Authentication, TLS and runtime](../../skills/postgres-security-review/references/authentication.md)
- [Changes and recovery](../../skills/postgres-security-review/references/changes.md)
