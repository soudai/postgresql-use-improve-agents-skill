-- Counts and times are cumulative; checkpoint requests may include skipped checkpoints.
SELECT s.* FROM pg_catalog.pg_stat_checkpointer AS s;
