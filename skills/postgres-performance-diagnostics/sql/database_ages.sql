-- Transaction-ID and MultiXact age of every database in the cluster. Wraparound protection is
-- driven by the oldest database, including template0, not by the database being diagnosed.
-- Names of other databases are visible here; no statistics from them are read.
SELECT d.oid AS datid, d.datname, d.datallowconn, d.datistemplate,
       pg_catalog.age(d.datfrozenxid) AS xid_age,
       pg_catalog.mxid_age(d.datminmxid) AS mxid_age,
       d.datname = pg_catalog.current_database() AS is_current
FROM pg_catalog.pg_database AS d
ORDER BY pg_catalog.age(d.datfrozenxid) DESC, d.oid;
