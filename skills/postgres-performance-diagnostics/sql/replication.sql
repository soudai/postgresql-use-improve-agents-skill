-- Cluster-wide WAL sender state. Lag is not an estimate of time to catch up.
-- Idle lag NULL is not automatically a fault. Compare LSNs on the same timeline.
-- backend_xmin is hot_standby_feedback from the standby; it holds back VACUUM here.
-- pending_send_gap_bytes is WAL generated here but not yet sent (NULL on a cascading standby,
-- where the local WAL head is not a write position). The four gaps locate where WAL waits:
-- not sent -> sent but not written -> written but not flushed -> flushed but not replayed.
WITH wal_position AS (
    SELECT CASE WHEN NOT pg_catalog.pg_is_in_recovery()
                THEN pg_catalog.pg_current_wal_lsn() END AS current_lsn
)
SELECT s.*,
       pg_catalog.age(s.backend_xmin) AS backend_xmin_age,
       p.current_lsn,
       pg_catalog.pg_wal_lsn_diff(p.current_lsn, s.sent_lsn) AS pending_send_gap_bytes,
       pg_catalog.pg_wal_lsn_diff(s.sent_lsn, s.write_lsn) AS sent_write_gap_bytes,
       pg_catalog.pg_wal_lsn_diff(s.write_lsn, s.flush_lsn) AS write_flush_gap_bytes,
       pg_catalog.pg_wal_lsn_diff(s.flush_lsn, s.replay_lsn) AS flush_replay_gap_bytes,
       pg_catalog.pg_wal_lsn_diff(p.current_lsn, s.replay_lsn) AS total_replay_gap_bytes
FROM pg_catalog.pg_stat_replication AS s
CROSS JOIN wal_position AS p
ORDER BY s.pid;
