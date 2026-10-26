-- Optional PG19 view: a current eligibility estimate, not a promised execution order.
-- Includes TOAST relations to expose retention/maintenance work hidden from user tables.
SELECT s.* FROM pg_catalog.pg_stat_autovacuum_scores AS s ORDER BY s.relid;
