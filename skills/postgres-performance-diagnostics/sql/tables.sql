-- Size/age fields are gauges; n_dead_tup/reltuples are estimates, not bloat measurements.
-- Partition parents have no heap. Their child tables appear as separate rows.
-- The size functions take ACCESS SHARE and queue behind a granted or waiting ACCESS EXCLUSIVE
-- lock (DDL, VACUUM FULL, REINDEX). Skip those relations instead of losing the whole section;
-- size_skipped_locked marks the rows whose sizes are unknown, not zero.
WITH busy AS MATERIALIZED (
    SELECT DISTINCT l.relation
    FROM pg_catalog.pg_locks AS l
    WHERE l.locktype = 'relation'
      AND l.mode = 'AccessExclusiveLock'
      AND l.database = (SELECT oid FROM pg_catalog.pg_database
                        WHERE datname = pg_catalog.current_database())
), relations AS (
    SELECT c.*,
           c.relkind IN ('r', 'm') AS has_storage,
           (EXISTS (SELECT 1 FROM busy AS b WHERE b.relation IN (c.oid, c.reltoastrelid))
            OR EXISTS (SELECT 1 FROM pg_catalog.pg_index AS i
                       JOIN busy AS b ON b.relation = i.indexrelid
                       WHERE i.indrelid = c.oid)) AS locked
    FROM pg_catalog.pg_class AS c
)
SELECT s.*,
       c.relkind,
       c.relpersistence,
       c.relispartition,
       c.relfilenode,
       c.reloptions,
       c.relpages,
       c.relallfrozen,
       c.reltuples,
       c.reltoastrelid,
       CASE WHEN c.has_storage
            THEN pg_catalog.age(c.relfrozenxid) END AS xid_age,
       CASE WHEN c.has_storage
            THEN pg_catalog.mxid_age(c.relminmxid) END AS mxid_age,
       c.has_storage AND c.locked AS size_skipped_locked,
       CASE WHEN c.has_storage AND NOT c.locked
            THEN pg_catalog.pg_relation_size(c.oid) END AS heap_main_bytes,
       CASE WHEN c.has_storage AND NOT c.locked
            THEN pg_catalog.pg_table_size(c.oid) END AS table_bytes,
       CASE WHEN c.has_storage AND NOT c.locked
            THEN pg_catalog.pg_indexes_size(c.oid) END AS indexes_bytes,
       CASE WHEN c.has_storage AND NOT c.locked
            THEN pg_catalog.pg_total_relation_size(c.oid) END AS total_bytes
FROM pg_catalog.pg_stat_user_tables AS s
JOIN relations AS c ON c.oid = s.relid
ORDER BY s.relid;
