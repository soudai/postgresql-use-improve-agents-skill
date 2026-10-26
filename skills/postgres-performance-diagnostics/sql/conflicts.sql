SELECT s.*
FROM pg_catalog.pg_stat_database_conflicts AS s
WHERE s.datname = pg_catalog.current_database()
ORDER BY s.datid;
