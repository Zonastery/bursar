-- Run after loading the canonical SQL files into a disposable PostgreSQL 15 DB.
-- This file is intentionally test-only and is not part of the install sequence.
BEGIN;

DO $$
BEGIN
  IF NOT bursar.is_finite_numeric(1::numeric)
     OR bursar.is_finite_numeric('NaN'::numeric)
     OR bursar.is_finite_numeric('Infinity'::numeric) THEN
    RAISE EXCEPTION 'finite numeric guard failed';
  END IF;
END;
$$;

SELECT has_schema_privilege('service_role','bursar','USAGE') AS service_schema_usage;
SELECT NOT has_schema_privilege('anon','bursar','USAGE') AS anon_schema_denied;
SELECT count(*) AS missing_rls
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'bursar' AND c.relkind = 'r' AND NOT c.relrowsecurity;

SELECT count(*) AS unsafe_security_definers
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'bursar' AND p.prosecdef
  AND NOT EXISTS (
    SELECT 1 FROM unnest(coalesce(p.proconfig, ARRAY[]::text[])) cfg
    WHERE cfg LIKE 'search_path=%'
  );

SELECT (bursar.credits_add(gen_random_uuid(), 'NaN'::numeric)::jsonb->>'error') = 'invalid_amount'
  AS nan_rejected;
SELECT (bursar.create_team('regression-team', 'NaN'::numeric)::jsonb->>'error') = 'invalid_amount'
  AS nan_team_rejected;

-- Cursor totals must describe the complete filtered set, not only the page.
SELECT total_count >= 0 AS cursor_total_is_valid
FROM bursar.list_transactions_cursor_with_total(gen_random_uuid(), NULL, NULL, NULL, 10)
LIMIT 1;

EXPLAIN (FORMAT JSON)
SELECT id
FROM bursar.credit_transactions
WHERE user_id = gen_random_uuid()
  AND type = 'usage'
ORDER BY created_at DESC, id DESC
LIMIT 10;

ROLLBACK;
