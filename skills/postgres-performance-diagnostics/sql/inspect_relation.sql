-- Targeted planner evidence, intentionally outside the automatic collection manifest.
-- psql -X -qAt --set=relation=public.orders --file sql/inspect_relation.sql
-- Supply a schema-qualified relation (quote case-sensitive identifiers inside the value).
-- :'relation' is a psql-quoted SQL literal; never change it to raw :relation interpolation.
-- This query omits data values, SQL expressions, predicates, defaults, and histogram bounds.
\set ON_ERROR_STOP on
\if :{?include_partition_bounds}
\else
\set include_partition_bounds false
\endif
BEGIN READ ONLY;
SET LOCAL search_path = pg_catalog;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '1s';
SET LOCAL timezone = 'UTC';

WITH target AS MATERIALIZED (
    SELECT c.*, n.nspname
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE c.oid = :'relation'::pg_catalog.regclass
), partition_members AS MATERIALIZED (
    SELECT tree.relid, tree.parentrelid, tree.isleaf, tree.level
    FROM target AS t
    CROSS JOIN LATERAL pg_catalog.pg_partition_tree(pg_catalog.pg_partition_root(t.oid)) AS tree
    LIMIT 501
), partition_details AS (
    SELECT m.relid, m.parentrelid, m.isleaf, m.level, n.nspname AS schema_name, c.relname,
           c.reltuples AS estimated_rows, pg_catalog.pg_relation_size(c.oid) AS heap_bytes,
           st.last_analyze, st.last_autoanalyze, st.n_mod_since_analyze,
           p.partstrat AS partition_strategy, p.partattrs::text AS partition_key_attributes,
           p.partexprs IS NOT NULL AS expression_key,
           CASE WHEN :'include_partition_bounds'::boolean
                THEN pg_catalog.pg_get_expr(c.relpartbound, c.oid) END AS bound,
           CASE WHEN :'include_partition_bounds'::boolean AND p.partrelid IS NOT NULL
                THEN pg_catalog.pg_get_partkeydef(c.oid) END AS partition_key,
           (SELECT count(*) FROM pg_catalog.pg_index AS i WHERE i.indrelid = c.oid) AS indexes,
           (SELECT count(*) FROM pg_catalog.pg_index AS i WHERE i.indrelid = c.oid
                 AND NOT (i.indisvalid AND i.indisready)) AS invalid_or_unready_indexes
    FROM (SELECT * FROM partition_members LIMIT 500) AS m
    JOIN pg_catalog.pg_class AS c ON c.oid = m.relid
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    LEFT JOIN pg_catalog.pg_partitioned_table AS p ON p.partrelid = c.oid
    LEFT JOIN pg_catalog.pg_stat_all_tables AS st ON st.relid = c.oid
), columns AS (
    SELECT a.attnum, a.attname,
           pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
           a.attnotnull, a.attidentity, a.attgenerated,
           a.attstattarget AS statistics_target,
           CASE WHEN a.attstattarget < 0
                THEN pg_catalog.current_setting('default_statistics_target')::integer
                ELSE a.attstattarget END AS effective_statistics_target,
           pg_catalog.has_column_privilege(t.oid, a.attnum, 'SELECT') AS select_permitted,
           CASE
             WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_stats AS s
                          WHERE s.schemaname = t.nspname AND s.tablename = t.relname
                            AND s.attname = a.attname) THEN 'visible'
             WHEN NOT pg_catalog.has_column_privilege(t.oid, a.attnum, 'SELECT')
               THEN 'not_visible_no_select'
             ELSE 'not_present_or_filtered'
           END AS stats_status,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'inherited', s.inherited, 'null_frac', s.null_frac,
               'avg_width', s.avg_width, 'n_distinct', s.n_distinct,
               'correlation', s.correlation, 'most_common_freqs', s.most_common_freqs
           ) ORDER BY s.inherited)
           FROM pg_catalog.pg_stats AS s
           WHERE s.schemaname = t.nspname AND s.tablename = t.relname
             AND s.attname = a.attname), '[]'::jsonb) AS statistics
    FROM target AS t
    JOIN pg_catalog.pg_attribute AS a ON a.attrelid = t.oid
    WHERE a.attnum > 0 AND NOT a.attisdropped
), indexes AS (
    SELECT i.indexrelid, ic.relname AS index_name,
           am.amname AS access_method, ic.relfilenode,
           i.indisunique, i.indisprimary, i.indisexclusion,
           i.indisvalid, i.indisready, i.indislive,
           i.indnkeyatts, i.indnatts,
           i.indkey::text AS attribute_numbers,
           i.indclass::text AS operator_class_oids,
           i.indcollation::text AS collation_oids,
           i.indoption::text AS column_options,
           i.indexprs IS NOT NULL AS has_expressions,
           i.indpred IS NOT NULL AS has_predicate
    FROM target AS t
    JOIN pg_catalog.pg_index AS i ON i.indrelid = t.oid
    JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
    JOIN pg_catalog.pg_am AS am ON am.oid = ic.relam
), constraints AS (
    SELECT con.oid, con.conname AS constraint_name, con.contype AS constraint_type,
           con.conrelid AS relation_oid, NULLIF(con.confrelid, 0) AS referenced_relation_oid,
           con.conkey AS column_numbers, con.confkey AS referenced_column_numbers,
           con.conindid AS supporting_index_oid,
           con.convalidated, con.condeferrable, con.condeferred,
           con.conbin IS NOT NULL AS has_expression
    FROM target AS t
    JOIN pg_catalog.pg_constraint AS con ON con.conrelid = t.oid OR con.confrelid = t.oid
), extended_statistics AS (
    SELECT e.oid, n.nspname AS statistics_schema, e.stxname AS statistics_name,
           e.stxkeys::text AS attribute_numbers, e.stxkind AS kinds,
           e.stxstattarget AS statistics_target,
           e.stxexprs IS NOT NULL AS has_expressions,
           CASE
             WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_stats_ext AS s
                          WHERE s.statistics_schemaname = n.nspname
                            AND s.statistics_name = e.stxname) THEN 'visible'
             WHEN NOT pg_catalog.pg_has_role(t.relowner, 'USAGE')
               THEN 'not_visible_not_owner'
             ELSE 'not_present_or_filtered'
           END AS stats_status,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'inherited', s.inherited, 'n_distinct', s.n_distinct,
               'dependencies', s.dependencies
           ) ORDER BY s.inherited)
           FROM pg_catalog.pg_stats_ext AS s
           WHERE s.statistics_schemaname = n.nspname
             AND s.statistics_name = e.stxname), '[]'::jsonb) AS statistics
    FROM target AS t
    JOIN pg_catalog.pg_statistic_ext AS e ON e.stxrelid = t.oid
    JOIN pg_catalog.pg_namespace AS n ON n.oid = e.stxnamespace
)
SELECT jsonb_build_object(
    'schema_version', 1,
    'collected_at', clock_timestamp(),
    'server_version_num', current_setting('server_version_num')::integer,
    'database', current_database(),
    'relation', jsonb_build_object(
        'oid', t.oid, 'schema', t.nspname, 'name', t.relname,
        'kind', t.relkind, 'relfilenode', t.relfilenode,
        'is_partition', t.relispartition, 'persistence', t.relpersistence,
        'estimated_rows', t.reltuples, 'estimated_pages', t.relpages,
        'options', t.reloptions, 'row_security', t.relrowsecurity,
        'force_row_security', t.relforcerowsecurity),
    'visibility', jsonb_build_object(
        'effective_role', current_user,
        'relation_select_permitted', has_table_privilege(t.oid, 'SELECT'),
        'any_column_select_permitted', has_any_column_privilege(t.oid, 'SELECT'),
        'table_owner_role_available', pg_has_role(t.relowner, 'USAGE'),
        'note', 'Empty statistics can mean not analyzed, disabled statistics, privilege/RLS filtering, or an inapplicable statistical kind; they do not prove that no statistics exist.'),
    'partition_tree', jsonb_build_object(
        'truncated', (SELECT count(*) > 500 FROM partition_members),
        'bounds_included', :'include_partition_bounds'::boolean,
        'members', COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.level, p.relid)
                            FROM partition_details AS p), '[]'::jsonb)),
    'columns', COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.attnum)
                         FROM columns AS c), '[]'::jsonb),
    'indexes', COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.indexrelid)
                         FROM indexes AS i), '[]'::jsonb),
    'constraints', COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.oid)
                             FROM constraints AS c), '[]'::jsonb),
    'extended_statistics', COALESCE((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.oid)
                                     FROM extended_statistics AS e), '[]'::jsonb),
    'omitted', jsonb_build_array('most_common_vals', 'histogram_bounds',
        'column_defaults', 'index_expressions', 'index_predicates',
        'constraint_expressions', 'extended_statistics_expressions')
)
FROM target AS t;
COMMIT;
