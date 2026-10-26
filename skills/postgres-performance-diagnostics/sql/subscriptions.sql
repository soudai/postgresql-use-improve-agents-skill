-- Do not read pg_subscription.subconninfo (contains connection credentials).
SELECT s.*
FROM pg_catalog.pg_stat_subscription AS s
JOIN pg_catalog.pg_subscription AS sub ON sub.oid = s.subid
WHERE sub.subdbid = (SELECT oid FROM pg_catalog.pg_database
                      WHERE datname = pg_catalog.current_database())
ORDER BY s.subid, s.pid NULLS FIRST, s.relid NULLS FIRST;
