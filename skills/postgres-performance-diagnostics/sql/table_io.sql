-- PostgreSQL buffer misses are not necessarily physical storage reads.
SELECT s.*, c.relfilenode
FROM pg_catalog.pg_statio_user_tables AS s
JOIN pg_catalog.pg_class AS c ON c.oid = s.relid
ORDER BY s.relid;
