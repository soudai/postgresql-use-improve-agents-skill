-- Deliberately omit conninfo, even though PostgreSQL masks sensitive connection fields.
SELECT s.pid, s.status, s.receive_start_lsn, s.receive_start_tli,
       s.written_lsn, s.flushed_lsn, s.received_tli,
       s.last_msg_send_time, s.last_msg_receipt_time,
       s.latest_end_lsn, s.latest_end_time, s.slot_name,
       s.sender_host, s.sender_port
FROM pg_catalog.pg_stat_wal_receiver AS s
ORDER BY s.pid;
