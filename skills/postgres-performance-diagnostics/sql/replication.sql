-- Cluster-wide WAL sender state. Lag is not an estimate of time to catch up.
-- Idle lag NULL is not automatically a fault. Compare LSNs on the same timeline.
SELECT s.*,
       pg_catalog.pg_wal_lsn_diff(s.sent_lsn, s.write_lsn) AS sent_write_gap_bytes,
       pg_catalog.pg_wal_lsn_diff(s.write_lsn, s.flush_lsn) AS write_flush_gap_bytes,
       pg_catalog.pg_wal_lsn_diff(s.flush_lsn, s.replay_lsn) AS flush_replay_gap_bytes
FROM pg_catalog.pg_stat_replication AS s
ORDER BY s.pid;
