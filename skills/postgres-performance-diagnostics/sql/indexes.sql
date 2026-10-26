-- idx_scan=0 is not evidence that an index is safe to drop.
-- indexdef can contain SQL literals: collect.py removes it unless explicitly requested.
SELECT s.*,
       ic.relfilenode,
       tc.relfilenode AS table_relfilenode,
       ic.relkind,
       ic.reloptions,
       am.amname AS access_method,
       i.indisunique,
       i.indisprimary,
       i.indisexclusion,
       i.indisvalid,
       i.indisready,
       i.indislive,
       i.indnkeyatts,
       i.indnatts,
       i.indkey::text AS indkey,
       i.indclass::text AS indclass,
       i.indcollation::text AS indcollation,
       i.indoption::text AS indoption,
       i.indexprs IS NOT NULL AS has_expressions,
       i.indpred IS NOT NULL AS is_partial,
       CASE WHEN ic.relkind = 'i'
            THEN pg_catalog.pg_relation_size(ic.oid) END AS index_bytes,
       pg_catalog.pg_get_indexdef(ic.oid) AS indexdef,
       ARRAY(SELECT con.conname::text
             FROM pg_catalog.pg_constraint AS con
             WHERE con.conindid = i.indexrelid
             ORDER BY con.oid) AS constraint_names
FROM pg_catalog.pg_stat_user_indexes AS s
JOIN pg_catalog.pg_index AS i ON i.indexrelid = s.indexrelid
JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
JOIN pg_catalog.pg_class AS tc ON tc.oid = i.indrelid
JOIN pg_catalog.pg_am AS am ON am.oid = ic.relam
ORDER BY s.indexrelid;
