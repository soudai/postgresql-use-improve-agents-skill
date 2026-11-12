-- idx_scan=0 is not evidence that an index is safe to drop.
-- indexdef can contain SQL literals. collect.py substitutes __INCLUDE_TEXT__ with true only for
-- --include-query-text; for direct psql use replace it with true or false first.
-- pg_get_indexdef() locks the table and pg_relation_size() locks the index: both queue behind a
-- granted or waiting ACCESS EXCLUSIVE lock, so locked relations are skipped (size_skipped_locked).
WITH busy AS MATERIALIZED (
    SELECT DISTINCT l.relation
    FROM pg_catalog.pg_locks AS l
    WHERE l.locktype = 'relation'
      AND l.mode = 'AccessExclusiveLock'
      AND l.database = (SELECT oid FROM pg_catalog.pg_database
                        WHERE datname = pg_catalog.current_database())
)
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
       ic.relkind = 'i' AND k.index_locked AS size_skipped_locked,
       CASE WHEN ic.relkind = 'i' AND NOT k.index_locked
            THEN pg_catalog.pg_relation_size(ic.oid) END AS index_bytes,
       CASE WHEN __INCLUDE_TEXT__ AND NOT k.index_locked AND NOT k.table_locked
            THEN pg_catalog.pg_get_indexdef(ic.oid) END AS indexdef,
       ARRAY(SELECT con.conname::text
             FROM pg_catalog.pg_constraint AS con
             WHERE con.conindid = i.indexrelid
             ORDER BY con.oid) AS constraint_names
FROM pg_catalog.pg_stat_user_indexes AS s
JOIN pg_catalog.pg_index AS i ON i.indexrelid = s.indexrelid
JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
JOIN pg_catalog.pg_class AS tc ON tc.oid = i.indrelid
JOIN pg_catalog.pg_am AS am ON am.oid = ic.relam
CROSS JOIN LATERAL (
    SELECT EXISTS (SELECT 1 FROM busy AS b WHERE b.relation = ic.oid) AS index_locked,
           EXISTS (SELECT 1 FROM busy AS b WHERE b.relation = tc.oid) AS table_locked
) AS k
ORDER BY s.indexrelid;
