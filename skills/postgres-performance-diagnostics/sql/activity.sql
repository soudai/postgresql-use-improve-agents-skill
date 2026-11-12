-- Current DB sessions plus cluster-wide auxiliary processes (datid IS NULL).
-- query is sensitive: collect.py removes it by default.
-- Timestamp ages and waits are point-in-time evidence, not cumulative counters.
-- backend_xmin_age identifies sessions holding back the VACUUM horizon of this DB.
-- pg_blocking_pids() locks the whole lock manager for each call, so it is asked only for
-- sessions with an ungranted lock; blocking_pids is NULL for everyone else. pg_locks is used
-- because wait_event is hidden from roles without pg_read_all_stats.
WITH waiting AS MATERIALIZED (
    SELECT DISTINCT l.pid
    FROM pg_catalog.pg_locks AS l
    WHERE NOT l.granted AND l.pid IS NOT NULL
)
SELECT s.*,
       pg_catalog.age(s.backend_xid) AS backend_xid_age,
       pg_catalog.age(s.backend_xmin) AS backend_xmin_age,
       CASE WHEN w.pid IS NOT NULL
            THEN pg_catalog.pg_blocking_pids(s.pid) END AS blocking_pids
FROM pg_catalog.pg_stat_activity AS s
LEFT JOIN waiting AS w ON w.pid = s.pid
WHERE (s.datid = (SELECT oid FROM pg_catalog.pg_database
                 WHERE datname = pg_catalog.current_database())
       OR s.datid IS NULL)
  AND s.pid <> pg_catalog.pg_backend_pid()
ORDER BY s.pid;
