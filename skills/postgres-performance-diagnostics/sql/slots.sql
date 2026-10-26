-- All DBs: any slot may retain this cluster's WAL. retained_wal_bytes is an LSN-distance
-- gauge, not measured on-disk WAL usage (segments/other retention rules also matter).
-- On a standby use replay LSN as an explicitly limited reference; it is not primary head.
WITH wal_position AS (
    SELECT pg_catalog.pg_is_in_recovery() AS in_recovery,
           CASE WHEN pg_catalog.pg_is_in_recovery()
                THEN pg_catalog.pg_last_wal_replay_lsn()
                ELSE pg_catalog.pg_current_wal_lsn() END AS reference_lsn
)
SELECT s.*, p.in_recovery, p.reference_lsn,
       CASE WHEN s.restart_lsn IS NOT NULL AND p.reference_lsn >= s.restart_lsn
            THEN pg_catalog.pg_wal_lsn_diff(p.reference_lsn, s.restart_lsn)
       END AS retained_wal_bytes,
       CASE WHEN s.xmin IS NOT NULL THEN pg_catalog.age(s.xmin) END AS xmin_age,
       CASE WHEN s.catalog_xmin IS NOT NULL
            THEN pg_catalog.age(s.catalog_xmin) END AS catalog_xmin_age
FROM pg_catalog.pg_replication_slots AS s
CROSS JOIN wal_position AS p
ORDER BY s.slot_name;
