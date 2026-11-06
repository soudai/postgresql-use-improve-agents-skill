"""Fixed catalog queries. No application objects or secret fields are selected."""
USER_NS = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)'"

# OIDs are stable only within the scope of a single snapshot.
OBJECTS = f"""
SELECT 'database' AS kind, '' AS schema, datname AS name, '' AS signature,
       oid, datdba AS owner, datacl AS acl, 'd'::"char" AS aclkind
FROM pg_database WHERE datname=current_database()
UNION ALL
SELECT 'schema', n.nspname, n.nspname, '', n.oid, n.nspowner, n.nspacl, 'n'
FROM pg_namespace n WHERE {USER_NS}
UNION ALL
SELECT CASE c.relkind WHEN 'S' THEN 'sequence' ELSE 'relation' END,
       n.nspname, c.relname, '', c.oid, c.relowner, c.relacl,
       CASE c.relkind WHEN 'S' THEN 's'::"char" ELSE 'r'::"char" END
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE {USER_NS} AND c.relkind IN ('r','p','v','m','f','S')
UNION ALL
SELECT 'column', n.nspname, c.relname, a.attname, c.oid, c.relowner, a.attacl, 'c'
FROM pg_attribute a JOIN pg_class c ON a.attrelid=c.oid
JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE {USER_NS} AND a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m','f')
UNION ALL
SELECT 'routine', n.nspname, p.proname, oidvectortypes(p.proargtypes),
       p.oid, p.proowner, p.proacl, 'f'
FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid WHERE {USER_NS}
"""


def queries(version):
    result = {
        "extensions": "SELECT extname AS name, extversion AS version FROM pg_extension ORDER BY extname",
        "databases": """SELECT datname AS name FROM pg_database
                        WHERE datallowconn AND NOT datistemplate ORDER BY datname""",
    }
    if version < 140000:
        return result
    inheritance = "inherit_option, set_option" if version >= 160000 else """
       (SELECT rolinherit FROM pg_roles WHERE oid=member) AS inherit_option, true AS set_option"""
    result.update({
        "roles": """SELECT oid::bigint AS oid, rolname AS name, rolcanlogin AS login, rolsuper AS superuser,
                    rolbypassrls AS bypassrls, rolcreaterole AS createrole,
                    rolcreatedb AS createdb, rolreplication AS replication,
                    rolvaliduntil AS valid_until FROM pg_roles ORDER BY oid""",
        "memberships": f"""SELECT roleid::bigint AS role, member::bigint AS member, grantor::bigint AS grantor, admin_option, {inheritance}
                           FROM pg_auth_members ORDER BY roleid, member, grantor""",
        "acl": f"""SELECT o.kind, o.schema, o.name, o.signature, o.oid::bigint AS oid, o.owner::bigint AS owner,
                     a.grantee::bigint AS grantee, a.grantor::bigint AS grantor, a.privilege_type AS privilege, a.is_grantable
                   FROM ({OBJECTS}) o
                   LEFT JOIN LATERAL aclexplode(coalesce(o.acl, acldefault(o.aclkind, o.owner))) a ON true
                   ORDER BY o.kind, o.schema, o.name, o.signature, a.grantee, a.grantor, a.privilege_type""",
        "default_acl": """SELECT d.defaclrole::bigint AS owner, n.nspname AS schema, d.defaclobjtype AS kind,
                            a.grantee::bigint AS grantee, a.grantor::bigint AS grantor, a.privilege_type AS privilege, a.is_grantable
                          FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
                          LEFT JOIN LATERAL aclexplode(d.defaclacl) a ON true
                          ORDER BY d.oid, a.grantee, a.grantor, a.privilege_type""",
        "relations": f"""SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name,
                         c.relkind AS kind, c.relowner::bigint AS owner, c.relrowsecurity AS rls,
                         c.relforcerowsecurity AS force,
                         coalesce('security_invoker=true'=ANY(c.reloptions),false) AS invoker,
                         coalesce('security_barrier=true'=ANY(c.reloptions),false) AS barrier
                         FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                         WHERE {USER_NS} AND c.relkind IN ('r','p','v','m','f') ORDER BY c.oid""",
        # Use node metadata for simple hints. Deparsing a user-defined constant
        # can invoke a type output function, so never deparse policy expressions.
        "policies": rf"""SELECT p.polrelid::bigint AS relation, p.polname AS name,
                         p.polroles::bigint[] AS roles, p.polcmd AS command, p.polpermissive AS permissive,
                         p.polqual IS NOT NULL AS has_using, p.polwithcheck IS NOT NULL AS has_check,
                         coalesce(p.polqual::text ~ '^\{{CONST :consttype 16 .*:constisnull false .*:constvalue 1 \[ 1 ',false) AS using_true,
                         coalesce(p.polwithcheck::text ~ '^\{{CONST :consttype 16 .*:constisnull false .*:constvalue 1 \[ 1 ',false) AS check_true,
                         EXISTS (SELECT 1 FROM pg_proc f
                                 WHERE f.pronamespace='pg_catalog'::regnamespace AND f.proname='current_setting'
                                   AND (p.polqual::text ~ (':funcid ' || f.oid || ' ')
                                     OR p.polwithcheck::text ~ (':funcid ' || f.oid || ' '))) AS session_setting
                         FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
                         JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {USER_NS}
                         ORDER BY p.polrelid,p.polname""",
        "view_edges": f"""SELECT DISTINCT v.oid::bigint AS view, d.refobjid::bigint AS relation
                           FROM pg_class v JOIN pg_namespace n ON n.oid=v.relnamespace
                           JOIN pg_rewrite r ON r.ev_class=v.oid
                           JOIN pg_depend d ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid
                           WHERE {USER_NS} AND v.relkind IN ('v','m') AND d.refclassid='pg_class'::regclass
                             AND d.refobjid<>v.oid ORDER BY view,relation""",
        "routine_edges": f"""SELECT DISTINCT p.oid::bigint AS routine, d.refobjid::bigint AS relation
                              FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                              JOIN pg_depend d ON d.classid='pg_proc'::regclass AND d.objid=p.oid
                              WHERE {USER_NS} AND p.prosecdef AND d.refclassid='pg_class'::regclass
                              ORDER BY routine,relation""",
        "policy_routines": f"""SELECT DISTINCT pol.polrelid::bigint AS relation, pol.polname AS policy,
                                n.nspname AS schema, p.proname AS routine, p.prosecdef AS security_definer
                               FROM pg_policy pol JOIN pg_depend d ON d.classid='pg_policy'::regclass AND d.objid=pol.oid
                               JOIN pg_proc p ON d.refclassid='pg_proc'::regclass AND d.refobjid=p.oid
                               JOIN pg_namespace n ON n.oid=p.pronamespace
                               WHERE {USER_NS} ORDER BY relation,policy,schema,routine,security_definer""",
        "partitions": """SELECT inhparent::bigint AS parent, inhrelid::bigint AS child
                          FROM pg_inherits JOIN pg_class ON oid=inhrelid
                          WHERE relispartition ORDER BY inhparent, inhrelid""",
        "routines": f"""SELECT p.oid::bigint AS oid, n.nspname AS schema, p.proname AS name,
                          oidvectortypes(p.proargtypes) AS signature, p.proowner::bigint AS owner,
                          (SELECT substr(v, length('search_path=') + 1)
                           FROM unnest(p.proconfig) v WHERE v LIKE 'search_path=%' LIMIT 1) AS search_path
                         FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                         WHERE {USER_NS} AND p.prosecdef ORDER BY p.oid""",
        "settings": """SELECT name, setting, source, pending_restart FROM pg_settings WHERE name IN
                        ('listen_addresses','ssl','ssl_min_protocol_version','password_encryption')
                        ORDER BY name""",
        "transport": """SELECT s.ssl, s.version AS tls_version, g.encrypted AS gss_encrypted
                         FROM (SELECT pg_backend_pid() AS pid) b
                         LEFT JOIN pg_stat_ssl s USING (pid) LEFT JOIN pg_stat_gssapi g USING (pid)""",
        "hba": """SELECT rule_number AS number, line_number AS line, type, database AS databases,
                    user_name AS users, address, netmask, auth_method AS method, error IS NOT NULL AS invalid
                  FROM pg_hba_file_rules ORDER BY rule_number NULLS LAST, line_number""",
        "passwords": """SELECT oid::bigint AS oid, CASE WHEN rolpassword IS NULL THEN 'none'
                         WHEN rolpassword LIKE 'SCRAM-SHA-256$%' THEN 'scram'
                         WHEN rolpassword ~ '^md5[0-9a-f]{32}$' THEN 'md5'
                         ELSE 'unknown' END AS format FROM pg_authid ORDER BY oid""",
    })
    if version >= 150000:
        result["parameter_acl"] = """SELECT parname AS name, a.grantee::bigint AS grantee, a.grantor::bigint AS grantor,
                                     a.privilege_type AS privilege, a.is_grantable
                                     FROM pg_parameter_acl CROSS JOIN LATERAL aclexplode(paracl) a
                                     ORDER BY parname,a.grantee,a.grantor,a.privilege_type"""
    if version < 160000:
        result.pop("hba")
    return result
