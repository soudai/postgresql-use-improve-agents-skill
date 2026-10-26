-- Progress is a point-in-time gauge; phases have different denominators.
SELECT s.*
FROM pg_catalog.pg_stat_progress_vacuum AS s
WHERE s.datname = pg_catalog.current_database()
ORDER BY s.pid;
