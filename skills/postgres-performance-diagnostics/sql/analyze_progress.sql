SELECT s.*
FROM pg_catalog.pg_stat_progress_analyze AS s
WHERE s.datname = pg_catalog.current_database()
ORDER BY s.pid;
