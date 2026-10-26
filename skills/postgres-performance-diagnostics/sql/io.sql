-- Cluster-wide backend/object/context aggregates. Not SQL- or table-attributed.
-- Keep native columns/units; operations * block_size is not universally byte volume.
SELECT s.*
FROM pg_catalog.pg_stat_io AS s
ORDER BY s.backend_type, s.object, s.context;
