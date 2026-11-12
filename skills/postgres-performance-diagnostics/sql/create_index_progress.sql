-- CREATE INDEX / REINDEX running now in this database; a point-in-time gauge.
-- With CONCURRENTLY, the "waiting for ..." phases mean the build is held up by older
-- transactions: lockers_total/lockers_done and current_locker_pid identify them.
SELECT s.*
FROM pg_catalog.pg_stat_progress_create_index AS s
WHERE s.datname = pg_catalog.current_database()
ORDER BY s.pid;
