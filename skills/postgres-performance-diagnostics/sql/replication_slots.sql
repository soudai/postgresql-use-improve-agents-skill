-- Logical decoding spill is distinct from query sort/hash spill.
SELECT s.*
FROM pg_catalog.pg_stat_replication_slots AS s
ORDER BY s.slot_name;
