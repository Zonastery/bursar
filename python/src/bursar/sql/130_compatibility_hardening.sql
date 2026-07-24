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
  IF NOT bursar.is_finite_numeric(p_initial_balance) OR p_initial_balance < 0 THEN
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
    usage = EXCLUDED.usage,
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
      plan_assigned_at = now(),
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

-- Canonical lot accounting: refunds reverse the original allocations in place.
-- A refund must not mint a fresh, non-expiring lot before provenance is recorded.
CREATE OR REPLACE FUNCTION bursar.allocate_ledger_entry_lots()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
  v_remaining numeric(18,4);
  v_available numeric(18,4);
  v_take numeric(18,4);
  v_lot record;
  v_is_expiry boolean := COALESCE(NEW.metadata->>'reason', '') = 'credit_expired';
  v_bucket text := NULLIF(NEW.metadata->>'bucket', '');
BEGIN
  IF NEW.entry_type = 'refund' OR NEW.reference_transaction_id IS NOT NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.amount > 0 THEN
    INSERT INTO bursar.credit_lots(account_id, source_entry_id, granted, expires_at, bucket)
    VALUES (NEW.account_id, NEW.id, NEW.amount,
            NULLIF(NEW.metadata->>'expires_at', '')::timestamptz,
            COALESCE(v_bucket, 'default'));
    RETURN NEW;
  END IF;
  v_remaining := -NEW.amount;
  FOR v_lot IN
    SELECT id, granted, consumed
    FROM bursar.credit_lots
    WHERE account_id = NEW.account_id
      AND consumed < granted
      AND (v_bucket IS NULL OR bucket = v_bucket)
      AND (v_is_expiry OR expires_at IS NULL OR expires_at > now())
    ORDER BY CASE WHEN v_is_expiry THEN expires_at END NULLS LAST,
             CASE WHEN NOT v_is_expiry THEN expires_at END NULLS LAST,
             created_at, id
    FOR UPDATE
  LOOP
    EXIT WHEN v_remaining <= 0;
    v_available := v_lot.granted - v_lot.consumed;
    v_take := LEAST(v_available, v_remaining);
    UPDATE bursar.credit_lots SET consumed = consumed + v_take WHERE id = v_lot.id;
    INSERT INTO bursar.credit_lot_allocations(debit_entry_id, lot_id, amount)
    VALUES (NEW.id, v_lot.id, v_take);
    v_remaining := v_remaining - v_take;
  END LOOP;
  IF v_remaining > 0 THEN
    INSERT INTO bursar.credit_lot_allocations(debit_entry_id, lot_id, amount)
    VALUES (NEW.id, NULL, v_remaining);
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION bursar.record_refund_lot_provenance()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
  v_remaining numeric(18,4) := NEW.amount;
  v_available numeric(18,4);
  v_take numeric(18,4);
  v_allocation record;
BEGIN
  IF NEW.entry_type <> 'refund' OR NEW.amount <= 0 OR NEW.reference_transaction_id IS NULL THEN
    RETURN NEW;
  END IF;
  FOR v_allocation IN
    SELECT a.id, a.lot_id, a.amount,
           a.amount - COALESCE((SELECT sum(r.amount)
                                FROM bursar.credit_lot_reversals r
                                WHERE r.original_allocation_id = a.id), 0) AS available
    FROM bursar.credit_lot_allocations a
    JOIN bursar.credit_ledger_entries d ON d.id = a.debit_entry_id
    WHERE d.source_transaction_id = NEW.reference_transaction_id
      AND a.lot_id IS NOT NULL
    ORDER BY a.created_at DESC, a.id DESC
    FOR UPDATE OF a
  LOOP
    EXIT WHEN v_remaining <= 0;
    v_available := GREATEST(v_allocation.available, 0);
    v_take := LEAST(v_available, v_remaining);
    IF v_take > 0 THEN
      INSERT INTO bursar.credit_lot_reversals(refund_entry_id, original_allocation_id, amount)
      VALUES (NEW.id, v_allocation.id, v_take);
      UPDATE bursar.credit_lots
      SET consumed = consumed - v_take
      WHERE id = v_allocation.lot_id;
      v_remaining := v_remaining - v_take;
    END IF;
  END LOOP;
  IF v_remaining > 0 THEN
    INSERT INTO bursar.credit_lot_allocations(debit_entry_id, lot_id, amount)
    VALUES (NEW.id, NULL, v_remaining);
  END IF;
  RETURN NEW;
END;
$$;

DROP FUNCTION IF EXISTS bursar.expire_credits_legacy(boolean, uuid);

DROP FUNCTION IF EXISTS bursar.validate_bursar_config(jsonb);
CREATE FUNCTION bursar.validate_bursar_config(p_config jsonb)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO ''
AS $$
DECLARE
  k text; v jsonb; n numeric; mode text; period text;
BEGIN
  IF p_config IS NULL OR jsonb_typeof(p_config) <> 'object' THEN
    RAISE EXCEPTION 'catalog config must be a JSON object' USING ERRCODE = '22023';
  END IF;
  IF jsonb_typeof(COALESCE(p_config->'plans','{}'::jsonb)) <> 'object'
     OR jsonb_typeof(COALESCE(p_config->'ledger'->'buckets','{}'::jsonb)) <> 'object'
     OR jsonb_typeof(COALESCE(p_config->'billing'->'offers','{}'::jsonb)) <> 'object'
     OR jsonb_typeof(COALESCE(p_config->'billing'->'topups','{}'::jsonb)) <> 'object' THEN
    RAISE EXCEPTION 'catalog sections must be objects' USING ERRCODE = '22023';
  END IF;
  FOR k,v IN SELECT * FROM jsonb_each(COALESCE(p_config->'plans','{}'::jsonb)) LOOP
    IF btrim(k) = '' OR jsonb_typeof(v) <> 'object' THEN RAISE EXCEPTION 'invalid plan %',k USING ERRCODE='22023'; END IF;
    mode := COALESCE(v #>> '{safety,billing_mode}', v->>'billing_mode', 'strict');
    period := COALESCE(v #>> '{allowance,period}', v->>'allowance_period', 'calendar_month');
    IF mode NOT IN ('strict','overdraft') OR period NOT IN ('calendar_month','rolling_30d','anniversary') THEN
      RAISE EXCEPTION 'invalid billing mode or allowance period for plan %', k USING ERRCODE='22023';
    END IF;
    BEGIN n := COALESCE((v #>> '{allowance,amount}')::numeric, (v->>'allowance_amount')::numeric, 0); EXCEPTION WHEN others THEN RAISE EXCEPTION 'invalid allowance amount for plan %',k USING ERRCODE='22023'; END;
    IF NOT bursar.is_finite_numeric(n) OR n < 0 THEN RAISE EXCEPTION 'invalid allowance amount for plan %',k USING ERRCODE='22023'; END IF;
  END LOOP;
  FOR k,v IN SELECT * FROM jsonb_each(COALESCE(p_config->'ledger'->'buckets','{}'::jsonb)) LOOP
    IF btrim(k) = '' OR jsonb_typeof(v) <> 'object' THEN RAISE EXCEPTION 'invalid bucket %',k USING ERRCODE='22023'; END IF;
    IF COALESCE((v->>'expires')::boolean,false) AND COALESCE((v->>'ttl_days')::int,(v->>'ttlDays')::int,0) <= 0 THEN
      RAISE EXCEPTION 'expiring bucket % requires positive ttl_days',k USING ERRCODE='22023';
    END IF;
  END LOOP;
END;
$$;
REVOKE ALL ON FUNCTION bursar.validate_bursar_config(jsonb) FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION bursar.expire_credits(
  p_dry_run boolean DEFAULT false,
  p_user_id uuid DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
  v_group record;
  v_expired numeric(18,4);
  v_count integer := 0;
  v_total numeric(18,4) := 0;
  v_by_bucket jsonb := '{}'::jsonb;
  v_account bursar.credit_accounts;
BEGIN
  FOR v_group IN
    SELECT l.account_id, l.bucket
    FROM bursar.credit_lots l
    JOIN bursar.credit_accounts a ON a.id = l.account_id
    WHERE a.account_type = 'personal'
      AND a.user_id IS NOT NULL
      AND (p_user_id IS NULL OR a.user_id = p_user_id)
      AND l.consumed < l.granted
      AND l.expires_at IS NOT NULL
      AND l.expires_at <= now()
    GROUP BY l.account_id, l.bucket
    ORDER BY l.account_id, l.bucket
  LOOP
    SELECT * INTO v_account
    FROM bursar.credit_accounts
    WHERE id = v_group.account_id
    FOR UPDATE;

    PERFORM 1
    FROM bursar.credit_lots
    WHERE account_id = v_group.account_id
      AND bucket = v_group.bucket
      AND consumed < granted
      AND expires_at IS NOT NULL
      AND expires_at <= now()
    FOR UPDATE;

    SELECT COALESCE(sum(granted - consumed), 0)::numeric(18,4)
    INTO v_expired
    FROM bursar.credit_lots
    WHERE account_id = v_group.account_id
      AND bucket = v_group.bucket
      AND consumed < granted
      AND expires_at IS NOT NULL
      AND expires_at <= now();

    IF v_expired <= 0 THEN CONTINUE; END IF;
    v_count := v_count + 1;
    v_total := v_total + v_expired;
    v_by_bucket := v_by_bucket || jsonb_build_object(
      v_group.bucket, COALESCE((v_by_bucket->>v_group.bucket)::numeric, 0) + v_expired);

    IF NOT p_dry_run THEN
      INSERT INTO bursar.user_credit_buckets(user_id, bucket_key, balance)
      VALUES (v_account.user_id, v_group.bucket, 0)
      ON CONFLICT (user_id, bucket_key) DO NOTHING;
      UPDATE bursar.user_credit_buckets
      SET balance = balance - v_expired, updated_at = now()
      WHERE user_id = v_account.user_id AND bucket_key = v_group.bucket;
      UPDATE bursar.user_credits
      SET balance = balance - v_expired, updated_at = now()
      WHERE user_id = v_account.user_id;
      INSERT INTO bursar.credit_transactions(user_id, amount, type, account_id, acting_user_id, metadata)
      VALUES (v_account.user_id, -v_expired, 'adjustment', v_account.id, v_account.user_id,
              jsonb_build_object('reason','credit_expired','expired_amount',v_expired,'bucket',v_group.bucket));
    END IF;
  END LOOP;
  RETURN jsonb_build_object('expired_count', v_count, 'expired_amount', v_total,
                            'by_bucket', v_by_bucket, 'dry_run', p_dry_run);
END;
$$;

DROP FUNCTION IF EXISTS bursar.list_transactions_cursor_with_total(uuid, text[], timestamptz, timestamptz, integer, timestamptz, uuid);
DROP FUNCTION IF EXISTS bursar.list_transactions_cursor_with_total(uuid, text[], timestamp, timestamp, integer, timestamp, uuid);
CREATE FUNCTION bursar.list_transactions_cursor_with_total(
  p_user_id uuid DEFAULT NULL,
  p_types text[] DEFAULT NULL,
  p_from_date timestamptz DEFAULT NULL,
  p_to_date timestamptz DEFAULT NULL,
  p_limit integer DEFAULT 50,
  p_cursor_created_at timestamptz DEFAULT NULL,
  p_cursor_id uuid DEFAULT NULL
)
RETURNS TABLE(
  id uuid, user_id uuid, amount numeric, type text, reference_type text,
  reference_id uuid, metadata jsonb, created_at timestamptz, total_count bigint,
  next_cursor_created_at timestamptz, next_cursor_id uuid
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO ''
AS $$
  WITH filtered AS (
    SELECT ct.id, ct.user_id, ct.amount, ct.type::text, ct.reference_type,
           ct.reference_id, ct.metadata, ct.created_at
    FROM bursar.credit_transactions ct
    WHERE (p_user_id IS NULL OR ct.user_id = p_user_id)
      AND (p_types IS NULL OR ct.type::text = ANY(p_types))
      AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
      AND (p_to_date IS NULL OR ct.created_at < p_to_date)
  ), page AS (
    SELECT f.*, (SELECT count(*) FROM filtered) AS total_count,
           row_number() OVER (ORDER BY f.created_at DESC, f.id DESC) AS rn
    FROM filtered f
    WHERE (p_cursor_created_at IS NULL AND p_cursor_id IS NULL)
       OR (p_cursor_created_at IS NOT NULL AND p_cursor_id IS NOT NULL
           AND (f.created_at, f.id) < (p_cursor_created_at, p_cursor_id))
    ORDER BY f.created_at DESC, f.id DESC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200) + 1
  ), visible AS (
    SELECT * FROM page
    WHERE rn <= LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)
  ), marker AS (
    SELECT created_at, id FROM page
    ORDER BY created_at ASC, id ASC LIMIT 1
  )
  SELECT v.id, v.user_id, v.amount, v.type, v.reference_type, v.reference_id,
         v.metadata, v.created_at, v.total_count,
         CASE WHEN EXISTS (SELECT 1 FROM page WHERE rn > (SELECT count(*) FROM visible))
              THEN (SELECT created_at FROM marker) END,
         CASE WHEN EXISTS (SELECT 1 FROM page WHERE rn > (SELECT count(*) FROM visible))
              THEN (SELECT id FROM marker) END
  FROM visible v
  ORDER BY v.created_at DESC, v.id DESC;
$$;

DROP FUNCTION IF EXISTS bursar.list_usage_events_cursor(uuid, timestamptz, timestamptz, integer, timestamptz, uuid);
DROP FUNCTION IF EXISTS bursar.list_usage_events_cursor(uuid, timestamp, timestamp, integer, timestamp, uuid);
CREATE FUNCTION bursar.list_usage_events_cursor(
  p_user_id uuid DEFAULT NULL,
  p_from_date timestamptz DEFAULT NULL,
  p_to_date timestamptz DEFAULT NULL,
  p_limit integer DEFAULT 50,
  p_cursor_created_at timestamptz DEFAULT NULL,
  p_cursor_id uuid DEFAULT NULL
)
RETURNS TABLE(
  id uuid, user_id uuid, amount numeric, type text, reference_type text,
  reference_id uuid, metadata jsonb, created_at timestamptz, total_count bigint,
  next_cursor_created_at timestamptz, next_cursor_id uuid
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO ''
AS $$
  SELECT * FROM bursar.list_transactions_cursor_with_total(
    p_user_id, ARRAY['usage','team_usage']::text[], p_from_date, p_to_date,
    p_limit, p_cursor_created_at, p_cursor_id
  );
$$;

COMMENT ON FUNCTION bursar.migrate_plan_users(text, integer, text) IS
  'Atomically migrates users once, carries usage forward, and records an audit row.';
REVOKE ALL ON FUNCTION bursar.migrate_plan_users(text, integer, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION bursar.migrate_plan_users(text, integer, text) TO service_role;

-- Cursor-backed compatibility queries.  Legacy SDK methods may still accept
-- an offset, but they are implemented by walking these stable cursors rather
-- than exposing mutable OFFSET pagination from SQL.
CREATE FUNCTION bursar.list_transactions_cursor_with_total_legacy(
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

CREATE FUNCTION bursar.list_usage_events_cursor_legacy(
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
