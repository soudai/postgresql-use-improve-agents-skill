-- collect.py substitutes the safely quoted extension schema for __PGSS_SCHEMA__.
-- For direct psql use, replace that identifier with the installed schema first.
-- Cumulative counters only; derive interval rankings from two valid snapshots.
-- query may contain literals: collect.py removes it unless explicitly requested.
-- This exact reserved prefix identifies this toolkit's collection SELECTs only.
SELECT s.*
FROM __PGSS_SCHEMA__.pg_stat_statements AS s
WHERE s.dbid = (SELECT oid FROM pg_catalog.pg_database
                WHERE datname = pg_catalog.current_database())
  AND (s.query IS NULL OR s.query NOT LIKE '/* postgres-performance-diagnostics */%')
ORDER BY s.dbid, s.userid, s.queryid, s.toplevel;
