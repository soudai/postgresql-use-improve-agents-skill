-- Size/age fields are gauges; n_dead_tup/reltuples are estimates, not bloat measurements.
-- Partition parents have no heap. Their child tables appear as separate rows.
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
       CASE WHEN c.relkind IN ('r', 'm')
            THEN pg_catalog.age(c.relfrozenxid) END AS xid_age,
       CASE WHEN c.relkind IN ('r', 'm')
            THEN pg_catalog.mxid_age(c.relminmxid) END AS mxid_age,
       CASE WHEN c.relkind IN ('r', 'm')
            THEN pg_catalog.pg_relation_size(c.oid) END AS heap_main_bytes,
       CASE WHEN c.relkind IN ('r', 'm')
            THEN pg_catalog.pg_table_size(c.oid) END AS table_bytes,
       CASE WHEN c.relkind IN ('r', 'm')
            THEN pg_catalog.pg_indexes_size(c.oid) END AS indexes_bytes,
       CASE WHEN c.relkind IN ('r', 'm')
            THEN pg_catalog.pg_total_relation_size(c.oid) END AS total_bytes
FROM pg_catalog.pg_stat_user_tables AS s
JOIN pg_catalog.pg_class AS c ON c.oid = s.relid
ORDER BY s.relid;
