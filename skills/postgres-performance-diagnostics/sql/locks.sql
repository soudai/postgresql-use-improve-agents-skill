-- Materialize once per session: pg_blocking_pids acquires shared lock-manager state.
-- Include other-DB/shared locks to retain cross-session chains; only resolve local OIDs.
-- PID NULL indicates a prepared transaction. A blocking_pids entry 0 also means prepared.
WITH sessions AS MATERIALIZED (
    SELECT a.pid, a.datid, a.datname, a.backend_start, a.xact_start,
           a.query_start, a.state, a.wait_event_type, a.wait_event,
           a.backend_xmin::text AS backend_xmin,
           pg_catalog.pg_blocking_pids(a.pid) AS blocking_pids
    FROM pg_catalog.pg_stat_activity AS a
    WHERE a.pid <> pg_catalog.pg_backend_pid()
), local_database AS (
    SELECT oid FROM pg_catalog.pg_database
    WHERE datname = pg_catalog.current_database()
)
SELECT l.*, a.datid AS session_datid, a.datname, a.backend_start,
       a.xact_start, a.query_start, a.state, a.wait_event_type,
       a.wait_event, a.backend_xmin, a.blocking_pids,
       n.nspname AS relation_schema, c.relname AS relation_name
FROM pg_catalog.pg_locks AS l
LEFT JOIN sessions AS a ON a.pid = l.pid
LEFT JOIN pg_catalog.pg_class AS c
       ON c.oid = l.relation
      AND l.database = (SELECT oid FROM local_database)
LEFT JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE l.pid IS DISTINCT FROM pg_catalog.pg_backend_pid()
ORDER BY l.granted, l.waitstart NULLS LAST, l.pid NULLS FIRST,
         l.locktype, l.database, l.relation, l.mode;
