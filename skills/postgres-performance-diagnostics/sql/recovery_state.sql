-- Standby-side replay position from functions that exist on every supported version
-- (pg_stat_recovery is PostgreSQL 19 only). One row; the standby columns are NULL on a primary.
-- replay_delay_seconds is "now - commit time of the last replayed transaction": it grows on an
-- idle primary too, so read it together with receive_replay_gap_bytes (0 means caught up).
SELECT r.in_recovery,
       CASE WHEN r.in_recovery THEN pg_catalog.pg_last_wal_receive_lsn() END AS receive_lsn,
       CASE WHEN r.in_recovery THEN pg_catalog.pg_last_wal_replay_lsn() END AS replay_lsn,
       CASE WHEN r.in_recovery
            THEN pg_catalog.pg_wal_lsn_diff(pg_catalog.pg_last_wal_receive_lsn(),
                                            pg_catalog.pg_last_wal_replay_lsn())
       END AS receive_replay_gap_bytes,
       CASE WHEN r.in_recovery THEN pg_catalog.pg_last_xact_replay_timestamp() END AS last_replay_xact_time,
       CASE WHEN r.in_recovery
            THEN EXTRACT(epoch FROM pg_catalog.clock_timestamp() - pg_catalog.pg_last_xact_replay_timestamp())
       END AS replay_delay_seconds,
       CASE WHEN r.in_recovery THEN pg_catalog.pg_get_wal_replay_pause_state() END AS replay_pause_state
FROM (SELECT pg_catalog.pg_is_in_recovery() AS in_recovery) AS r;
