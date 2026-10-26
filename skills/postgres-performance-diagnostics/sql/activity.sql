-- Current DB sessions plus cluster-wide auxiliary processes (datid IS NULL).
-- query is sensitive: collect.py removes it by default.
-- Timestamp ages and waits are point-in-time evidence, not cumulative counters.
SELECT s.*,
       pg_catalog.pg_blocking_pids(s.pid) AS blocking_pids
FROM pg_catalog.pg_stat_activity AS s
WHERE (s.datid = (SELECT oid FROM pg_catalog.pg_database
                 WHERE datname = pg_catalog.current_database())
       OR s.datid IS NULL)
  AND s.pid <> pg_catalog.pg_backend_pid()
ORDER BY s.pid;
