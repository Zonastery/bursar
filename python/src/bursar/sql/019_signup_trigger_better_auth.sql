-- bursar: migrate signup bonus trigger from auth.users to better-auth "user" table.
-- The "user" table is better-auth's default table name (quoted because it's a reserved word).
-- Uses to_regclass for safety: environments without the table skip cleanly.
--
-- Also updates grant_signup_bonus() to default to 0 (no bonus) when the pricing
-- config doesn't specify ledger.signup_grant — no surprise credits.

CREATE OR REPLACE FUNCTION public.grant_signup_bonus()
RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_bonus NUMERIC;
BEGIN
  SELECT COALESCE(
    (SELECT (config->'ledger'->>'signup_grant')::numeric FROM public.credit_pricing_config WHERE active = TRUE LIMIT 1),
    0
  ) INTO v_bonus;

  IF v_bonus <= 0 THEN
    RETURN NEW;
  END IF;

  PERFORM public.credits_add_internal(NEW.id, v_bonus, 'signup_bonus', NULL, 'gifted');
  RETURN NEW;
END;
$$;

DO $$ BEGIN
  IF to_regclass('auth.users') IS NOT NULL THEN
    DROP TRIGGER IF EXISTS on_signup_credit_bonus ON auth.users;
  END IF;
END $$;

DO $$ BEGIN
  IF to_regclass('public.user') IS NOT NULL THEN
    IF NOT EXISTS (
      SELECT 1 FROM pg_trigger
      WHERE tgname = 'on_signup_credit_bonus'
      AND tgrelid = to_regclass('public.user')
    ) THEN
      CREATE CONSTRAINT TRIGGER on_signup_credit_bonus
        AFTER INSERT ON "user"
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION public.grant_signup_bonus();
    END IF;
  END IF;
END $$;
