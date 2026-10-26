-- wal_distance/block_distance/io_depth are gauges; other fields include counters/reset.
SELECT s.* FROM pg_catalog.pg_stat_recovery_prefetch AS s;
