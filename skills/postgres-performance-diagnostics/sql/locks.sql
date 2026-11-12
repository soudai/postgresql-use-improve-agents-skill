-- Locks of the sessions involved in a wait: every ungranted request, plus all locks held by
-- the waiters and by the sessions blocking them. Granted locks of uninvolved sessions are
-- left out: on a busy server they are most of pg_locks and say nothing about a wait.
-- An empty result therefore means "no lock wait at this instant", not "no locks".
-- Other-DB/shared locks stay in to retain cross-session chains; only local OIDs are resolved.
-- PID NULL indicates a prepared transaction. A blocking_pids entry 0 also means prepared.
-- pg_blocking_pids() is called once per waiter (it locks the whole lock manager each time).
WITH waiting AS MATERIALIZED (
    SELECT w.pid, pg_catalog.pg_blocking_pids(w.pid) AS blocking_pids
    FROM (SELECT DISTINCT l.pid
          FROM pg_catalog.pg_locks AS l
          WHERE NOT l.granted AND l.pid IS NOT NULL
            AND l.pid <> pg_catalog.pg_backend_pid()) AS w
), involved AS MATERIALIZED (
    SELECT w.pid FROM waiting AS w
    UNION
    SELECT b.pid FROM waiting AS w CROSS JOIN LATERAL pg_catalog.unnest(w.blocking_pids) AS b(pid)
), sessions AS MATERIALIZED (
    SELECT a.pid, a.datid, a.datname, a.backend_start, a.xact_start,
           a.query_start, a.state, a.wait_event_type, a.wait_event,
           a.backend_xmin::text AS backend_xmin, w.blocking_pids
    FROM pg_catalog.pg_stat_activity AS a
    JOIN involved AS i ON i.pid = a.pid
    LEFT JOIN waiting AS w ON w.pid = a.pid
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
  AND (NOT l.granted
       OR l.pid IN (SELECT i.pid FROM involved AS i)
       -- Prepared transactions have no PID; keep their locks when one of them blocks a waiter.
       OR (l.pid IS NULL AND EXISTS (SELECT 1 FROM waiting AS w WHERE 0 = ANY (w.blocking_pids))))
ORDER BY l.granted, l.waitstart NULLS LAST, l.pid NULLS FIRST,
         l.locktype, l.database, l.relation, l.mode;
