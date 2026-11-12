-- Foreign keys of user tables and whether the referencing (child) side has an index that
-- starts with the key columns. Without one, every DELETE or key UPDATE on the referenced table
-- scans the child inside the RI trigger and holds its locks for that long.
-- A match needs a valid, non-partial btree (or single-column hash) index whose leading key
-- columns are exactly the FK columns in any order. Catalog only: no table is scanned or locked.
-- Partitions inherit the parent's constraint, so only top-level constraints are listed.
SELECT con.oid AS constraint_oid,
       con.conname::text AS constraint_name,
       con.conrelid AS relid,
       n.nspname::text AS schemaname,
       c.relname::text AS relname,
       c.relkind,
       c.reltuples,
       c.relpages,
       ARRAY(SELECT a.attname::text
             FROM pg_catalog.unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
             JOIN pg_catalog.pg_attribute AS a
               ON a.attrelid = con.conrelid AND a.attnum = k.attnum
             ORDER BY k.ord) AS columns,
       con.confrelid AS referenced_relid,
       rn.nspname::text AS referenced_schemaname,
       rc.relname::text AS referenced_relname,
       con.confdeltype AS delete_action,
       con.confupdtype AS update_action,
       con.convalidated AS validated,
       s.index_name AS supporting_index
FROM pg_catalog.pg_constraint AS con
JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_class AS rc ON rc.oid = con.confrelid
JOIN pg_catalog.pg_namespace AS rn ON rn.oid = rc.relnamespace
LEFT JOIN LATERAL (
    SELECT ic.relname::text AS index_name
    FROM pg_catalog.pg_index AS i
    JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
    JOIN pg_catalog.pg_am AS am ON am.oid = ic.relam
    WHERE i.indrelid = con.conrelid
      AND i.indisvalid
      AND i.indpred IS NULL
      AND am.amname IN ('btree', 'hash')
      AND i.indnkeyatts >= pg_catalog.cardinality(con.conkey)
      AND ARRAY(SELECT k FROM pg_catalog.unnest(
                    (pg_catalog.string_to_array(i.indkey::text, ' ')::smallint[])
                        [1:pg_catalog.cardinality(con.conkey)]) AS k ORDER BY k)
        = ARRAY(SELECT k FROM pg_catalog.unnest(con.conkey) AS k ORDER BY k)
    ORDER BY i.indnkeyatts, ic.relname
    LIMIT 1
) AS s ON true
WHERE con.contype = 'f'
  AND con.conparentid = 0
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_toast'
ORDER BY c.relpages DESC, con.oid;
