-- Export pg_settings as one JSON document for scripts/review_config.py. Read-only.
-- psql -X -qAt --file sql/settings.sql > settings.json
-- Values that can hold credentials or shell commands are redacted; file paths are omitted.
-- Run as a role without ALTER ROLE ... SET overrides, or the export shows that role's values.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
SET LOCAL search_path = pg_catalog;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '1s';
SET LOCAL timezone = 'UTC';

SELECT jsonb_build_object(
  'kind', 'pg_settings_export',
  'collected_at', clock_timestamp(),
  'server_version_num', current_setting('server_version_num')::integer,
  'in_recovery', pg_is_in_recovery(),
  'settings', jsonb_agg(jsonb_build_object(
      'name', s.name,
      -- Parameters SET LOCAL above report the session default (reset_val), not this script's value.
      'setting', CASE WHEN s.redact THEN '<redacted>'
                      WHEN s.name IN ('statement_timeout', 'lock_timeout', 'timezone', 'search_path')
                      THEN s.reset_val ELSE s.setting END,
      'boot_val', CASE WHEN s.redact THEN '<redacted>' ELSE s.boot_val END,
      'reset_val', CASE WHEN s.redact THEN '<redacted>' ELSE s.reset_val END,
      'unit', s.unit, 'vartype', s.vartype, 'context', s.context, 'source', s.source,
      'pending_restart', s.pending_restart) ORDER BY s.name))
FROM (SELECT p.*, p.name ~ '(conninfo|_command|_file|keyfile|_dir|_directory|_directories|library_path|control_path)$' AS redact
      FROM pg_catalog.pg_settings AS p) AS s;
COMMIT;
