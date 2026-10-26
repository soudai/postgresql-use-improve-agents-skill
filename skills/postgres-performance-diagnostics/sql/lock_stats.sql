-- Optional PG19 view. Counts completed acquisitions that waited > deadlock_timeout,
-- not every lock wait and not the currently waiting sessions.
SELECT s.* FROM pg_catalog.pg_stat_lock AS s ORDER BY s.locktype;
