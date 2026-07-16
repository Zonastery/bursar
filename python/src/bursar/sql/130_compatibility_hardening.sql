-- PostgreSQL database dump complete

-- This Bursar-owned trigger is intentionally attached to Better Auth's host
-- table, so it is not included by a schema-only `bursar` dump.  Keep signup
-- initialization idempotent and backend-only while preserving the existing
-- default-plan/signup-grant behavior.
DO $$
BEGIN
  IF to_regclass('public.user') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_trigger
       WHERE tgname = 'on_signup_credit_bonus'
         AND tgrelid = 'public.user'::regclass
     ) THEN
    CREATE CONSTRAINT TRIGGER on_signup_credit_bonus
      AFTER INSERT ON public."user"
      DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION bursar.grant_signup_bonus();
  END IF;
END;
$$;

-- Team creation must seed the authoritative account ledger as well as the
-- legacy team projection.  Without this, the first team charge would have a
-- correct mutable balance but an incomplete ledger reconciliation trail.
CREATE OR REPLACE FUNCTION bursar.create_team(p_name text, p_initial_balance numeric DEFAULT 0)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO ''
AS $$
DECLARE
  v_team_id uuid;
  v_account_id uuid;
BEGIN
  IF p_initial_balance IS NULL OR p_initial_balance < 0 THEN
    RETURN jsonb_build_object('error', 'invalid_amount');
  END IF;

  INSERT INTO bursar.credit_teams (name, balance)
  VALUES (p_name, p_initial_balance)
  RETURNING id INTO v_team_id;

  INSERT INTO bursar.credit_accounts (account_type, team_id)
  VALUES ('team', v_team_id)
  RETURNING id INTO v_account_id;

  IF p_initial_balance > 0 THEN
    UPDATE bursar.credit_accounts
    SET balance = p_initial_balance, updated_at = now()
    WHERE id = v_account_id;
    INSERT INTO bursar.credit_ledger_entries
      (account_id, amount, entry_type, idempotency_key, metadata)
    VALUES
      (v_account_id, p_initial_balance, 'team_initial', v_team_id::text,
       jsonb_build_object('team_id', v_team_id, 'source', 'team_creation'));
  END IF;

  RETURN jsonb_build_object('team_id', v_team_id, 'name', p_name);
END;
$$;

-- Reservation provenance is first-class: every lease is tied to the charged
-- account and its operation-scoped idempotency key, while metadata remains
-- available only for non-critical annotations.
ALTER TABLE bursar.credit_reservations
  ADD COLUMN account_id uuid,
  ADD COLUMN idempotency_key text GENERATED ALWAYS AS ((metadata ->> 'idempotency_key'::text)) STORED;

ALTER TABLE bursar.credit_reservations
  ADD CONSTRAINT credit_reservations_status_check
    CHECK (status = ANY (ARRAY['active'::text, 'settled'::text, 'released'::text, 'expired'::text])),
  ADD CONSTRAINT credit_reservations_billing_mode_check
    CHECK (billing_mode = ANY (ARRAY['strict'::text, 'strict_prepaid'::text, 'overdraft'::text])),
  ADD CONSTRAINT credit_reservations_overdraft_floor_check
    CHECK (overdraft_floor IS NULL OR overdraft_floor <= 0),
  ADD CONSTRAINT credit_reservations_account_id_fkey
    FOREIGN KEY (account_id) REFERENCES bursar.credit_accounts(id),
  ADD CONSTRAINT credit_reservations_settle_tx_id_fkey
    FOREIGN KEY (settle_tx_id) REFERENCES bursar.credit_transactions(id);

CREATE UNIQUE INDEX credit_reservations_operation_key_uq
  ON bursar.credit_reservations (account_id, operation_type, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE OR REPLACE FUNCTION bursar.assign_reservation_account()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO ''
AS $$
DECLARE
  v_team_id uuid;
BEGIN
  IF NEW.account_id IS NOT NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.metadata ? 'team_id' THEN
    BEGIN
      v_team_id := (NEW.metadata->>'team_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
      v_team_id := NULL;
    END;
    IF v_team_id IS NOT NULL THEN
      SELECT id INTO NEW.account_id
      FROM bursar.credit_accounts
      WHERE account_type = 'team' AND team_id = v_team_id;
    END IF;
  END IF;
  IF NEW.account_id IS NULL THEN
    INSERT INTO bursar.credit_accounts(account_type, user_id)
    VALUES ('personal', NEW.user_id)
    ON CONFLICT DO NOTHING;
    SELECT id INTO NEW.account_id
    FROM bursar.credit_accounts
    WHERE account_type = 'personal' AND user_id = NEW.user_id;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS assign_reservation_account ON bursar.credit_reservations;
CREATE TRIGGER assign_reservation_account
  BEFORE INSERT ON bursar.credit_reservations
  FOR EACH ROW EXECUTE FUNCTION bursar.assign_reservation_account();

COMMENT ON COLUMN bursar.credit_reservations.account_id IS
  'Authoritative personal or team account charged by this lease.';
COMMENT ON COLUMN bursar.credit_reservations.idempotency_key IS
  'Operation-scoped key extracted from metadata for duplicate-safe lease creation.';

-- Plan migration has one canonical implementation; defaults preserve the
-- existing SDK call shape without retaining an overload.
CREATE UNIQUE INDEX credit_plan_migrations_once_uq
  ON bursar.credit_plan_migrations (user_id, to_plan_id, to_config_version)
  WHERE reason = 'migrate_plan_users';

CREATE OR REPLACE FUNCTION bursar.migrate_plan_users(
  p_plan_key text,
  p_target_config_version integer DEFAULT NULL,
  p_from_plan_key text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO ''
AS $$
DECLARE
  v_target_plan_id uuid;
  v_target_version integer;
  v_from_key text := COALESCE(p_from_plan_key, p_plan_key);
  v_count integer := 0;
BEGIN
  IF p_plan_key IS NULL OR btrim(p_plan_key) = '' THEN
    RETURN jsonb_build_object('error', 'invalid_plan_key');
  END IF;

  SELECT id, config_version INTO v_target_plan_id, v_target_version
  FROM bursar.credit_plans
  WHERE plan_key = p_plan_key
    AND (p_target_config_version IS NULL OR config_version = p_target_config_version)
  ORDER BY config_version DESC, id DESC
  LIMIT 1
  FOR SHARE;
  IF v_target_plan_id IS NULL THEN
    RETURN jsonb_build_object('error', 'plan_not_found');
  END IF;

  -- Lock the affected users before copying usage or changing their plan so
  -- concurrent migration calls serialize and repeated calls become no-ops.
  PERFORM 1
  FROM bursar.user_credits uc
  JOIN bursar.credit_plans cp ON cp.id = uc.plan_id
  WHERE cp.plan_key = v_from_key AND cp.id <> v_target_plan_id
  FOR UPDATE;

  INSERT INTO bursar.credit_usage_window (user_id, plan_id, billing_period, usage)
  SELECT uw.user_id, v_target_plan_id, uw.billing_period, uw.usage
  FROM bursar.credit_usage_window uw
  JOIN bursar.user_credits uc ON uc.user_id = uw.user_id
  JOIN bursar.credit_plans cp ON cp.id = uc.plan_id AND cp.id = uw.plan_id
  WHERE cp.plan_key = v_from_key AND cp.id <> v_target_plan_id
  ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
    usage = GREATEST(bursar.credit_usage_window.usage, EXCLUDED.usage),
    updated_at = now();

  INSERT INTO bursar.credit_plan_migrations
    (user_id, from_plan_id, to_plan_id, from_config_version, to_config_version, reason)
  SELECT uc.user_id, uc.plan_id, v_target_plan_id, cp.config_version,
         v_target_version, 'migrate_plan_users'
  FROM bursar.user_credits uc
  JOIN bursar.credit_plans cp ON cp.id = uc.plan_id
  WHERE cp.plan_key = v_from_key AND cp.id <> v_target_plan_id
  ON CONFLICT (user_id, to_plan_id, to_config_version)
    WHERE reason = 'migrate_plan_users' DO NOTHING;

  UPDATE bursar.user_credits uc
  SET plan_id = v_target_plan_id,
      catalog_version = v_target_version,
      updated_at = now()
  FROM bursar.credit_plans cp
  WHERE uc.plan_id = cp.id
    AND cp.plan_key = v_from_key
    AND cp.id <> v_target_plan_id;
  GET DIAGNOSTICS v_count = ROW_COUNT;

  RETURN jsonb_build_object(
    'plan_key', p_plan_key,
    'from_plan_key', v_from_key,
    'target_plan_id', v_target_plan_id,
    'target_config_version', v_target_version,
    'migrated_count', v_count
  );
END;
$$;

COMMENT ON FUNCTION bursar.migrate_plan_users(text, integer, text) IS
  'Atomically migrates users once, carries usage forward, and records an audit row.';
REVOKE ALL ON FUNCTION bursar.migrate_plan_users(text, integer, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION bursar.migrate_plan_users(text, integer, text) TO service_role;

-- Cursor-backed compatibility queries.  Legacy SDK methods may still accept
-- an offset, but they are implemented by walking these stable cursors rather
-- than exposing mutable OFFSET pagination from SQL.
CREATE FUNCTION bursar.list_transactions_cursor_with_total(
  p_user_id uuid,
  p_types text[] DEFAULT NULL,
  p_from_date timestamptz DEFAULT NULL,
  p_to_date timestamptz DEFAULT NULL,
  p_limit integer DEFAULT 50,
  p_cursor_created_at timestamptz DEFAULT NULL,
  p_cursor_id uuid DEFAULT NULL
) RETURNS TABLE(
  id uuid, user_id uuid, amount numeric, type text, reference_type text,
  reference_id uuid, metadata jsonb, created_at timestamptz,
  total_count bigint, next_cursor_created_at timestamptz, next_cursor_id uuid
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO ''
AS $$
  WITH filtered AS MATERIALIZED (
    SELECT ct.id, ct.user_id, ct.amount, ct.type::text, ct.reference_type,
           ct.reference_id, ct.metadata, ct.created_at
    FROM bursar.credit_transactions ct
    WHERE ct.user_id = p_user_id
      AND (p_types IS NULL OR ct.type::text = ANY(p_types))
      AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
      AND (p_to_date IS NULL OR ct.created_at < p_to_date)
  ), page AS (
    SELECT f.*, count(*) OVER () AS total_count
    FROM filtered f
    WHERE p_cursor_created_at IS NULL
       OR (f.created_at, f.id) < (p_cursor_created_at, p_cursor_id)
    ORDER BY f.created_at DESC, f.id DESC
    LIMIT LEAST(GREATEST(p_limit, 1), 200) + 1
  ), visible AS (
    SELECT * FROM page ORDER BY created_at DESC, id DESC
    LIMIT LEAST(GREATEST(p_limit, 1), 200)
  ), marker AS (
    SELECT created_at, id FROM visible ORDER BY created_at ASC, id ASC LIMIT 1
  )
  SELECT v.id, v.user_id, v.amount, v.type, v.reference_type, v.reference_id,
         v.metadata, v.created_at, v.total_count,
         CASE WHEN (SELECT count(*) FROM page) > (SELECT count(*) FROM visible)
              THEN (SELECT created_at FROM marker) END,
         CASE WHEN (SELECT count(*) FROM page) > (SELECT count(*) FROM visible)
              THEN (SELECT id FROM marker) END
  FROM visible v
  ORDER BY v.created_at DESC, v.id DESC;
$$;

CREATE FUNCTION bursar.list_usage_events_cursor(
  p_user_id uuid,
  p_from_date timestamptz DEFAULT NULL,
  p_to_date timestamptz DEFAULT NULL,
  p_limit integer DEFAULT 50,
  p_cursor_created_at timestamptz DEFAULT NULL,
  p_cursor_id uuid DEFAULT NULL
) RETURNS TABLE(
  id uuid, user_id uuid, amount numeric, type text, reference_type text,
  reference_id uuid, metadata jsonb, created_at timestamptz,
  total_count bigint, next_cursor_created_at timestamptz, next_cursor_id uuid
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO ''
AS $$
  WITH filtered AS MATERIALIZED (
    SELECT ct.id, ct.user_id, ct.amount, ct.type::text, ct.reference_type,
           ct.reference_id, ct.metadata, ct.created_at
    FROM bursar.credit_transactions ct
    WHERE ct.user_id = p_user_id
      AND ct.type = 'usage'
      AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
      AND (p_to_date IS NULL OR ct.created_at < p_to_date)
  ), page AS (
    SELECT f.*, count(*) OVER () AS total_count
    FROM filtered f
    WHERE p_cursor_created_at IS NULL
       OR (f.created_at, f.id) < (p_cursor_created_at, p_cursor_id)
    ORDER BY f.created_at DESC, f.id DESC
    LIMIT LEAST(GREATEST(p_limit, 1), 200) + 1
  ), visible AS (
    SELECT * FROM page ORDER BY created_at DESC, id DESC
    LIMIT LEAST(GREATEST(p_limit, 1), 200)
  ), marker AS (
    SELECT created_at, id FROM visible ORDER BY created_at ASC, id ASC LIMIT 1
  )
  SELECT v.id, v.user_id, v.amount, v.type, v.reference_type, v.reference_id,
         v.metadata, v.created_at, v.total_count,
         CASE WHEN (SELECT count(*) FROM page) > (SELECT count(*) FROM visible)
              THEN (SELECT created_at FROM marker) END,
         CASE WHEN (SELECT count(*) FROM page) > (SELECT count(*) FROM visible)
              THEN (SELECT id FROM marker) END
  FROM visible v
  ORDER BY v.created_at DESC, v.id DESC;
$$;

REVOKE ALL ON FUNCTION bursar.list_transactions_cursor_with_total(uuid, text[], timestamptz, timestamptz, integer, timestamptz, uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.list_transactions_cursor_with_total(uuid, text[], timestamptz, timestamptz, integer, timestamptz, uuid) TO service_role;
REVOKE ALL ON FUNCTION bursar.list_usage_events_cursor(uuid, timestamptz, timestamptz, integer, timestamptz, uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.list_usage_events_cursor(uuid, timestamptz, timestamptz, integer, timestamptz, uuid) TO service_role;
--
