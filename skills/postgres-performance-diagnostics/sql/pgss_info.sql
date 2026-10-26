-- Extension-wide resets/deallocations matter even though pgss.sql is DB-filtered.
SELECT s.* FROM __PGSS_SCHEMA__.pg_stat_statements_info AS s;
