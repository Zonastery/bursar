-- ducto: 014 — money columns INTEGER/BIGINT -> NUMERIC(18,4) (M11).
--
-- Forward migration for EXISTING installs whose tables were created before the
-- money type was widened. On a fresh install, 001/004/008/009 already create
-- these columns as NUMERIC(18,4), so every ALTER below is a guarded no-op.
--
-- Fully idempotent and re-runnable: each ALTER is only applied when the column
-- is still an integer-family type, so re-runs do nothing and never rewrite a
-- table that is already NUMERIC.

DO $$
DECLARE
    -- (table, column) pairs whose money type must be NUMERIC(18,4).
    r RECORD;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('user_credits',        'balance'),
            ('user_credits',        'lifetime_purchased'),
            ('credit_transactions', 'amount'),
            ('credit_reservations', 'amount'),
            ('credit_plans',        'free_allowance'),
            ('credit_usage_window', 'usage'),
            ('credit_teams',        'balance'),
            ('credit_team_members', 'spend_cap'),
            ('credit_team_members', 'total_spent'),
            ('credit_spend_caps',   'cap_limit')
        ) AS t(tbl, col)
    LOOP
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = r.tbl
              AND column_name = r.col
              AND data_type IN ('integer', 'bigint', 'smallint')
        ) THEN
            EXECUTE format(
                'ALTER TABLE public.%I ALTER COLUMN %I TYPE NUMERIC(18,4) USING %I::numeric',
                r.tbl, r.col, r.col
            );
        END IF;
    END LOOP;
END;
$$;

-- Replace the legacy (non-user-scoped) idempotency index with the user-scoped
-- one (H16). The user-scoped index is also created in 001 for fresh installs;
-- here we drop the old global one so the same key can't collide across users
-- and so an existing install picks up the corrected uniqueness scope.
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_transactions_idempotency_user
    ON public.credit_transactions (user_id, (metadata ->> 'idempotency_key'))
    WHERE metadata ->> 'idempotency_key' IS NOT NULL;

DROP INDEX IF EXISTS public.idx_credit_transactions_idempotency;

-- Add a unique constraint on pricing config version for installs that predate
-- it (M14). Wrapped so a duplicate-version legacy row doesn't abort the whole
-- migration; if it can't be created, surface a notice instead of failing setup.
DO $$
BEGIN
    CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_pricing_config_version_unique
        ON public.credit_pricing_config (version);
EXCEPTION
    WHEN unique_violation THEN
        RAISE NOTICE 'ducto 014: existing duplicate pricing versions prevent the version unique index; dedupe credit_pricing_config.version manually.';
    WHEN undefined_table THEN
        NULL; -- credit_pricing_config not present yet; created by 003.
END;
$$;

NOTIFY pgrst, 'reload schema';
