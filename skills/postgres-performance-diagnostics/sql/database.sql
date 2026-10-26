-- Current database cumulative statistics. Compare snapshots; do not reset counters.
SELECT s.*,
       d.datfrozenxid::text AS datfrozenxid,
       d.datminmxid::text AS datminmxid,
       pg_catalog.age(d.datfrozenxid) AS xid_age,
       pg_catalog.mxid_age(d.datminmxid) AS mxid_age
FROM pg_catalog.pg_stat_database AS s
JOIN pg_catalog.pg_database AS d ON d.oid = s.datid
WHERE d.datname = pg_catalog.current_database()
ORDER BY s.datid;
