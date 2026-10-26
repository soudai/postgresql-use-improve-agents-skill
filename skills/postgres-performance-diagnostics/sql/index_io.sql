SELECT s.*, c.relfilenode
FROM pg_catalog.pg_statio_user_indexes AS s
JOIN pg_catalog.pg_class AS c ON c.oid = s.indexrelid
ORDER BY s.indexrelid;
