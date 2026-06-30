-- ducto: drop overloaded deduct_with_allowance (6-param) from 015_atomic_deduct.sql
--
-- 018_billing_fixes.sql added a 7-param version with p_skip_allowance BOOLEAN
-- DEFAULT FALSE.  Because the 6-param version (015) also has DEFAULTS on the
-- optional params, calling deduct_with_allowance with 6 args matches BOTH
-- functions — PostgreSQL raises "not unique".
--
-- We drop the old signature so only the 7-param version (018, with
-- skip_allowance support) survives.  Callers that omit the BOOLEAN get its
-- DEFAULT FALSE, so this is backward-compatible for every existing caller
-- (including the JS PostgresStore which hasn't added skip_allowance yet).

DROP FUNCTION IF EXISTS public.deduct_with_allowance(
    UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB
);

-- Refresh PostgREST schema cache.
NOTIFY pgrst, 'reload schema';
