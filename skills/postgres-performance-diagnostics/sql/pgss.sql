-- collect.py substitutes the safely quoted extension schema for __PGSS_SCHEMA__.
-- For direct psql use, replace that identifier with the installed schema first.
-- Cumulative counters only; derive interval rankings from two valid snapshots.
-- query may contain literals: collect.py removes it unless explicitly requested.
-- The reserved comment identifies this toolkit's own SELECTs (collector and sampler). It is
-- matched anywhere in the text because pg_stat_statements drops a comment that precedes the
-- statement; the tools place it after the first keyword.
SELECT s.*
FROM __PGSS_SCHEMA__.pg_stat_statements AS s
WHERE s.dbid = (SELECT oid FROM pg_catalog.pg_database
                WHERE datname = pg_catalog.current_database())
  AND (s.query IS NULL OR s.query NOT LIKE '%/* postgres-performance-diagnostics */%')
ORDER BY s.dbid, s.userid, s.queryid, s.toplevel;
