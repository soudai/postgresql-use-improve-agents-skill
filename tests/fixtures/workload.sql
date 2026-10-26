-- Synthetic integration workload. Run ONLY in an isolated disposable test database.
-- Nothing in this file is a diagnostic command intended for a production database.
CREATE SCHEMA IF NOT EXISTS diagnostics;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA diagnostics;
CREATE SCHEMA IF NOT EXISTS pgskills_fixture;

CREATE TABLE IF NOT EXISTS pgskills_fixture.accounts (
    id integer PRIMARY KEY,
    tenant_id integer NOT NULL,
    status text NOT NULL,
    payload text NOT NULL
);
INSERT INTO pgskills_fixture.accounts
SELECT i, i % 20, CASE WHEN i % 20 = 0 THEN 'inactive' ELSE 'active' END,
       repeat(md5(i::text), 8)
FROM generate_series(1, 20000) AS i
ON CONFLICT (id) DO NOTHING;
CREATE INDEX IF NOT EXISTS accounts_tenant_id
    ON pgskills_fixture.accounts (tenant_id, id);
CREATE STATISTICS IF NOT EXISTS pgskills_fixture.account_correlation
    (ndistinct, dependencies, mcv) ON tenant_id, status
    FROM pgskills_fixture.accounts;

CREATE TABLE IF NOT EXISTS pgskills_fixture.events (
    event_day date NOT NULL,
    account_id integer NOT NULL,
    amount integer NOT NULL
) PARTITION BY RANGE (event_day);
CREATE TABLE IF NOT EXISTS pgskills_fixture.events_2026
    PARTITION OF pgskills_fixture.events
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE INDEX IF NOT EXISTS events_account_id
    ON pgskills_fixture.events (account_id);
INSERT INTO pgskills_fixture.events
SELECT DATE '2026-01-01' + (i % 300), i % 20000 + 1, i % 100
FROM generate_series(1, 20000) AS i
WHERE NOT EXISTS (SELECT 1 FROM pgskills_fixture.events);

ANALYZE pgskills_fixture.accounts;
ANALYZE pgskills_fixture.events;

SELECT count(*) FROM pgskills_fixture.accounts WHERE tenant_id = 7;
SELECT sum(e.amount)
FROM pgskills_fixture.accounts AS a
JOIN pgskills_fixture.events AS e ON e.account_id = a.id
WHERE a.tenant_id = 7;

BEGIN;
SET LOCAL work_mem = '64kB';
-- Fetch only a count to the test harness while forcing a materialized spilling sort.
WITH sorted AS MATERIALIZED (
    SELECT payload, id FROM pgskills_fixture.accounts ORDER BY payload, id
)
SELECT count(*) FROM sorted;
COMMIT;

-- Ensure committed statistics are visible to the next collecting backend.
SELECT pg_stat_force_next_flush();
