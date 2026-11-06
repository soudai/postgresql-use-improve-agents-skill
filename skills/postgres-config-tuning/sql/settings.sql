-- Export pg_settings as one JSON document for scripts/review_config.py. Read-only.
-- psql -X -qAt --file sql/settings.sql > settings.json
-- Values that can hold credentials or shell commands are redacted; file paths are omitted.
-- Connect as the application role in the target database to capture its effective values.
-- Supply --set=instance_id=NAME to match diagnostic evidence from the same instance.
\set ON_ERROR_STOP on
\if :{?instance_id}
\else
\set instance_id ''
\endif
BEGIN READ ONLY;
-- Preserve the setting and its source together before applying collector guards.
SELECT pg_catalog.jsonb_object_agg(name, pg_catalog.jsonb_build_object('setting', setting, 'source', source)) AS original_guards
FROM pg_catalog.pg_settings
WHERE name IN ('statement_timeout', 'lock_timeout', 'timezone', 'search_path')
\gset
SET LOCAL search_path = pg_catalog;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '1s';
SET LOCAL timezone = 'UTC';

SELECT jsonb_build_object(
  'kind', 'pg_settings_export',
  'collected_at', clock_timestamp(),
  'server_version_num', current_setting('server_version_num')::integer,
  'in_recovery', pg_is_in_recovery(),
  'metadata', jsonb_build_object(
      'instance_id', NULLIF(:'instance_id', ''), 'database', current_database(),
      'database_oid', (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()),
      'session_role', session_user, 'effective_role', current_user,
      'server_version_num', current_setting('server_version_num')::integer,
      'in_recovery', pg_is_in_recovery(), 'postmaster_start', pg_postmaster_start_time()),
  'scoped_overrides', COALESCE((
      SELECT jsonb_agg(jsonb_build_object('database_oid', o.setdatabase, 'role_oid', o.setrole,
               'name', s.name, 'setting', substr(v.entry, length(s.name) + 2))
               ORDER BY o.setdatabase, o.setrole, s.name)
      FROM pg_catalog.pg_db_role_setting AS o
      CROSS JOIN LATERAL unnest(o.setconfig) AS v(entry)
      JOIN pg_catalog.pg_settings AS s ON s.name = split_part(v.entry, '=', 1)
      WHERE o.setdatabase IN (0, (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()))
        AND o.setrole IN (0, (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = session_user))
        AND s.name IN ('work_mem', 'maintenance_work_mem', 'hash_mem_multiplier', 'plan_cache_mode',
                       'statement_timeout', 'lock_timeout', 'idle_in_transaction_session_timeout',
                       'max_parallel_workers_per_gather', 'random_page_cost', 'jit', 'synchronous_commit')
  ), '[]'::jsonb),
  'settings', jsonb_agg(jsonb_build_object(
      'name', s.name,
      'setting', CASE WHEN s.redact THEN '<redacted>'
                      ELSE COALESCE(:'original_guards'::jsonb -> s.name ->> 'setting', s.setting) END,
      'boot_val', CASE WHEN s.redact THEN '<redacted>' ELSE s.boot_val END,
      'reset_val', CASE WHEN s.redact THEN '<redacted>' ELSE s.reset_val END,
      'min_val', s.min_val, 'unit', s.unit, 'vartype', s.vartype, 'context', s.context, 'source', COALESCE(:'original_guards'::jsonb -> s.name ->> 'source', s.source),
      'pending_restart', s.pending_restart) ORDER BY s.name))
FROM (SELECT p.*, p.name ~ '(conninfo|_command|_file|keyfile|_dir|_directory|_directories|library_path|control_path)$' AS redact
      FROM pg_catalog.pg_settings AS p) AS s;
COMMIT;
