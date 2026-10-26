-- Cluster-wide WAL generation counters. PG18 WAL timing is in pg_stat_io.
SELECT s.* FROM pg_catalog.pg_stat_wal AS s;
