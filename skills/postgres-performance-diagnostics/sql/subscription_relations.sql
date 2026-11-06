SELECT sr.srsubid AS subid, sr.srrelid AS relid, n.nspname AS schema_name, c.relname AS relation_name,
       sr.srsubstate AS sync_state, sr.srsublsn AS sync_lsn
FROM pg_catalog.pg_subscription_rel AS sr
JOIN pg_catalog.pg_class AS c ON c.oid = sr.srrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
ORDER BY sr.srsubid, sr.srrelid;
