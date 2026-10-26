-- Prepared transactions can hold locks and old snapshots without an active backend.
SELECT s.*
FROM pg_catalog.pg_prepared_xacts AS s
WHERE s.database = pg_catalog.current_database()
ORDER BY s.prepared, s.transaction::text;
