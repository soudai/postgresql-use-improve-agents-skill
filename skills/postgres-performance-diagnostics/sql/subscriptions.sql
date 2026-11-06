-- Do not read pg_subscription.subconninfo (contains connection credentials).
SELECT sub.oid AS subid, sub.subname, sub.subenabled AS enabled, sub.subslotname AS slot_name,
       s.worker_type, s.pid, s.leader_pid, s.relid, s.received_lsn, s.latest_end_lsn,
       s.latest_end_time, s.last_msg_receipt_time
FROM pg_catalog.pg_subscription AS sub
LEFT JOIN pg_catalog.pg_stat_subscription AS s ON sub.oid = s.subid
WHERE sub.subdbid = (SELECT oid FROM pg_catalog.pg_database
                      WHERE datname = pg_catalog.current_database())
ORDER BY sub.oid, s.pid NULLS FIRST, s.relid NULLS FIRST;
