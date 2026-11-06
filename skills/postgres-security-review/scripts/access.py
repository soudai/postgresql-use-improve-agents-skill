"""Role reachability, ACL, RLS and SECURITY DEFINER review."""
import re
from collections import defaultdict, deque

ROLE_FLAGS = ("superuser", "bypassrls", "createrole", "createdb", "replication")
HOST_ROLES = {"pg_read_server_files", "pg_write_server_files", "pg_execute_server_program"}
BROAD_ROLES = HOST_ROLES | {"pg_read_all_data", "pg_write_all_data", "pg_monitor",
                            "pg_read_all_stats", "pg_signal_backend"}


def object_key(row):
    return [row["kind"], row["schema"], row["name"], row.get("signature", "")]


def role_finding(review, rule, subject, evidence, expected, severity="high"):
    review.add(rule, subject, "finding", severity, evidence, expected,
               "Separate runtime, migration, owner and operations roles. Narrow only the grant paths in scope.",
               "Use fresh application and admin connections. Verify required operations and denied operations, including new objects.",
               "Record grantor, grant/admin options, membership flags, ACLs and ownership. Restore the scoped prior state if needed.")


class Access:
    def __init__(self, review):
        self.roles = {r["oid"]: r for r in review.rows("roles")}
        self.names = {r["name"]: oid for oid, r in self.roles.items()}
        self.edges = defaultdict(list)
        for edge in review.rows("memberships"):
            self.edges[edge["member"]].append(edge)
        self.acl = defaultdict(list)
        for row in review.rows("acl"):
            self.acl[tuple(object_key(row))].append(row)
        self.owner_role = self.names.get("pg_database_owner")
        self.db_owner = next((r["owner"] for r in review.rows("acl") if r["kind"] == "database"), None)

    def name(self, oid):
        return "PUBLIC" if oid == 0 else self.roles.get(oid, {}).get("name", str(oid))

    def walk(self, start, option):
        paths = {start: [start]}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            targets = [e["role"] for e in self.edges[current] if e[option]]
            if option == "inherit_option" and current == self.db_owner and self.owner_role:
                targets.append(self.owner_role)
            for target in targets:
                if target not in paths:
                    paths[target] = paths[current] + [target]
                    queue.append(target)
        return paths

    def path(self, path):
        return " → ".join(self.name(oid) for oid in path)

    def grants(self, key, actor, privilege, include_column=False):
        inherited = self.walk(actor, "inherit_option")
        rows = list(self.acl.get(tuple(key), []))
        if key[0] == "column":
            rows.extend(self.acl.get(("relation", *key[1:3], ""), []))
        if include_column and key[0] == "relation":
            rows.extend(row for obj, rows in self.acl.items()
                        if obj[:3] == ("column", *key[1:3]) for row in rows)
        return [r for r in rows if r.get("privilege") == privilege
                and (r["grantee"] == 0 or r["grantee"] in inherited)]

    def builtin(self, key, actor, privilege):
        groups = {self.name(oid) for oid in self.walk(actor, "inherit_option")}
        read = "pg_read_all_data" in groups
        write = "pg_write_all_data" in groups
        kind = key[0]
        if read and privilege == "SELECT" and kind in {"relation", "column", "sequence"}:
            return "pg_read_all_data"
        if write and privilege in {"INSERT", "UPDATE", "DELETE"} and kind in {"relation", "column"}:
            return "pg_write_all_data"
        if write and privilege == "UPDATE" and kind == "sequence":
            return "pg_write_all_data"
        if (read or write) and kind == "schema" and privilege == "USAGE":
            return "pg_read_all_data" if read else "pg_write_all_data"
        return None

    def can(self, key, actor, privilege, include_column=False):
        # Ownership grants control of the ACL even when the ordinary grant was revoked.
        # Check ordinary operation privileges here. Ownership control is reported separately.
        return self.roles.get(actor, {}).get("superuser") or self.builtin(key, actor, privilege) or bool(
            self.grants(key, actor, privilege, include_column))


def roles(review, access):
    settings = review.contract.get("runtime_roles", {})
    if not settings or not review.ready("roles", "memberships", "acl"):
        review.missing("ROLE", "runtime roles", "Runtime role contract and complete role, membership and ACL sections")
        return
    owned = defaultdict(list)
    for rows in access.acl.values():
        row = rows[0]
        if row["kind"] != "column":
            owned[row["owner"]].append(object_key(row))
    for name, spec in settings.items():
        actor = access.names.get(name)
        if actor is None:
            review.missing("ROLE", name, "Declared runtime role exists in the target")
            continue
        inherited = access.walk(actor, "inherit_option")
        switched = access.walk(actor, "set_option")
        power, unverified = [], []
        for target, path in switched.items():
            attrs = access.roles.get(target, {})
            absent = [flag for flag in ROLE_FLAGS if type(attrs.get(flag)) is not bool]
            if absent:
                unverified.append({"role": access.name(target), "path": access.path(path), "attributes": absent})
            for flag in ROLE_FLAGS:
                if attrs.get(flag) is True and flag not in spec.get("allow_flags", []):
                    power.append(f"{access.path(path)}: {flag}")
        # Built-in groups can confer ordinary privileges through inheritance, or after SET ROLE.
        for target, path in switched.items():
            for group, suffix in access.walk(target, "inherit_option").items():
                group_name = access.name(group)
                if group_name in BROAD_ROLES and group_name not in spec.get("allow_groups", []):
                    power.append(f"{access.path(path + suffix[1:])}: {group_name}")
        if power:
            evidence = ({"capabilities": sorted(set(power)), "unverified_attributes": unverified}
                        if unverified else sorted(set(power)))
            role_finding(review, "ROLE", name, evidence,
                         "Runtime role has only explicitly allowed administrative capabilities.")
        elif unverified:
            review.missing("ROLE", name,
                           "Observed administrative attributes for the login and each reachable SET ROLE target",
                           {"unverified_attributes": unverified})
        elif not spec.get("allow_flags") and not spec.get("allow_groups"):
            review.add("ROLE", name, "pass", "info", "No reviewed administrative capability found.",
                       "No administrative capability required.", "Retain role separation.",
                       "Verify with the application login.", "No change proposed.")
        owners = set(inherited)
        for target in switched:
            owners.update(access.walk(target, "inherit_option"))
        if not spec.get("allow_ownership"):
            for owner in sorted(owners & owned.keys()):
                role_finding(review, "OWNER", [name, access.name(owner)],
                             {"objects": owned[owner][:25], "count": len(owned[owner])},
                             "Object ownership separated from runtime access.")
        for edge in review.rows("memberships"):
            if edge["member"] in switched and edge["admin_option"] and access.name(edge["role"]) not in spec.get("allow_admin", []):
                role_finding(review, "ADMIN", [name, access.name(edge["role"]), access.name(edge["grantor"])],
                             access.path(switched[edge["member"]]), "No unapproved membership administration.")
        for spec_acl in spec.get("privileges", []):
            key, privilege, expected = spec_acl["object"], spec_acl["privilege"], spec_acl["allowed"]
            subject = [name, key, privilege]
            if tuple(key) not in access.acl:
                review.missing("ACL", subject, "Object and ACL visible in the target database")
                continue
            matched = []
            include_column = not expected and privilege in {"SELECT", "INSERT", "UPDATE", "REFERENCES"}
            for target, path in switched.items():
                if access.roles.get(target, {}).get("superuser"):
                    matched.append({"set_role": access.path(path), "privilege": "superuser"})
                builtin = access.builtin(key, target, privilege)
                if builtin:
                    matched.append({"set_role": access.path(path), "privilege": builtin})
                for r in access.grants(key, target, privilege, include_column):
                    matched.append({"set_role": access.path(path), "grantee": access.name(r["grantee"]),
                                    "grantor": access.name(r["grantor"]), "grant_option": r["is_grantable"],
                                    "column": r["signature"] if r["kind"] == "column" else None})
            # Required access is checked at the declared login context. A possible SET
            # ROLE route matters for denied access, but does not make an app operation work.
            current = access.can(key, actor, privilege)
            require_no_grant = not spec_acl.get("grantable", False)
            excessive = require_no_grant and any(r.get("grant_option") for r in matched)
            if (expected and (not current or excessive)) or (not expected and matched):
                role_finding(review, "ACL", subject, matched,
                             "Privilege and grant option match the scoped access contract.")
            else:
                review.add("ACL", subject, "pass", "info", matched, "Scoped ACL matches contract.",
                           "Retain scoped grants.", "Verify actual operation, schema access and RLS.", "No change proposed.")
    for clause in review.contract.get("deny_default_grants", []):
        if not review.ready("default_acl"):
            review.missing("DEFAULT_ACL", "creator", "Creator-specific default grants")
            break
        owner, grantee = access.names.get(clause["creator"]), access.names.get(clause["grantee"])
        if clause["grantee"] == "PUBLIC":
            grantee = 0
        if owner is None or grantee is None:
            review.missing("DEFAULT_ACL", [clause["creator"], clause["grantee"]],
                           "Declared creator and grantee are visible in the target")
            continue
        # A schema-specific default grant is additive to the global default.
        # A missing global row uses PostgreSQL's built-in default.
        defaults = [r for r in review.rows("default_acl") if r["owner"] == owner and r["kind"] == clause["kind"]]
        grants = [r for r in defaults if r["grantee"] == grantee and r["privilege"] == clause["privilege"]
                  and r["schema"] in (None, clause.get("schema"))]
        builtin = (grantee == 0 and (clause["kind"], clause["privilege"]) in
                   {("f", "EXECUTE"), ("T", "USAGE")} and not any(r["schema"] is None for r in defaults))
        subject = [clause["creator"], clause.get("schema"), clause["kind"], clause["grantee"], clause["privilege"]]
        expected = "Future objects created by the declared creator have no prohibited grant."
        if grants or builtin:
            role_finding(review, "DEFAULT_ACL", subject, "Built-in PUBLIC default" if builtin and not grants else grants,
                         expected)
        else:
            review.add("DEFAULT_ACL", subject, "pass", "info", "No prohibited default grant in scope.", expected,
                       "Retain creator-specific defaults.", "Verify a new object created by this role.", "No change proposed.")


OPS = {"select": "r", "insert": "a", "update": "w", "delete": "d"}


def rls(review, access):
    targets = review.contract.get("rls", [])
    if not targets or not review.ready("roles", "memberships", "acl", "relations", "policies", "partitions", "view_edges"):
        review.missing("RLS", "protected relations", "Protected-table contract, roles, policies and access paths")
        return
    relations = {(r["schema"], r["name"]): r for r in review.rows("relations")}
    by_oid = {r["oid"]: r for r in relations.values()}
    edges = defaultdict(set)
    for edge in review.rows("view_edges"):
        edges[edge["relation"]].add(edge["view"])
    routine_edges = defaultdict(set)
    for edge in review.rows("routine_edges"):
        routine_edges[edge["relation"]].add(edge["routine"])
    routines = {routine["oid"]: routine for routine in review.rows("routines")}
    if not review.ready("routine_edges", "routines", "policy_routines"):
        review.missing("RLS_PATH", "routines", "Catalog evidence for policy and routine references")
    children = defaultdict(set)
    for edge in review.rows("partitions"):
        children[edge["parent"]].add(edge["child"])
    for spec in targets:
        key = (spec["schema"], spec["table"])
        rel = relations.get(key)
        if rel is None or rel["kind"] not in {"r", "p"}:
            review.missing("RLS", list(key), "Protected table or partitioned table is visible")
            continue
        if not rel["rls"]:
            rls_finding(review, "RLS", list(key), "finding", "Declared protected relation has RLS disabled.")
        else:
            review.add("RLS", list(key), "pass", "info", "RLS flag is enabled.",
                       "Declared protected relation has RLS enabled.",
                       "Review policy behavior and all access paths independently.",
                       "Run the same-role tenant matrix.", "No change proposed.")
        if not spec.get("roles"):
            review.missing("RLS_POLICY", list(key), "Expected roles and operations for the protected relation")
        policies = [p for p in review.rows("policies") if p["relation"] == rel["oid"]]
        descendants = reachable(children, rel["oid"])
        views = reachable(edges, rel["oid"])
        for name, operations in spec.get("roles", {}).items():
            actor = access.names.get(name)
            if actor is None:
                review.missing("RLS", [*key, name], "Declared application role exists")
                continue
            inherited = access.walk(actor, "inherit_option")
            for effective, path in access.walk(actor, "set_option").items():
                if bypasses(access, effective, rel):
                    rls_finding(review, "RLS_BYPASS", [*key, name, access.name(effective)],
                                "finding", f"Effective role can bypass policies through {access.path(path)}.")
                for oid in sorted(descendants):
                    child = by_oid.get(oid)
                    if child is None or not access.can(
                        ["schema", child["schema"], child["schema"], ""], effective, "USAGE"
                    ):
                        continue
                    for op in operations:
                        if not access.can(["relation", child["schema"], child["name"], ""],
                                          effective, op.upper(), True):
                            continue
                        subject = [*key, name, child["schema"], child["name"], access.name(effective), op]
                        if not child["rls"]:
                            rls_finding(review, "RLS_PATH", subject, "finding",
                                        "Direct child access is granted with child RLS disabled.",
                                        source="https://www.postgresql.org/docs/current/ddl-inherit.html")
                        elif bypasses(access, effective, child):
                            rls_finding(review, "RLS_PATH", subject, "finding",
                                        f"Direct child access bypasses child policies through {access.path(path)}.",
                                        source="https://www.postgresql.org/docs/current/ddl-inherit.html")
                        else:
                            child_policies = [p for p in review.rows("policies") if p["relation"] == oid]
                            status, evidence = policy_scope(child_policies, access.walk(effective, "inherit_option"), op)
                            rls_finding(review, "RLS_PATH", subject, status,
                                        "Direct child access: " + evidence,
                                        source="https://www.postgresql.org/docs/current/ddl-inherit.html")
                for oid in sorted(views):
                    view = by_oid.get(oid)
                    if not view:
                        continue
                    if (view["kind"] == "m" or not view["invoker"]) and access.can(
                        ["relation", view["schema"], view["name"], ""], effective, "SELECT", True
                    ):
                        bypass = view["kind"] == "v" and oid in edges[rel["oid"]] and bypasses(
                            access, view["owner"], rel)
                        status = "finding" if bypass else "unknown"
                        rls_finding(review, "RLS_PATH", [*key, name, view["schema"], view["name"]],
                                    status, "Reachable view uses owner privileges or stored rows. Verify the view's own filters.")
                for oid in sorted(routine_edges[rel["oid"]]):
                    routine = routines.get(oid)
                    if routine is None or not bypasses(access, routine["owner"], rel):
                        continue
                    schema = routine["schema"]
                    routine_key = ["routine", schema, routine["name"], routine["signature"]]
                    if access.can(routine_key, effective, "EXECUTE") and access.can(
                        ["schema", schema, schema, ""], effective, "USAGE"
                    ):
                        rls_finding(review, "RLS_PATH", [*key, name, routine_key], "finding",
                                    "Callable SECURITY DEFINER references this table as an RLS bypass role. Verify its filters.",
                                    source="https://www.postgresql.org/docs/current/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY")
            if not rel["rls"] or bypasses(access, actor, rel):
                continue
            for op in operations:
                status, evidence = policy_scope(policies, inherited, op)
                # Default deny is safe for an incidental child route, but blocks a
                # declared operation on the protected target itself.
                if status == "pass":
                    rls_finding(review, "RLS_POLICY", [*key, name, op], "finding",
                                "Default deny prevents the declared operation. This does not expose rows.", severity="medium")
                else:
                    rls_finding(review, "RLS_POLICY", [*key, name, op], status, evidence)


def policy_scope(policies, inherited, op):
    applicable = [p for p in policies if p["command"] in ("*", OPS[op])
                  and (0 in p["roles"] or set(p["roles"]) & inherited.keys())]
    permissive = [p for p in applicable if p["permissive"]]
    if not permissive:
        return "pass", "No applicable permissive policy; default deny blocks this operation."
    # Permissive policies combine with OR; restrictive policies combine with AND.
    # Do not infer arbitrary predicates or their functions from metadata.
    if (any(unfiltered(p, op) for p in permissive)
            and all(unfiltered(p, op) for p in applicable if not p["permissive"])):
        return "finding", ("An applicable permissive policy is unfiltered and no applicable restrictive policy "
                           "narrows it. Verify the concrete statement, including SELECT policies for writes.")
    if any(p["session_setting"] for p in applicable):
        return "unknown", ("Policy reads a session setting. Verify who can set tenant identity and reset pooled "
                           "connections for this relation, effective role and operation.")
    return "unknown", ("Applicable predicates exist. Metadata cannot prove tenant isolation; run the scoped "
                       "tenant CRUD matrix for this relation and effective role.")


def unfiltered(policy, op):
    using = not policy["has_using"] or policy["using_true"]
    check = policy["check_true"] if policy["has_check"] else using
    return using if op in {"select", "delete"} else check if op == "insert" else using and check


def reachable(edges, root):
    seen = {root}
    queue = deque([root])
    while queue:
        for target in edges[queue.popleft()] - seen:
            seen.add(target)
            queue.append(target)
    return seen - {root}


def bypasses(access, actor, rel):
    attrs = access.roles.get(actor, {})
    return attrs.get("superuser") or attrs.get("bypassrls") or (
        not rel["force"] and rel["owner"] in access.walk(actor, "inherit_option"))


def rls_finding(review, rule, subject, status, evidence, severity="high", source=None):
    review.add(rule, subject, status, severity if status == "finding" else "unrated", evidence,
               "Only expected tenants and operations are permitted.",
               "Separate owner and runtime roles. Correct grants, policy predicates or access paths as applicable.",
               "In staging exercise A/B tenants, missing context, write checks, views, partitions and pooled reuse.",
               "Record policy, grants and owner first. Preserve containment if restoring the previous state would expose rows.",
               confidence="candidate" if rule in {"RLS_PATH", "RLS_POLICY"} else "observed", source=source)


PATH_ITEM = re.compile(r'\s*(?:"((?:[^"]|"")*)"|([A-Za-z_][A-Za-z_0-9$]*))\s*(,|$)')


def search_path(value):
    if value is None:
        return None
    if not value.strip():
        return []
    items = []
    pos = 0
    while pos < len(value):
        match = PATH_ITEM.match(value, pos)
        if match is None:
            return None
        items.append(match[1].replace('""', '"') if match[1] is not None else match[2].lower())
        pos = match.end()
        if match[3] and not value[pos:].strip():
            return None
    return items


def definer(review, access):
    if not review.contract.get("runtime_roles") or not review.ready("routines", "acl", "roles", "memberships"):
        review.missing("DEFINER", "routine reachability", "Complete routines, ACL and runtime role contract")
        return
    for routine in review.rows("routines"):
        key = ["routine", routine["schema"], routine["name"], routine["signature"]]
        path = search_path(routine["search_path"])
        if key[1:] in review.contract.get("restrict_public_routines", []):
            public = any(row["grantee"] == 0 and row["privilege"] == "EXECUTE"
                         for row in access.acl.get(tuple(key), []))
            if public:
                role_finding(review, "PUBLIC_EXECUTE", key, "PUBLIC has EXECUTE on this routine.",
                             "Execution is restricted to named callers.")
            else:
                review.add("PUBLIC_EXECUTE", key, "pass", "info", "No PUBLIC EXECUTE grant.",
                           "Execution is restricted to named callers.", "Retain scoped EXECUTE grants.",
                           "Check callers and default privileges.", "No change proposed.")
        owner = access.name(routine["owner"])
        for name in review.contract["runtime_roles"]:
            actor = access.names.get(name)
            if actor is None:
                continue
            for effective, route in access.walk(actor, "set_option").items():
                if not access.can(key, effective, "EXECUTE"):
                    continue
                # The caller must also be able to resolve the routine through its schema.
                if not access.can(["schema", key[1], key[1], ""], effective, "USAGE"):
                    continue
                writable = sorted(s for s in path or [] if s not in {"pg_catalog", "pg_temp", "$user"}
                                  and (access.can(["schema", s, s, ""], effective, "CREATE")
                                       or any(r["owner"] in access.walk(effective, "inherit_option")
                                              for r in access.acl.get(("schema", s, s, ""), []))))
                owner_differs = routine["owner"] != effective
                has_temp = access.can(["database", "", review.scope["database"], ""], effective, "TEMPORARY")
                unsafe_temp = has_temp and (path is None or not path or path[-1] != "pg_temp")
                evidence = {"caller": access.path(route), "owner": owner, "writable_schemas": writable,
                            "pinned_path_parsed": path is not None, "temporary_lookup_risk": bool(unsafe_temp)}
                status = "finding" if owner_differs and (writable or unsafe_temp) else "unknown"
                review.add("DEFINER", [*key, name, access.name(effective)], status,
                           "high" if status == "finding" else "unrated", evidence,
                           "Callable routines resolve objects only in trusted schemas and use only required owner privileges.",
                           "Qualify references, restrict EXECUTE and owner privileges, and pin a trusted path with pg_temp last.",
                           "Check body and dynamic SQL privately. Reproduce shadowing with synthetic objects and test legitimate callers.",
                           "Save prior owner, ACL and routine settings. Keep access contained during recovery.",
                           confidence="candidate")
