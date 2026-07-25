
-- ============================================================================
-- Source: 080_billing_functions.sql
-- ============================================================================

-- Name: expire_due_leases(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.expire_due_leases() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE bursar.credit_reservations SET status = 'expired'
    WHERE status = 'active' AND expires_at <= now();
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN jsonb_build_object('expired_count', v_count);
END;
$$;


--
-- Name: fail_billing_event(text, text, uuid, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.fail_billing_event(p_provider text, p_event_id text, p_claim_token uuid, p_error text DEFAULT NULL::text) RETURNS boolean
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO ''
    AS $$
  UPDATE bursar.billing_events
  SET status = 'failed', claim_token = NULL, claim_expires_at = NULL,
      envelope = envelope || jsonb_build_object('error', left(coalesce(p_error, 'failed'), 4000)), updated_at = now()
  WHERE provider = p_provider AND provider_event_id = p_event_id AND status = 'processing'
    AND claim_token = p_claim_token AND claim_expires_at >= now()
  RETURNING true
$$;


--
-- Name: get_active_bursar_config(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_active_bursar_config() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_config JSONB;
    v_version INTEGER;
    v_id UUID;
BEGIN
    SELECT id, config, version INTO v_id, v_config, v_version
    FROM bursar.bursar_config
    WHERE active = true
    ORDER BY created_at DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'id', v_id,
        'config', v_config,
        'version', v_version
    );
END;
$$;


--
-- Name: get_available_credits(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_available_credits(p_user_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_balance  NUMERIC;
    v_reserved NUMERIC;
BEGIN
    SELECT COALESCE(balance, 0) INTO v_balance FROM bursar.user_credits WHERE user_id = p_user_id;
    v_balance := COALESCE(v_balance, 0);
    SELECT COALESCE(SUM(amount), 0) INTO v_reserved
    FROM bursar.credit_reservations
    WHERE user_id = p_user_id AND status = 'active' AND expires_at > now();

    RETURN jsonb_build_object(
        'user_id', p_user_id, 'balance', v_balance,
        'reserved', v_reserved, 'available', v_balance - v_reserved
    );
END;
$$;


--
-- Name: get_billing_customer(text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_billing_customer(p_provider text, p_provider_customer_id text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_user_id UUID;
BEGIN
    SELECT user_id INTO v_user_id
    FROM bursar.billing_customers
    WHERE provider = p_provider AND provider_customer_id = p_provider_customer_id
    LIMIT 1;

    IF v_user_id IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object('user_id', v_user_id);
END;
$$;


--
-- Name: get_billing_customer_by_user_id(uuid, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_billing_customer_by_user_id(p_user_id uuid, p_provider text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_row RECORD;
BEGIN
    IF p_provider IS NOT NULL THEN
        SELECT provider, provider_customer_id INTO v_row
        FROM bursar.billing_customers
        WHERE user_id = p_user_id AND provider = p_provider
        ORDER BY updated_at DESC
        LIMIT 1;
    ELSE
        SELECT provider, provider_customer_id INTO v_row
        FROM bursar.billing_customers
        WHERE user_id = p_user_id
        ORDER BY updated_at DESC
        LIMIT 1;
    END IF;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'provider', v_row.provider,
        'provider_customer_id', v_row.provider_customer_id
    );
END;
$$;


--
-- Name: get_billing_payment_for_refund(text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_billing_payment_for_refund(p_provider text, p_provider_payment_id text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_payment RECORD;
BEGIN
    SELECT purpose, amount_minor, currency, user_id, metadata
    INTO v_payment
    FROM bursar.billing_payments
    WHERE provider = p_provider AND provider_payment_id = p_provider_payment_id
    LIMIT 1;

    IF v_payment.purpose IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'purpose', v_payment.purpose,
        'amount_minor', v_payment.amount_minor,
        'currency', v_payment.currency,
        'user_id', v_payment.user_id,
        'metadata', v_payment.metadata
    );
END;
$$;


--
-- Name: get_billing_preferences(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_billing_preferences(p_user_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_row RECORD;
BEGIN
    SELECT * INTO v_row FROM bursar.billing_preferences WHERE user_id = p_user_id LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'user_id', v_row.user_id,
        'auto_recharge', v_row.auto_recharge,
        'overage_protection', v_row.overage_protection,
        'email_notifications', v_row.email_notifications,
        'usage_alerts', v_row.usage_alerts,
        'invoice_reminders', v_row.invoice_reminders,
        'usage_limit_alerts', v_row.usage_limit_alerts
    );
END;
$$;


--
-- Name: get_billing_subscription(text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_billing_subscription(p_provider text, p_provider_subscription_id text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_row RECORD;
BEGIN
    SELECT
        user_id, provider, provider_subscription_id, provider_customer_id,
        offer_key, plan, status, current_period_start,
        current_period_end, cancel_at_period_end, interval, interval_count, metadata
    INTO v_row
    FROM bursar.billing_subscriptions
    WHERE provider = p_provider AND provider_subscription_id = p_provider_subscription_id
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'user_id', v_row.user_id,
        'provider', v_row.provider,
        'provider_subscription_id', v_row.provider_subscription_id,
        'provider_customer_id', v_row.provider_customer_id,
        'offer_key', v_row.offer_key,
        'plan', v_row.plan,
        'status', v_row.status,
        'current_period_start', v_row.current_period_start,
        'current_period_end', v_row.current_period_end,
        'cancel_at_period_end', v_row.cancel_at_period_end,
        'interval', v_row.interval,
        'interval_count', v_row.interval_count,
        'metadata', v_row.metadata
    );
END;
$$;


--
-- Name: get_bursar_config(integer); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_bursar_config(p_version integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_config JSONB;
    v_id UUID;
    v_version INTEGER;
BEGIN
    SELECT id, config, version INTO v_id, v_config, v_version
    FROM bursar.bursar_config
    WHERE version = p_version
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'id', v_id,
        'config', v_config,
        'version', v_version
    );
END;
$$;


--
-- Name: get_bursar_configs(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_bursar_configs() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
    RETURN (
        SELECT jsonb_agg(
            jsonb_build_object(
                'id', id,
                'version', version,
                'label', label,
                'active', active,
                'created_at', created_at
            )
            ORDER BY version DESC
        )
        FROM bursar.bursar_config
    );
END;
$$;


--
-- Name: get_credits_balance(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_credits_balance(p_user_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_balance NUMERIC;
    v_lifetime NUMERIC;
BEGIN
    SELECT balance, lifetime_purchased INTO v_balance, v_lifetime
    FROM bursar.user_credits
    WHERE user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'balance', COALESCE(v_balance, 0),
        'lifetime_purchased', COALESCE(v_lifetime, 0)
    );
END;
$$;


--
-- Name: get_team_balance(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_team_balance(p_team_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_team RECORD;
BEGIN
  SELECT id, name, balance, member_count INTO v_team
  FROM bursar.credit_teams
  WHERE id = p_team_id;

  IF v_team.id IS NULL THEN
    RETURN jsonb_build_object('error', 'team_not_found');
  END IF;

  RETURN jsonb_build_object(
    'team_id', v_team.id,
    'name', v_team.name,
    'balance', v_team.balance,
    'member_count', v_team.member_count
  );
END;
$$;


--
-- Name: get_team_members(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_team_members(p_team_id uuid) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
  RETURN QUERY
  SELECT jsonb_build_object(
    'user_id', tm.user_id,
    'role', tm.role,
    'spend_cap', tm.spend_cap,
    'total_spent', COALESCE(SUM(ABS(ct.amount)) FILTER (
        WHERE ct.type = 'team_usage'
          AND ct.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
      ), 0),
    'joined_at', tm.joined_at
  )
  FROM bursar.credit_team_members tm
  LEFT JOIN bursar.credit_transactions ct
    ON ct.user_id = tm.user_id
   AND ct.metadata->>'team_id' = p_team_id::text
  WHERE tm.team_id = p_team_id
  GROUP BY tm.user_id, tm.role, tm.spend_cap, tm.joined_at
  ORDER BY tm.joined_at;
END;
$$;


--
-- Name: get_user_billing_subscription(uuid, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_user_billing_subscription(p_user_id uuid, p_provider text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_row RECORD;
BEGIN
    IF p_provider IS NOT NULL THEN
        SELECT * INTO v_row
        FROM bursar.billing_subscriptions
        WHERE user_id = p_user_id AND provider = p_provider
        ORDER BY current_period_start DESC NULLS LAST, created_at DESC
        LIMIT 1;
    ELSE
        SELECT * INTO v_row
        FROM bursar.billing_subscriptions
        WHERE user_id = p_user_id
        ORDER BY current_period_start DESC NULLS LAST, created_at DESC
        LIMIT 1;
    END IF;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'user_id', v_row.user_id,
        'provider', v_row.provider,
        'provider_subscription_id', v_row.provider_subscription_id,
        'provider_customer_id', v_row.provider_customer_id,
        'offer_key', v_row.offer_key,
        'plan', v_row.plan,
        'status', v_row.status,
        'current_period_start', v_row.current_period_start,
        'current_period_end', v_row.current_period_end,
        'cancel_at_period_end', v_row.cancel_at_period_end,
        'interval', v_row.interval,
        'interval_count', v_row.interval_count,
        'metadata', v_row.metadata
    );
END;
$$;


--
-- Name: get_user_billing_subscriptions(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_user_billing_subscriptions(p_user_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_result JSONB;
BEGIN
    SELECT COALESCE(jsonb_agg(
        jsonb_build_object(
            'user_id', bs.user_id,
            'provider', bs.provider,
            'provider_subscription_id', bs.provider_subscription_id,
            'provider_customer_id', bs.provider_customer_id,
            'offer_key', bs.offer_key,
            'plan', bs.plan,
            'status', bs.status,
            'current_period_start', bs.current_period_start,
            'current_period_end', bs.current_period_end,
            'cancel_at_period_end', bs.cancel_at_period_end,
            'interval', bs.interval,
            'interval_count', bs.interval_count,
            'metadata', bs.metadata
        )
        ORDER BY bs.current_period_start DESC NULLS LAST, bs.created_at DESC
    ), '[]'::JSONB) INTO v_result
    FROM bursar.billing_subscriptions bs
    WHERE bs.user_id = p_user_id;

    RETURN v_result;
END;
$$;


--
-- Name: get_user_credit_buckets(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_user_credit_buckets(p_user_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_total_balance NUMERIC;
    v_buckets JSONB;
    v_bucket_count INTEGER;
BEGIN
    SELECT COALESCE(balance, 0) INTO v_total_balance
    FROM bursar.user_credits
    WHERE user_id = p_user_id;

    SELECT COUNT(*) INTO v_bucket_count FROM bursar.credit_buckets;

    IF v_bucket_count = 0 THEN
        v_buckets := jsonb_build_array(
            jsonb_build_object(
                'bucket_key', 'default',
                'label', 'default',
                'priority', 0,
                'expires', false,
                'balance', COALESCE(v_total_balance, 0)
            )
        );
    ELSE
        SELECT COALESCE(jsonb_agg(
            jsonb_build_object(
                'bucket_key', cb.bucket_key,
                'label', cb.label,
                'priority', cb.priority,
                'expires', cb.expires,
                'balance', COALESCE(ucb.balance, 0)
            )
            ORDER BY cb.priority ASC, cb.bucket_key ASC
        ), '[]'::jsonb) INTO v_buckets
        FROM bursar.credit_buckets cb
        LEFT JOIN bursar.user_credit_buckets ucb
            ON ucb.bucket_key = cb.bucket_key AND ucb.user_id = p_user_id;
    END IF;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'buckets', v_buckets,
        'total_balance', COALESCE(v_total_balance, 0)
    );
END;
$$;


--
-- Name: get_user_plan(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.get_user_plan(p_user_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_plan_id UUID;
    v_plan_label TEXT;
    v_allowance_amount NUMERIC;
    v_entitlements JSONB;
    v_rate_overrides JSONB;
    v_billing_mode TEXT;
    v_per_operation JSONB;
    v_max_concurrent INTEGER;
    v_overdraft_floor NUMERIC;
    v_allowance_period TEXT;
    v_plan_assigned_at TIMESTAMPTZ;
    v_config_version INTEGER;
    v_catalog_version INTEGER;
BEGIN
    SELECT uc.plan_id, cp.label, cp.allowance_amount, cp.entitlements, cp.rate_overrides,
           cp.billing_mode, cp.per_operation, cp.max_concurrent, cp.overdraft_floor,
           cp.allowance_period, uc.plan_assigned_at, cp.config_version, uc.catalog_version
    INTO v_plan_id, v_plan_label, v_allowance_amount, v_entitlements, v_rate_overrides,
         v_billing_mode, v_per_operation, v_max_concurrent, v_overdraft_floor,
         v_allowance_period, v_plan_assigned_at, v_config_version, v_catalog_version
    FROM bursar.user_credits uc
    LEFT JOIN bursar.credit_plans cp ON cp.id = uc.plan_id
    WHERE uc.user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_label', v_plan_label,
        'allowance_amount', COALESCE(v_allowance_amount, 0),
        'entitlements', COALESCE(v_entitlements, '{}'::jsonb),
        'rate_overrides', COALESCE(v_rate_overrides, '{}'::jsonb),
        'billing_mode', COALESCE(v_billing_mode, 'strict'),
        'per_operation', COALESCE(v_per_operation, '{}'::jsonb),
        'max_concurrent', v_max_concurrent,
        'overdraft_floor', v_overdraft_floor,
        'allowance_period', COALESCE(v_allowance_period, 'calendar_month'),
        'plan_assigned_at', v_plan_assigned_at,
        'config_version', v_config_version,
        'catalog_version', COALESCE(v_catalog_version, v_config_version)
    );
END;
$$;


--


-- ============================================================================
-- Source: 090_lease_catalog_functions.sql
-- ============================================================================

-- Name: grant_signup_bonus(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.grant_signup_bonus() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_config JSONB;
  v_grant JSONB;
  v_bonus NUMERIC;
  v_bucket TEXT;
  v_result JSONB;
BEGIN
  SELECT config INTO v_config
  FROM bursar.bursar_config
  WHERE active = TRUE
  LIMIT 1;

  IF v_config IS NULL THEN
    RETURN NEW;
  END IF;

  v_grant := COALESCE(v_config #> '{credits,signup_grant}', v_config #> '{ledger,signup_grant}');

  IF v_grant IS NULL OR jsonb_typeof(v_grant) <> 'object' THEN
    RETURN NEW;
  END IF;

  v_bonus := COALESCE((v_grant->>'amount')::numeric, 0);
  v_bucket := v_grant->>'bucket';

  IF v_bonus <= 0 OR v_bucket IS NULL OR v_bucket = '' THEN
    RETURN NEW;
  END IF;

  v_result := bursar.credits_add_internal(NEW.id, v_bonus, 'signup_bonus', NULL, v_bucket);

  IF v_result ? 'error' THEN
    INSERT INTO bursar.signup_grant_failures (user_id, error)
    VALUES (NEW.id, v_result);
    RAISE WARNING 'grant_signup_bonus failed for user %: %', NEW.id, v_result;
  END IF;

  RETURN NEW;
END;
$$;


--
-- Name: increment_usage_window(uuid, uuid, numeric, date); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.increment_usage_window(p_user_id uuid, p_plan_id uuid, p_amount numeric, p_period_start date DEFAULT NULL::date) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_period_start DATE;
    v_new_usage NUMERIC;
BEGIN
    IF NOT bursar.is_finite_numeric(p_amount) OR p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);

    INSERT INTO bursar.credit_usage_window (user_id, plan_id, billing_period, usage)
    VALUES (p_user_id, p_plan_id, v_period_start, p_amount)
    ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
        usage = bursar.credit_usage_window.usage + p_amount,
        updated_at = now()
    RETURNING usage INTO v_new_usage;

    RETURN jsonb_build_object(
        'usage', v_new_usage,
        'period_start', v_period_start::TEXT
    );
END;
$$;


--
-- Name: list_transactions_cursor(uuid, text[], timestamp with time zone, timestamp with time zone, integer, timestamp with time zone, uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.list_transactions_cursor(p_user_id uuid, p_types text[] DEFAULT NULL::text[], p_from_date timestamp with time zone DEFAULT NULL::timestamp with time zone, p_to_date timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 50, p_cursor_created_at timestamp with time zone DEFAULT NULL::timestamp with time zone, p_cursor_id uuid DEFAULT NULL::uuid) RETURNS TABLE(id uuid, user_id uuid, amount numeric, type text, reference_type text, reference_id uuid, metadata jsonb, created_at timestamp with time zone, next_cursor_created_at timestamp with time zone, next_cursor_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
  WITH page AS (
    SELECT ct.id, ct.user_id, ct.amount, ct.type::text, ct.reference_type,
           ct.reference_id, ct.metadata, ct.created_at
    FROM bursar.credit_transactions ct
    WHERE ct.user_id = p_user_id
      AND (p_types IS NULL OR ct.type::text = ANY(p_types))
      AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
      AND (p_to_date IS NULL OR ct.created_at < p_to_date)
      AND (p_cursor_created_at IS NULL OR (ct.created_at, ct.id) < (p_cursor_created_at, p_cursor_id))
    ORDER BY ct.created_at DESC, ct.id DESC
    LIMIT LEAST(GREATEST(p_limit, 1), 200) + 1
  ), visible AS (
    SELECT * FROM page ORDER BY created_at DESC, id DESC LIMIT LEAST(GREATEST(p_limit, 1), 200)
  ), marker AS (
    SELECT created_at, id FROM visible ORDER BY created_at ASC, id ASC LIMIT 1
  )
  SELECT v.id, v.user_id, v.amount, v.type, v.reference_type, v.reference_id,
         v.metadata, v.created_at,
         CASE WHEN (SELECT count(*) FROM page) > (SELECT count(*) FROM visible) THEN (SELECT created_at FROM marker) END,
         CASE WHEN (SELECT count(*) FROM page) > (SELECT count(*) FROM visible) THEN (SELECT id FROM marker) END
  FROM visible v
  ORDER BY v.created_at DESC, v.id DESC;
$$;


--
-- Name: prevent_bursar_config_payload_mutation(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.prevent_bursar_config_payload_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
    IF NEW.config IS DISTINCT FROM OLD.config
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'catalog versions are immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: project_credit_transaction(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.project_credit_transaction() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  account bursar.credit_accounts;
  key text := NEW.idempotency_key;
BEGIN
  IF NEW.amount = 0 THEN RETURN NEW; END IF;
  SELECT * INTO account FROM bursar.credit_accounts WHERE id = NEW.account_id FOR UPDATE;
  IF account.id IS NULL THEN RAISE EXCEPTION 'credit transaction is missing a charged account'; END IF;
  INSERT INTO bursar.credit_ledger_entries(
    account_id, source_transaction_id, reference_transaction_id, amount,
    entry_type, idempotency_key, metadata
  ) VALUES (
    account.id, NEW.id, NEW.reference_id, NEW.amount,
    NEW.type::text, key, coalesce(NEW.metadata, '{}'::jsonb)
  )
  ;
  UPDATE bursar.credit_accounts SET balance = balance + NEW.amount, updated_at = now() WHERE id = account.id;
  RETURN NEW;
END $$;


--
-- Name: pseudonymize_financial_subject(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.pseudonymize_financial_subject(p_user_id uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE v_subject uuid := (substr(md5('bursar-retention:' || p_user_id::text), 1, 8) || '-' || substr(md5('bursar-retention:' || p_user_id::text), 9, 4) || '-' || substr(md5('bursar-retention:' || p_user_id::text), 13, 4) || '-' || substr(md5('bursar-retention:' || p_user_id::text), 17, 4) || '-' || substr(md5('bursar-retention:' || p_user_id::text), 21, 12))::uuid;
BEGIN
  UPDATE bursar.billing_customers SET subject_id = v_subject, user_id = NULL, email = NULL WHERE user_id = p_user_id;
  UPDATE bursar.billing_subscriptions SET subject_id = v_subject, user_id = NULL WHERE user_id = p_user_id;
  UPDATE bursar.billing_invoices SET subject_id = v_subject, user_id = NULL WHERE user_id = p_user_id;
  UPDATE bursar.billing_payments SET subject_id = v_subject, user_id = NULL WHERE user_id = p_user_id;
  UPDATE bursar.billing_refunds SET subject_id = v_subject, user_id = NULL WHERE user_id = p_user_id;
  UPDATE bursar.billing_disputes SET subject_id = v_subject, user_id = NULL WHERE user_id = p_user_id;
  DELETE FROM bursar.billing_preferences WHERE user_id = p_user_id;
    UPDATE bursar.credit_accounts SET subject_id = v_subject, user_id = NULL WHERE user_id = p_user_id AND account_type = 'personal';
    RETURN v_subject;
END $$;


--
-- Name: publish_bursar_config(jsonb, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.publish_bursar_config(p_config jsonb, p_label text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_new_id uuid;
    v_next_version integer;
BEGIN
    PERFORM bursar.validate_bursar_config(p_config);
    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));
    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version FROM bursar.bursar_config;
    INSERT INTO bursar.bursar_config (config, active, version, label)
    VALUES (p_config, false, v_next_version, p_label)
    RETURNING id INTO v_new_id;
    RETURN jsonb_build_object('id', v_new_id, 'version', v_next_version, 'active', false);
END;
$$;


--
-- Name: reclaim_billing_event(text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.reclaim_billing_event(p_provider text, p_event_id text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_existing RECORD;
    v_token uuid := gen_random_uuid();
BEGIN
    SELECT * INTO v_existing FROM bursar.billing_events
    WHERE provider = p_provider AND provider_event_id = p_event_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'not_found');
    END IF;

    IF v_existing.status = 'completed' THEN
        RETURN jsonb_build_object('status', 'already_completed');
    END IF;

    IF v_existing.status = 'processing' THEN
        IF v_existing.updated_at >= now() - interval '5 minutes' THEN
            RETURN jsonb_build_object('status', 'retry');
        END IF;
    END IF;

    IF v_existing.status = 'failed' AND v_existing.retry_count >= 3 THEN
        RETURN jsonb_build_object('status', 'max_retries_exceeded');
    END IF;

    UPDATE bursar.billing_events
    SET status = 'processing', updated_at = now(), retry_count = v_existing.retry_count + 1,
        claim_token = v_token, claim_expires_at = now() + interval '5 minutes'
    WHERE id = v_existing.id;

    RETURN jsonb_build_object('status', 'reclaimed', 'event_id', v_existing.id, 'claim_token', v_token);
END;
$$;


--
-- Name: reconcile_credit_account(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.reconcile_credit_account(p_account_id uuid) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
    SELECT jsonb_build_object(
        'account_id', a.id,
        'projected_balance', a.balance,
        'ledger_balance', COALESCE((SELECT sum(e.amount) FROM bursar.credit_ledger_entries e WHERE e.account_id = a.id), 0),
        'matches', a.balance = COALESCE((SELECT sum(e.amount) FROM bursar.credit_ledger_entries e WHERE e.account_id = a.id), 0)
    )
    FROM bursar.credit_accounts a
    WHERE a.id = p_account_id;
$$;


--
-- Name: record_refund_lot_provenance(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.record_refund_lot_provenance() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_remaining numeric(18,4);
  v_available numeric(18,4);
  v_take numeric(18,4);
  v_allocation record;
BEGIN
  IF NEW.amount <= 0 OR NEW.reference_transaction_id IS NULL OR NEW.entry_type <> 'refund' THEN
    RETURN NEW;
  END IF;
  v_remaining := NEW.amount;
  FOR v_allocation IN
    SELECT a.id,
           a.amount - COALESCE((
             SELECT sum(r.amount)
             FROM bursar.credit_lot_reversals r
             WHERE r.original_allocation_id = a.id
           ), 0) AS available
    FROM bursar.credit_lot_allocations a
    JOIN bursar.credit_ledger_entries debit ON debit.id = a.debit_entry_id
    WHERE debit.source_transaction_id = NEW.reference_transaction_id
      AND a.lot_id IS NOT NULL
      AND a.amount > COALESCE((
        SELECT sum(r.amount)
        FROM bursar.credit_lot_reversals r
        WHERE r.original_allocation_id = a.id
      ), 0)
    ORDER BY a.created_at DESC, a.id DESC
    FOR UPDATE OF a
  LOOP
    EXIT WHEN v_remaining = 0;
    v_available := v_allocation.available;
    v_take := LEAST(v_available, v_remaining);
    INSERT INTO bursar.credit_lot_reversals (refund_entry_id, original_allocation_id, amount)
    VALUES (NEW.id, v_allocation.id, v_take);
    v_remaining := v_remaining - v_take;
  END LOOP;
  RETURN NEW;
END;
$$;


--
-- Name: refund_credits(uuid, numeric, text, jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.refund_credits(p_transaction_id uuid, p_amount numeric DEFAULT NULL::numeric, p_reason text DEFAULT NULL::text, p_metadata jsonb DEFAULT '{}'::jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_tx RECORD;
    v_already_refunded BOOLEAN;
    v_original_debit NUMERIC;      -- positive magnitude of the original debit
    v_prior_refunded NUMERIC;      -- sum of all prior refunds for this original
    v_remaining NUMERIC;           -- still-refundable amount
    v_refund_amount NUMERIC;
    v_new_balance NUMERIC;
    v_refund_tx_id UUID;
    -- Bucket LIFO restoration
    v_orig_breakdown JSONB;
    v_prior_refund_breakdown JSONB;
    v_new_breakdown JSONB := '{}'::jsonb;
    v_to_allocate NUMERIC;
    v_bucket_key TEXT;
    v_bucket_orig_amt NUMERIC;
    v_bucket_prior NUMERIC;
    v_bucket_remaining NUMERIC;
    v_give NUMERIC;
BEGIN
    -- Prevent concurrent refund on same transaction (advisory + row locks below).
    PERFORM pg_advisory_xact_lock(hashtext('refund_' || p_transaction_id));

    -- Fetch + lock the original transaction row so its refund total cannot move
    -- under us while we compute the over-refund check. metadata is selected so
    -- the bucket_breakdown driving LIFO restoration is available.
    SELECT id, user_id, amount, type, metadata INTO v_tx
    FROM bursar.credit_transactions
    WHERE id = p_transaction_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'error', 'not_found',
            'user_id', NULL::UUID,
            'new_balance', 0
        );
    END IF;

    -- Lock the balance row up front. Same lock the debit took, so a refund and a
    -- concurrent deduct on the same user serialize. Created if missing (the row
    -- should already exist for any user with a prior debit, but be defensive).
    SELECT balance INTO v_new_balance
    FROM bursar.user_credits
    WHERE user_id = v_tx.user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        INSERT INTO bursar.user_credits (user_id, balance, lifetime_purchased)
        VALUES (v_tx.user_id, 0, 0)
        ON CONFLICT (user_id) DO NOTHING;

        SELECT balance INTO v_new_balance
        FROM bursar.user_credits
        WHERE user_id = v_tx.user_id
        FOR UPDATE;
    END IF;

    -- (2) Reject refunding a non-debit. Only a `usage`/`team_usage` deduction
    -- (negative amount) is refundable. A purchase / refund / adjustment / bonus
    -- has nothing to give back, so its refundable amount is 0 and ANY refund
    -- over-refunds. We return `over_refund` (not `not_found`) because the row
    -- DOES exist — `not_found` would be misleading; `over_refund` precisely says
    -- "more than is refundable" (which for a non-debit is anything > 0).
    IF v_tx.type NOT IN ('usage', 'team_usage') OR v_tx.amount >= 0 THEN
        RETURN jsonb_build_object(
            'error', 'over_refund',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- Positive magnitude of the original debit (amount is negative for a debit).
    v_original_debit := ABS(v_tx.amount);

    -- (3a) Back-compat duplicate detection: a prior FULL refund of this exact
    -- transaction (one refund row whose amount equals the full original debit)
    -- replays as `already_refunded`. Cumulative partials are NOT treated as
    -- duplicates here — they fall through to the over-refund cap in (1)/(3b).
    SELECT EXISTS (
        SELECT 1 FROM bursar.credit_transactions
        WHERE reference_id = p_transaction_id
          AND type = 'refund'
          AND amount = v_original_debit
    ) INTO v_already_refunded;

    IF v_already_refunded THEN
        RETURN jsonb_build_object(
            'error', 'already_refunded',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- Determine the requested refund amount (NULL ⇒ full remaining).
    -- Sum of all prior refunds for this original (refund rows store a positive
    -- amount). Read under the FOR UPDATE lock taken above.
    SELECT COALESCE(SUM(amount), 0) INTO v_prior_refunded
    FROM bursar.credit_transactions
    WHERE reference_id = p_transaction_id
      AND type = 'refund';

    v_remaining := v_original_debit - v_prior_refunded;

    -- Requested amount: explicit value, else the full remaining refundable.
    IF p_amount IS NOT NULL AND NOT bursar.is_finite_numeric(p_amount) THEN
        RETURN jsonb_build_object('error', 'over_refund', 'user_id', v_tx.user_id, 'new_balance', COALESCE(v_new_balance, 0));
    END IF;
    v_refund_amount := COALESCE(p_amount, v_remaining);

    -- (1) Over-refund rejection: prior refunds + this refund must not exceed the
    -- original debit. Equivalently: this refund must not exceed what remains.
    -- A non-positive request (<= 0), or one that exceeds the remaining balance
    -- (including the case where the original is already fully refunded so
    -- v_remaining = 0), is rejected WITHOUT refunding.
    IF v_refund_amount <= 0 OR v_refund_amount > v_remaining THEN
        RETURN jsonb_build_object(
            'error', 'over_refund',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- (3b) Apply: restore balance and append the refund ledger row. Cumulative
    -- partials accumulate via successive refund rows; the cap above guarantees
    -- the running total never exceeds v_original_debit.
    UPDATE bursar.user_credits
    SET balance = balance + v_refund_amount,
        updated_at = now()
    WHERE user_id = v_tx.user_id
    RETURNING balance INTO v_new_balance;

    -- ── Bucket LIFO restoration ─────────────────────────────────────────────
    -- bucket_remaining[t] is derived fresh each time from
    -- original_breakdown[t] - sum(prior refunds' own breakdown[t]) — never a
    -- running counter — so repeated partial refunds compose correctly.
    v_orig_breakdown := COALESCE(v_tx.metadata->'bucket_breakdown', jsonb_build_object('default', v_original_debit));

    SELECT COALESCE(jsonb_object_agg(kv.bucket_key, kv.bucket_sum), '{}'::jsonb) INTO v_prior_refund_breakdown
    FROM (
        SELECT e.key AS bucket_key, SUM((e.value)::numeric) AS bucket_sum
        FROM bursar.credit_transactions ct
        CROSS JOIN LATERAL jsonb_each_text(COALESCE(ct.metadata->'bucket_breakdown', '{}'::jsonb)) AS e(key, value)
        WHERE ct.reference_id = p_transaction_id AND ct.type = 'refund'
        GROUP BY e.key
    ) kv;

    v_to_allocate := v_refund_amount;

    -- Walk buckets in REVERSE priority order (highest-priority-number / last
    -- drained bucket first). Buckets no longer present in credit_buckets (config
    -- drift) sort last, mirroring the deduct walk's "orphans appended last".
    FOR v_bucket_key, v_bucket_orig_amt IN
        SELECT e.key, (e.value)::numeric
        FROM jsonb_each_text(v_orig_breakdown) AS e(key, value)
        LEFT JOIN bursar.credit_buckets ct ON ct.bucket_key = e.key
        ORDER BY COALESCE(ct.priority, -2147483648) DESC, e.key DESC
    LOOP
        EXIT WHEN v_to_allocate <= 0;

        v_bucket_prior := COALESCE((v_prior_refund_breakdown->>v_bucket_key)::numeric, 0);
        v_bucket_remaining := GREATEST(v_bucket_orig_amt - v_bucket_prior, 0);
        v_give := LEAST(v_bucket_remaining, v_to_allocate);

        IF v_give > 0 THEN
            INSERT INTO bursar.user_credit_buckets (user_id, bucket_key, balance)
            VALUES (v_tx.user_id, v_bucket_key, v_give)
            ON CONFLICT (user_id, bucket_key) DO UPDATE SET
                balance = bursar.user_credit_buckets.balance + v_give,
                updated_at = now();

            v_new_breakdown := v_new_breakdown || jsonb_build_object(v_bucket_key, v_give);
            v_to_allocate := v_to_allocate - v_give;
        END IF;
    END LOOP;

    INSERT INTO bursar.credit_transactions (user_id, amount, type, reference_type, reference_id, metadata)
    VALUES (v_tx.user_id, v_refund_amount, 'refund', p_reason, p_transaction_id,
            p_metadata || jsonb_build_object('reason', p_reason, 'bucket_breakdown', v_new_breakdown))
    RETURNING id INTO v_refund_tx_id;

    RETURN jsonb_build_object(
        'refund_transaction_id', v_refund_tx_id,
        'user_id', v_tx.user_id,
        'amount', v_refund_amount,
        'new_balance', v_new_balance,
        'bucket_breakdown', v_new_breakdown
    );
END;
$$;


--
-- Name: release_lease(uuid, uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.release_lease(p_user_id uuid, p_lease_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT status INTO v_status FROM bursar.credit_reservations
    WHERE id = p_lease_id AND user_id = p_user_id FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('released', false, 'reason', 'not_found');
    END IF;
    IF v_status = 'settled' THEN
        RETURN jsonb_build_object('released', false, 'reason', 'already_settled');
    END IF;
    IF v_status = 'released' THEN
        RETURN jsonb_build_object('released', false, 'reason', 'already_released');
    END IF;

    UPDATE bursar.credit_reservations SET status = 'released' WHERE id = p_lease_id;
    RETURN jsonb_build_object('released', true, 'reason', 'released');
END;
$$;


--
-- Name: renew_lease(uuid, uuid, integer); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.renew_lease(p_user_id uuid, p_lease_id uuid, p_ttl_seconds integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_status      TEXT;
    v_amount      NUMERIC;
    v_billing     TEXT;
    v_expires_at  TIMESTAMPTZ;
    v_lease_exp   TIMESTAMPTZ;
    v_balance     NUMERIC;
    v_reserved    NUMERIC;
BEGIN
    SELECT status, amount, billing_mode, expires_at
    INTO v_status, v_amount, v_billing, v_lease_exp
    FROM bursar.credit_reservations
    WHERE id = p_lease_id AND user_id = p_user_id FOR UPDATE;

    IF NOT FOUND OR v_status IN ('released', 'settled') THEN
        RETURN jsonb_build_object('error', 'lease_not_found');
    END IF;
    IF v_status = 'expired' OR v_lease_exp <= now() THEN
        UPDATE bursar.credit_reservations SET status = 'expired' WHERE id = p_lease_id;
        RETURN jsonb_build_object('error', 'lease_expired');
    END IF;

    v_expires_at := now() + make_interval(secs => p_ttl_seconds);
    UPDATE bursar.credit_reservations SET expires_at = v_expires_at WHERE id = p_lease_id;

    SELECT balance INTO v_balance FROM bursar.user_credits WHERE user_id = p_user_id;
    SELECT COALESCE(SUM(amount), 0) INTO v_reserved
    FROM bursar.credit_reservations
    WHERE user_id = p_user_id AND status = 'active' AND expires_at > now();

    RETURN jsonb_build_object(
        'lease_id', p_lease_id, 'user_id', p_user_id, 'amount', v_amount,
        'available', COALESCE(v_balance, 0) - v_reserved, 'reserved', v_reserved,
        'billing_mode', v_billing, 'expires_at', v_expires_at
    );
END;
$$;


--
-- Name: resolve_billing_offer_by_lookup(text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.resolve_billing_offer_by_lookup(p_provider text, p_lookup_key text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_ref RECORD;
    v_offer RECORD;
BEGIN
    IF p_lookup_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_ref
    FROM bursar.billing_provider_refs
 WHERE provider = p_provider AND environment = bursar.current_provider_environment() AND lookup_key = p_lookup_key
      AND resource_type = 'offer' AND active = true
    LIMIT 1;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_offer
    FROM bursar.billing_offers
    WHERE offer_key = v_ref.resource_key
      AND status = 'active'
      AND (valid_from IS NULL OR valid_from <= now())
      AND (valid_to IS NULL OR valid_to > now());

    IF v_offer.offer_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'offer_key', v_offer.offer_key,
        'plan', v_offer.plan,
        'interval', v_offer.interval,
        'interval_count', v_offer.interval_count,
        'grant_mode', v_offer.grant_mode,
        'grant_credits', v_offer.grant_credits,
        'grant_bucket', v_offer.grant_bucket,
        'grant_replace_prior', v_offer.grant_replace_prior
    );
END;
$$;


--
-- Name: resolve_billing_offer_by_price(text, text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.resolve_billing_offer_by_price(p_provider text, p_price_id text DEFAULT NULL::text, p_product_id text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_ref RECORD;
    v_offer RECORD;
BEGIN
    IF p_price_id IS NULL AND p_product_id IS NULL THEN
        RETURN NULL;
    END IF;

    IF p_price_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM bursar.billing_provider_refs
 WHERE provider = p_provider AND environment = bursar.current_provider_environment() AND price_id = p_price_id
          AND resource_type = 'offer' AND active = true
        LIMIT 1;
    ELSIF p_product_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM bursar.billing_provider_refs
 WHERE provider = p_provider AND environment = bursar.current_provider_environment() AND product_id = p_product_id
          AND resource_type = 'offer' AND active = true
        LIMIT 1;
    END IF;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_offer
    FROM bursar.billing_offers
    WHERE offer_key = v_ref.resource_key
      AND status = 'active'
      AND (valid_from IS NULL OR valid_from <= now())
      AND (valid_to IS NULL OR valid_to > now());

    IF v_offer.offer_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'offer_key', v_offer.offer_key,
        'plan', v_offer.plan,
        'interval', v_offer.interval,
        'interval_count', v_offer.interval_count,
        'grant_mode', v_offer.grant_mode,
        'grant_credits', v_offer.grant_credits,
        'grant_bucket', v_offer.grant_bucket,
        'grant_replace_prior', v_offer.grant_replace_prior
    );
END;
$$;


--
-- Name: resolve_credit_topup_by_lookup(text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.resolve_credit_topup_by_lookup(p_provider text, p_lookup_key text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_ref RECORD;
    v_topup RECORD;
BEGIN
    IF p_lookup_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_ref
    FROM bursar.billing_provider_refs
 WHERE provider = p_provider AND environment = bursar.current_provider_environment() AND lookup_key = p_lookup_key
      AND resource_type = 'topup' AND active = true
    LIMIT 1;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_topup
    FROM bursar.billing_credit_topups
    WHERE topup_key = v_ref.resource_key
      AND status = 'active';

    IF v_topup.topup_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'topup_key', v_topup.topup_key,
        'deposit_to', v_topup.deposit_to,
        'credits_per_unit', v_topup.credits_per_unit,
        'min_amount_minor', v_topup.min_amount_minor,
        'max_amount_minor', v_topup.max_amount_minor,
        'tax_behavior', v_topup.tax_behavior
    );
END;
$$;


--
-- Name: resolve_credit_topup_by_price(text, text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.resolve_credit_topup_by_price(p_provider text, p_price_id text DEFAULT NULL::text, p_product_id text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_ref RECORD;
    v_topup RECORD;
BEGIN
    IF p_price_id IS NULL AND p_product_id IS NULL THEN
        RETURN NULL;
    END IF;

    IF p_price_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM bursar.billing_provider_refs
 WHERE provider = p_provider AND environment = bursar.current_provider_environment() AND price_id = p_price_id
          AND resource_type = 'topup' AND active = true
        LIMIT 1;
    ELSIF p_product_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM bursar.billing_provider_refs
 WHERE provider = p_provider AND environment = bursar.current_provider_environment() AND product_id = p_product_id
          AND resource_type = 'topup' AND active = true
        LIMIT 1;
    END IF;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_topup
    FROM bursar.billing_credit_topups
    WHERE topup_key = v_ref.resource_key
      AND status = 'active';

    IF v_topup.topup_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'topup_key', v_topup.topup_key,
        'deposit_to', v_topup.deposit_to,
        'credits_per_unit', v_topup.credits_per_unit,
        'min_amount_minor', v_topup.min_amount_minor,
        'max_amount_minor', v_topup.max_amount_minor,
        'tax_behavior', v_topup.tax_behavior
    );
END;
$$;


--
-- Name: revoke_credits_by_tx_type(uuid, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.revoke_credits_by_tx_type(p_user_id uuid, p_tx_type text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_total_granted NUMERIC;
    v_total_revoked NUMERIC;
    v_revocable NUMERIC;
    v_current_balance NUMERIC;
    v_remaining NUMERIC;
    v_to_deduct NUMERIC;
    v_bucket_row RECORD;
    v_first_bucket TEXT;
    v_bucket_breakdown JSONB := '{}'::jsonb;
    v_transaction_id UUID;
    v_new_balance NUMERIC;
BEGIN
    -- Total credits granted of the given type
    SELECT COALESCE(SUM(amount), 0) INTO v_total_granted
    FROM bursar.credit_transactions
    WHERE user_id = p_user_id
      AND type = p_tx_type::bursar.credit_tx_type
      AND amount > 0;

    -- Total already revoked for this tx_type
    SELECT COALESCE(SUM(ABS(amount)), 0) INTO v_total_revoked
    FROM bursar.credit_transactions
    WHERE user_id = p_user_id
      AND type = 'cycle_grant_revoke'::bursar.credit_tx_type
      AND metadata->>'revoked_tx_type' = p_tx_type;

    v_revocable := v_total_granted - v_total_revoked;

    -- Cap at the user's current balance (parity with MemoryStore).
    -- FOR UPDATE prevents concurrent revoke calls from over-deducting.
    SELECT COALESCE(balance, 0) INTO v_current_balance
    FROM bursar.user_credits
    WHERE user_id = p_user_id
    FOR UPDATE;

    v_revocable := LEAST(v_revocable, v_current_balance);

    IF v_revocable <= 0 THEN
        RETURN jsonb_build_object(
            'user_id', p_user_id,
            'amount', 0,
            'new_balance', v_current_balance
        );
    END IF;

    -- Priority-walk across buckets (parity with MemoryStore's _walk_tiers):
    -- drain configured buckets in ascending priority order, then any bucket keys
    -- the user holds a nonzero balance in that are no longer configured
    -- ("config drift" safety net).
    v_remaining := v_revocable;
    FOR v_bucket_row IN
        SELECT uct.bucket_key, uct.balance
        FROM bursar.user_credit_buckets uct
        LEFT JOIN bursar.credit_buckets ct ON ct.bucket_key = uct.bucket_key
        WHERE uct.user_id = p_user_id AND uct.balance > 0
        ORDER BY COALESCE(ct.priority, 999999) ASC, uct.bucket_key ASC
        FOR UPDATE OF uct
    LOOP
        v_to_deduct := LEAST(v_bucket_row.balance, v_remaining);
        UPDATE bursar.user_credit_buckets
        SET balance = balance - v_to_deduct, updated_at = now()
        WHERE user_id = p_user_id AND bucket_key = v_bucket_row.bucket_key;
        v_remaining := v_remaining - v_to_deduct;

        v_bucket_breakdown := v_bucket_breakdown || jsonb_build_object(
            v_bucket_row.bucket_key,
            COALESCE((v_bucket_breakdown->>v_bucket_row.bucket_key)::numeric, 0) + v_to_deduct
        );

        IF v_first_bucket IS NULL THEN
            v_first_bucket := v_bucket_row.bucket_key;
        END IF;

        EXIT WHEN v_remaining <= 0;
    END LOOP;

    -- If the user has no bucket rows (edge case), create one in the default bucket
    -- so the aggregate/per-tier invariant stays intact.
    IF v_first_bucket IS NULL THEN
        v_first_bucket := 'default';
        INSERT INTO bursar.user_credit_buckets (user_id, bucket_key, balance)
        VALUES (p_user_id, v_first_bucket, -v_revocable)
        ON CONFLICT (user_id, bucket_key) DO UPDATE SET
            balance = bursar.user_credit_buckets.balance - v_revocable,
            updated_at = now();
    END IF;

    -- Insert reversal transaction
    INSERT INTO bursar.credit_transactions (user_id, amount, type, metadata)
    VALUES (
        p_user_id,
        -v_revocable,
        'cycle_grant_revoke'::bursar.credit_tx_type,
        jsonb_build_object(
            'revoked_tx_type', p_tx_type,
            'revoked_amount', v_revocable,
            'bucket', v_first_bucket,
            'bucket_breakdown', v_bucket_breakdown
        )
    )
    RETURNING id INTO v_transaction_id;

    -- Deduct from aggregate balance
    UPDATE bursar.user_credits
    SET balance = balance - v_revocable, updated_at = now()
    WHERE user_id = p_user_id
    RETURNING balance INTO v_new_balance;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'amount', v_revocable,
        'new_balance', COALESCE(v_new_balance, 0),
        'transaction_id', v_transaction_id,
        'bucket', v_first_bucket
    );
END;
$$;


--
-- Name: set_active_bursar_config(jsonb, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.set_active_bursar_config(p_config jsonb, p_label text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_new_id UUID;
    v_next_version INTEGER;
BEGIN
    PERFORM bursar.validate_bursar_config(p_config);
    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM bursar.bursar_config;

    UPDATE bursar.bursar_config SET active = false WHERE active = true;

    INSERT INTO bursar.bursar_config (config, active, version, label)
    VALUES (p_config, true, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    PERFORM bursar.sync_plans_from_config(bursar.internal_catalog_config_from_public(p_config), v_next_version);
    PERFORM bursar.sync_buckets_from_config(bursar.internal_catalog_config_from_public(p_config), v_next_version);
    PERFORM bursar.sync_billing_from_config(bursar.internal_catalog_config_from_public(p_config)->'billing');

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true
    );
END;
$$;

CREATE OR REPLACE FUNCTION bursar.refund_credits(
  p_transaction_id uuid,
  p_amount numeric DEFAULT NULL,
  p_reason text DEFAULT NULL,
  p_metadata jsonb DEFAULT '{}'
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
  v_tx record;
  v_account bursar.credit_accounts;
  v_refund numeric(18,4);
  v_prior numeric(18,4);
  v_remaining numeric(18,4);
  v_new_balance numeric(18,4);
  v_refund_id uuid;
  v_bucket_key text;
  v_bucket_amount numeric(18,4);
  v_to_restore numeric(18,4);
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('refund:' || p_transaction_id::text, 0));
  SELECT ct.*, a.account_type, a.team_id, a.balance AS account_balance
  INTO v_tx
  FROM bursar.credit_transactions ct
  JOIN bursar.credit_accounts a ON a.id = ct.account_id
  WHERE ct.id = p_transaction_id
  FOR UPDATE;
  IF NOT FOUND OR v_tx.type NOT IN ('usage','team_usage') OR v_tx.amount >= 0 THEN
    RETURN jsonb_build_object('error','over_refund');
  END IF;
  IF p_amount IS NOT NULL AND NOT bursar.is_finite_numeric(p_amount) THEN
    RETURN jsonb_build_object('error','over_refund');
  END IF;
  v_refund := COALESCE(p_amount, abs(v_tx.amount));
  SELECT COALESCE(sum(amount),0) INTO v_prior
  FROM bursar.credit_transactions
  WHERE reference_id = p_transaction_id AND type = 'refund';
  v_remaining := abs(v_tx.amount) - v_prior;
  IF v_refund <= 0 OR v_refund > v_remaining THEN
    RETURN jsonb_build_object('error','over_refund');
  END IF;

  IF v_tx.account_type = 'personal' THEN
    UPDATE bursar.user_credits SET balance = balance + v_refund, updated_at = now()
    WHERE user_id = v_tx.user_id RETURNING balance INTO v_new_balance;
    v_to_restore := v_refund;
    FOR v_bucket_key, v_bucket_amount IN
      SELECT key, value::numeric FROM jsonb_each_text(
        COALESCE(v_tx.metadata->'bucket_breakdown', jsonb_build_object('default', abs(v_tx.amount)))
      )
    LOOP
      v_bucket_amount := least(v_bucket_amount, v_to_restore);
      EXIT WHEN v_to_restore <= 0;
      INSERT INTO bursar.user_credit_buckets (user_id, bucket_key, balance)
      VALUES (v_tx.user_id, v_bucket_key, least(v_bucket_amount, v_refund))
      ON CONFLICT (user_id, bucket_key) DO UPDATE
      SET balance = bursar.user_credit_buckets.balance + excluded.balance, updated_at = now();
      v_to_restore := v_to_restore - v_bucket_amount;
    END LOOP;
  ELSE
    UPDATE bursar.credit_teams SET balance = balance + v_refund, updated_at = now()
    WHERE id = v_tx.team_id RETURNING balance INTO v_new_balance;
  END IF;

  INSERT INTO bursar.credit_transactions
    (user_id, amount, type, reference_type, reference_id, metadata, account_id, acting_user_id)
  VALUES (v_tx.user_id, v_refund, 'refund', p_reason, p_transaction_id,
          COALESCE(p_metadata,'{}') || jsonb_build_object('reason',p_reason), v_tx.account_id, v_tx.acting_user_id)
  RETURNING id INTO v_refund_id;

  RETURN jsonb_build_object('refund_transaction_id', v_refund_id, 'user_id', v_tx.user_id,
                            'account_id', v_tx.account_id, 'account_type', v_tx.account_type,
                            'amount', v_refund, 'new_balance', v_new_balance);
END;
$$;


--


-- ============================================================================
-- Source: 100_plan_billing_functions.sql
-- ============================================================================

-- Name: set_user_plan(uuid, text, timestamp with time zone, integer, boolean); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.set_user_plan(p_user_id uuid, p_plan_key text, p_plan_assigned_at timestamp with time zone DEFAULT NULL::timestamp with time zone, p_config_version integer DEFAULT NULL::integer, p_allow_grandfathered boolean DEFAULT false) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_plan_id UUID;
    v_assigned_at TIMESTAMPTZ;
    v_catalog_version INTEGER;
    v_old_plan_id UUID;
    v_old_catalog_version INTEGER;
BEGIN
    IF p_config_version IS NOT NULL THEN
        SELECT id INTO v_plan_id
        FROM bursar.credit_plans
        WHERE plan_key = p_plan_key AND config_version = p_config_version;
    ELSE
        SELECT cp.id, cp.config_version INTO v_plan_id, v_catalog_version
        FROM bursar.credit_plans cp
        WHERE cp.plan_key = p_plan_key
          AND cp.config_version = (
              SELECT version FROM bursar.bursar_config WHERE active = true LIMIT 1
          )
          AND cp.status = 'active';

        IF v_plan_id IS NULL AND p_allow_grandfathered THEN
            SELECT id, config_version INTO v_plan_id, v_catalog_version
            FROM bursar.credit_plans
            WHERE plan_key = p_plan_key AND status = 'active'
            ORDER BY config_version DESC
            LIMIT 1;
        END IF;
    END IF;

    IF v_plan_id IS NULL THEN
        RETURN jsonb_build_object('error', 'plan_not_found');
    END IF;

    SELECT config_version INTO v_catalog_version
    FROM bursar.credit_plans WHERE id = v_plan_id;

    v_assigned_at := COALESCE(p_plan_assigned_at, now());

    SELECT plan_id, catalog_version
    INTO v_old_plan_id, v_old_catalog_version
    FROM bursar.user_credits
    WHERE user_id = p_user_id;

    INSERT INTO bursar.user_credits (user_id, plan_id, plan_assigned_at, catalog_version)
    VALUES (p_user_id, v_plan_id, v_assigned_at, v_catalog_version)
    ON CONFLICT (user_id) DO UPDATE SET
        plan_id = v_plan_id,
        plan_assigned_at = v_assigned_at,
        catalog_version = v_catalog_version,
        updated_at = now();

    IF v_old_plan_id IS DISTINCT FROM v_plan_id THEN
        INSERT INTO bursar.credit_plan_migrations (
            user_id, from_plan_id, to_plan_id, from_config_version, to_config_version, reason
        ) VALUES (
            p_user_id, v_old_plan_id, v_plan_id, v_old_catalog_version, v_catalog_version, 'set_user_plan'
        );
    END IF;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_assigned_at', v_assigned_at,
        'catalog_version', v_catalog_version
    );
END;
$$;


--
-- Name: settle_lease(uuid, uuid, numeric, text, numeric, text, jsonb, boolean, date, text, integer, text, date, date); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.settle_lease(p_user_id uuid, p_lease_id uuid, p_amount numeric, p_idempotency_key text DEFAULT NULL::text, p_min_balance numeric DEFAULT 0, p_model text DEFAULT NULL::text, p_metadata jsonb DEFAULT '{}'::jsonb, p_skip_allowance boolean DEFAULT false, p_period_start date DEFAULT NULL::date, p_feature text DEFAULT NULL::text, p_feature_max_calls integer DEFAULT NULL::integer, p_feature_action text DEFAULT NULL::text, p_feature_period_start date DEFAULT NULL::date, p_feature_period_end date DEFAULT NULL::date) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_balance        NUMERIC;
    v_plan_id        UUID;
    v_status         TEXT;
    v_settle_tx      UUID;
    v_lease_expires  TIMESTAMPTZ;
    v_billing_mode   TEXT;
    v_overdraft_floor NUMERIC;
    v_settle_floor   NUMERIC;
    v_max_debit      NUMERIC;
    v_allowance_amount NUMERIC;
    v_period_start   DATE;
    v_used           NUMERIC;
    v_consume        NUMERIC := 0;
    v_net            NUMERIC;
    v_cap            RECORD;
    v_cap_window     TIMESTAMPTZ;
    v_cap_spend      NUMERIC;
    v_cap_warning    TEXT := NULL;
    v_feature_count  INT;
    v_feature_limit_warning TEXT := NULL;
    v_new_balance    NUMERIC;
    v_tx_id          UUID;
    v_metadata       JSONB;
    v_existing_id    UUID;
    v_existing_amt   NUMERIC;
    v_existing_cons  NUMERIC;
    v_existing_bal_after NUMERIC;
    v_existing_bucket_bd JSONB;
    v_bucket_breakdown JSONB := '{}'::jsonb;
BEGIN
    IF NOT bursar.is_finite_numeric(p_amount) OR p_amount < 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    SELECT balance, plan_id INTO v_balance, v_plan_id
    FROM bursar.user_credits WHERE user_id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO bursar.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0) ON CONFLICT (user_id) DO NOTHING;
        SELECT balance, plan_id INTO v_balance, v_plan_id
        FROM bursar.user_credits WHERE user_id = p_user_id FOR UPDATE;
    END IF;

    -- Idempotency replay (user-scoped).
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0),
               COALESCE((metadata->>'balance_after')::numeric, v_balance),
               COALESCE(metadata->'bucket_breakdown', '{}'::jsonb)
        INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after, v_existing_bucket_bd
        FROM bursar.credit_transactions
        WHERE user_id = p_user_id AND type = 'usage' AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id, 'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons, 'balance_after', v_existing_bal_after,
                'idempotent', true, 'cap_warning', NULL, 'feature_limit_warning', NULL, 'bucket_breakdown', v_existing_bucket_bd
            );
        END IF;
    END IF;

    -- Lock + validate the lease state; also read billing policy columns.
    SELECT status, settle_tx_id, expires_at, billing_mode, overdraft_floor
    INTO v_status, v_settle_tx, v_lease_expires, v_billing_mode, v_overdraft_floor
    FROM bursar.credit_reservations
    WHERE id = p_lease_id AND user_id = p_user_id FOR UPDATE;

    IF NOT FOUND OR v_status = 'released' THEN
        RETURN jsonb_build_object('error', 'lease_not_found', 'balance_after', v_balance);
    END IF;
    IF v_status = 'settled' THEN
        IF v_settle_tx IS NOT NULL THEN
            SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0),
                   COALESCE((metadata->>'balance_after')::numeric, v_balance),
                   COALESCE(metadata->'bucket_breakdown', '{}'::jsonb)
            INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after, v_existing_bucket_bd
            FROM bursar.credit_transactions WHERE id = v_settle_tx;
            IF FOUND THEN
                RETURN jsonb_build_object(
                    'transaction_id', v_existing_id, 'amount', v_existing_amt,
                    'allowance_consumed', v_existing_cons, 'balance_after', v_existing_bal_after,
                    'idempotent', true, 'cap_warning', NULL, 'feature_limit_warning', NULL, 'bucket_breakdown', v_existing_bucket_bd
                );
            END IF;
        END IF;
        RETURN jsonb_build_object('amount', 0, 'balance_after', v_balance, 'idempotent', true, 'bucket_breakdown', '{}'::jsonb);
    END IF;
    IF v_status = 'expired' OR v_lease_expires <= now() THEN
        UPDATE bursar.credit_reservations SET status = 'expired' WHERE id = p_lease_id;
        RETURN jsonb_build_object('error', 'lease_expired', 'balance_after', v_balance);
    END IF;

    -- Zero-cost settle releases the lease without charging (and does not
    -- tag/count anything toward a feature limit — no work happened).
    IF p_amount = 0 THEN
        UPDATE bursar.credit_reservations SET status = 'settled' WHERE id = p_lease_id;
        RETURN jsonb_build_object('transaction_id', NULL, 'amount', 0, 'balance_after', v_balance, 'idempotent', false, 'bucket_breakdown', '{}'::jsonb);
    END IF;

    -- Allowance consume on the actual cost (mirrors deduct_with_allowance).
    -- Skipped when p_skip_allowance = TRUE: fixed-cost batch jobs reserved via
    -- the lease path must not deplete the free inference allowance.
    -- v_period_start: explicit p_period_start else the current UTC calendar
    -- month (unchanged).
    IF NOT p_skip_allowance AND v_plan_id IS NOT NULL THEN
        SELECT allowance_amount INTO v_allowance_amount FROM bursar.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM bursar.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_consume := LEAST(GREATEST(COALESCE(v_allowance_amount, 0) - COALESCE(v_used, 0), 0), p_amount);
    END IF;
    v_net := p_amount - v_consume;

    -- Settlement records the complete actual cost. Admission floors protect
    -- new work, but they must not silently erase overage after work completes.
    v_settle_floor := CASE WHEN v_billing_mode = 'overdraft'
                           THEN COALESCE(v_overdraft_floor, 0)
                           ELSE COALESCE(p_min_balance, 0)
                      END;

    -- Spend cap is ADVISORY at settle (never blocks): record the strongest breach.
    FOR v_cap IN
        SELECT action, cap_type, model, cap_limit FROM bursar.credit_spend_caps
        WHERE user_id = p_user_id AND (model IS NULL OR model = p_model)
        ORDER BY (action = 'deny') DESC, cap_limit ASC
    LOOP
        v_cap_window := CASE v_cap.cap_type
            WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
            ELSE date_trunc('month', now() AT TIME ZONE 'UTC')
        END;
        SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_cap_spend
        FROM bursar.credit_transactions ct
        WHERE ct.user_id = p_user_id AND ct.type IN ('usage', 'team_usage') AND ct.amount < 0
          AND ct.created_at >= v_cap_window
          AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);
        IF v_cap_spend + v_net > v_cap.cap_limit AND (v_cap_warning IS NULL OR (v_cap_warning <> 'deny' AND v_cap.action = 'deny')) THEN
            v_cap_warning := v_cap.action;
        END IF;
    END LOOP;

    -- Feature limit is ADVISORY at settle (never blocks — the work already
    -- happened): a breach only sets v_feature_limit_warning, using the
    -- configured action even when it is 'deny' (this is the "prefer deny"
    -- signal — it means the call would have been denied had it gone through
    -- deduct/create_lease). Skipped when no feature/limit was resolved.
    IF p_feature IS NOT NULL AND p_feature_max_calls IS NOT NULL THEN
        -- Deliberately no `amount < 0` filter — see deduct_with_allowance's 4b
        -- block for why (zero-net calls still count as invocations).
        SELECT COUNT(*) INTO v_feature_count
        FROM bursar.credit_transactions ct
        WHERE ct.user_id = p_user_id
          AND ct.type = 'usage'
          AND ct.metadata->>'feature' = p_feature
          AND ct.created_at >= (p_feature_period_start::timestamp AT TIME ZONE 'UTC')
          AND ct.created_at < (p_feature_period_end::timestamp AT TIME ZONE 'UTC');
        IF v_feature_count >= p_feature_max_calls THEN
            v_feature_limit_warning := p_feature_action;
        END IF;
    END IF;

    IF v_consume > 0 THEN
        INSERT INTO bursar.credit_usage_window (user_id, plan_id, billing_period, usage)
        VALUES (p_user_id, v_plan_id, v_period_start, v_consume)
        ON CONFLICT (user_id, plan_id, billing_period)
        DO UPDATE SET usage = bursar.credit_usage_window.usage + v_consume, updated_at = now();
    END IF;

    BEGIN
        -- ── Bucket walk (delegated to shared helper) ─────────────────────
        SELECT (result->>'bucket_breakdown')::jsonb INTO v_bucket_breakdown
        FROM bursar._walk_and_debit_buckets(p_user_id, v_net) AS result;

        v_metadata := COALESCE(p_metadata, '{}'::jsonb)
            || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model, 'feature', p_feature))
            || jsonb_build_object('allowance_consumed', v_consume, 'requested_amount', p_amount, 'charged_amount', v_net, 'balance_after', v_balance - v_net, 'floor_breached', v_balance - v_net < v_settle_floor, 'bucket_breakdown', v_bucket_breakdown);

        UPDATE bursar.user_credits SET balance = balance - v_net, updated_at = now()
        WHERE user_id = p_user_id RETURNING balance INTO v_new_balance;

        INSERT INTO bursar.credit_transactions (user_id, amount, type, reference_type, metadata)
        VALUES (p_user_id, -v_net, 'usage', p_metadata->>'reference_type', v_metadata) RETURNING id INTO v_tx_id;

        UPDATE bursar.credit_reservations SET status = 'settled', settle_tx_id = v_tx_id WHERE id = p_lease_id;

    EXCEPTION
        WHEN unique_violation THEN
            SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0),
                   COALESCE((metadata->>'balance_after')::numeric, v_balance),
                   COALESCE(metadata->'bucket_breakdown', '{}'::jsonb)
            INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after, v_existing_bucket_bd
            FROM bursar.credit_transactions
            WHERE user_id = p_user_id
              AND type = 'usage'
              AND metadata->>'idempotency_key' = p_idempotency_key
            LIMIT 1;
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id, 'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons, 'balance_after', v_existing_bal_after,
                'idempotent', true, 'cap_warning', NULL, 'feature_limit_warning', NULL,
                'bucket_breakdown', v_existing_bucket_bd
            );
    END;

    RETURN jsonb_build_object(
        'transaction_id', v_tx_id, 'amount', v_net, 'allowance_consumed', v_consume,
        'balance_after', v_new_balance, 'idempotent', false, 'cap_warning', v_cap_warning,
        'feature_limit_warning', v_feature_limit_warning,
        'bucket_breakdown', v_bucket_breakdown,
        'requested_amount', p_amount,
        'charged_amount', v_net,
        'floor_breached', v_new_balance < v_settle_floor
    );
END;
$$;


--
-- Name: snapshot_catalog_objects(integer, jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.snapshot_catalog_objects(p_version integer, p_config jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_plans jsonb := CASE WHEN jsonb_typeof(p_config->'plans') = 'object' THEN p_config->'plans' ELSE '{}'::jsonb END;
  v_buckets jsonb := CASE WHEN jsonb_typeof(p_config #> '{ledger,buckets}') = 'object' THEN p_config #> '{ledger,buckets}' ELSE '{}'::jsonb END;
  v_offers jsonb := CASE WHEN jsonb_typeof(p_config #> '{billing,subscriptions}') = 'object' THEN p_config #> '{billing,subscriptions}' ELSE '{}'::jsonb END;
  v_topups jsonb := CASE WHEN jsonb_typeof(p_config #> '{billing,topups}') = 'object' THEN p_config #> '{billing,topups}' ELSE '{}'::jsonb END;
  v_key text;
  v_value jsonb;
  v_provider text;
  v_ref jsonb;
BEGIN
  INSERT INTO bursar.catalog_object_versions (config_version, object_type, object_key, definition)
  SELECT p_version, 'plan', key, value FROM jsonb_each(v_plans)
  ON CONFLICT DO NOTHING;
  INSERT INTO bursar.catalog_object_versions (config_version, object_type, object_key, definition)
  SELECT p_version, 'bucket', key, value FROM jsonb_each(v_buckets)
  ON CONFLICT DO NOTHING;
  INSERT INTO bursar.catalog_object_versions (config_version, object_type, object_key, definition)
  SELECT p_version, 'offer', key, value FROM jsonb_each(v_offers)
  ON CONFLICT DO NOTHING;
  INSERT INTO bursar.catalog_object_versions (config_version, object_type, object_key, definition)
  SELECT p_version, 'topup', key, value FROM jsonb_each(v_topups)
  ON CONFLICT DO NOTHING;

  FOR v_key, v_value IN SELECT * FROM jsonb_each(v_offers)
  LOOP
    FOR v_provider, v_ref IN
      SELECT * FROM jsonb_each(CASE WHEN jsonb_typeof(v_value->'providers') = 'object' THEN v_value->'providers' ELSE '{}'::jsonb END)
    LOOP
      INSERT INTO bursar.catalog_object_versions (config_version, object_type, object_key, definition)
      VALUES (p_version, 'provider_ref', 'offer:' || v_key || ':' || v_provider, v_ref)
      ON CONFLICT DO NOTHING;
    END LOOP;
  END LOOP;
  FOR v_key, v_value IN SELECT * FROM jsonb_each(v_topups)
  LOOP
    FOR v_provider, v_ref IN
      SELECT * FROM jsonb_each(CASE WHEN jsonb_typeof(v_value->'providers') = 'object' THEN v_value->'providers' ELSE '{}'::jsonb END)
    LOOP
      INSERT INTO bursar.catalog_object_versions (config_version, object_type, object_key, definition)
      VALUES (p_version, 'provider_ref', 'topup:' || v_key || ':' || v_provider, v_ref)
      ON CONFLICT DO NOTHING;
    END LOOP;
  END LOOP;
END;
$$;


--
-- Name: spend_by_model(timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.spend_by_model(p_start timestamp with time zone, p_end timestamp with time zone) RETURNS TABLE(model text, total_spend numeric, transaction_count bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
    RETURN QUERY
    SELECT
        COALESCE(ct.metadata->>'model', 'unknown')::TEXT AS model,
        COALESCE(SUM(ABS(ct.amount)), 0)::NUMERIC AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM bursar.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at < p_end
    GROUP BY ct.metadata->>'model'
    ORDER BY total_spend DESC;
END;
$$;


--
-- Name: spend_by_user(timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.spend_by_user(p_start timestamp with time zone, p_end timestamp with time zone) RETURNS TABLE(user_id text, total_spend numeric, transaction_count bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
    RETURN QUERY
    SELECT
        ct.user_id::TEXT,
        COALESCE(SUM(ABS(ct.amount)), 0)::NUMERIC AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM bursar.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at < p_end
    GROUP BY ct.user_id
    ORDER BY total_spend DESC;
END;
$$;


--
-- Name: sync_billing_from_config(jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.sync_billing_from_config(p_config jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_key TEXT;
    v_item JSONB;
    v_ref JSONB;
    v_provider TEXT;
    v_config_keys TEXT[];
BEGIN
    IF p_config ? 'subscriptions' AND jsonb_typeof(p_config->'subscriptions') = 'object' THEN
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(COALESCE(p_config->'subscriptions', '{}'::jsonb)) k;
        v_config_keys := COALESCE(v_config_keys, ARRAY[]::text[]);

        IF v_config_keys IS NOT NULL THEN
            UPDATE bursar.billing_offers SET status = 'archived', updated_at = now()
            WHERE offer_key != ALL(v_config_keys) AND status = 'active';
        END IF;

        UPDATE bursar.billing_provider_refs SET active = false, updated_at = now()
        WHERE resource_type = 'offer'
          AND environment = bursar.current_provider_environment();

        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'subscriptions')
        LOOP
            INSERT INTO bursar.billing_offers (
                offer_key, plan, interval, interval_count,
                grant_mode, grant_credits, grant_bucket, grant_replace_prior,
                valid_from, valid_to
            )
            VALUES (
                v_key,
                v_item->>'plan',
                COALESCE(v_item->>'interval', 'month'),
                COALESCE((v_item->>'interval_count')::INTEGER, 1),
                COALESCE(v_item#>>'{grant,mode}', 'allowance'),
                (v_item#>>'{grant,credits}')::NUMERIC,
                v_item#>>'{grant,bucket}',
                COALESCE((v_item#>>'{grant,replace_prior}')::BOOLEAN, true),
                (v_item->>'valid_from')::TIMESTAMPTZ,
                (v_item->>'valid_to')::TIMESTAMPTZ
            )
            ON CONFLICT (offer_key) DO UPDATE SET
                plan = EXCLUDED.plan,
                interval = EXCLUDED.interval,
                interval_count = EXCLUDED.interval_count,
                grant_mode = EXCLUDED.grant_mode,
                grant_credits = EXCLUDED.grant_credits,
                grant_bucket = EXCLUDED.grant_bucket,
                grant_replace_prior = EXCLUDED.grant_replace_prior,
                valid_from = EXCLUDED.valid_from,
                valid_to = EXCLUDED.valid_to,
                status = 'active',
                updated_at = now();

            IF v_item ? 'providers' AND jsonb_typeof(v_item->'providers') = 'object' THEN
                FOR v_provider, v_ref IN SELECT * FROM jsonb_each(v_item->'providers')
                LOOP
                    PERFORM bursar._upsert_billing_provider_ref(
                        'offer', v_provider,
                        v_ref->>'price_id', v_ref->>'product_id',
                        v_ref->>'variant_id', v_ref->>'lookup_key',
                        v_key
                    );
                END LOOP;
            END IF;
        END LOOP;
    END IF;

    IF p_config ? 'topups' AND jsonb_typeof(p_config->'topups') = 'object' THEN
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(COALESCE(p_config->'topups', '{}'::jsonb)) k;
        v_config_keys := COALESCE(v_config_keys, ARRAY[]::text[]);

        IF v_config_keys IS NOT NULL THEN
            UPDATE bursar.billing_credit_topups SET status = 'archived', updated_at = now()
            WHERE topup_key != ALL(v_config_keys) AND status = 'active';
        END IF;

        UPDATE bursar.billing_provider_refs SET active = false, updated_at = now()
        WHERE resource_type = 'topup'
          AND environment = bursar.current_provider_environment();

        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'topups')
        LOOP
            INSERT INTO bursar.billing_credit_topups (
                topup_key, deposit_to, credits_per_unit,
                min_amount_minor, max_amount_minor, tax_behavior
            )
            VALUES (
                v_key,
                v_item->>'deposit_to',
                COALESCE((v_item->>'credits_per_unit')::NUMERIC, 1000),
                COALESCE((v_item->>'min_amount_minor')::INTEGER, 500),
                COALESCE((v_item->>'max_amount_minor')::INTEGER, 500000),
                COALESCE(v_item->>'tax_behavior', 'exclude_tax')
            )
            ON CONFLICT (topup_key) DO UPDATE SET
                deposit_to = EXCLUDED.deposit_to,
                credits_per_unit = EXCLUDED.credits_per_unit,
                min_amount_minor = EXCLUDED.min_amount_minor,
                max_amount_minor = EXCLUDED.max_amount_minor,
                tax_behavior = EXCLUDED.tax_behavior,
                status = 'active',
                updated_at = now();

            IF v_item ? 'providers' AND jsonb_typeof(v_item->'providers') = 'object' THEN
                FOR v_provider, v_ref IN SELECT * FROM jsonb_each(v_item->'providers')
                LOOP
                    PERFORM bursar._upsert_billing_provider_ref(
                        'topup', v_provider,
                        v_ref->>'price_id', v_ref->>'product_id',
                        v_ref->>'variant_id', v_ref->>'lookup_key',
                        v_key
                    );
                END LOOP;
            END IF;
        END LOOP;
    END IF;
END;
$$;


--
-- Name: sync_buckets_from_config(jsonb, integer); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.sync_buckets_from_config(p_config jsonb, p_config_version integer DEFAULT NULL::integer) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_bucket_key TEXT;
    v_bucket_def JSONB;
    v_config_keys TEXT[];
    v_version INTEGER;
BEGIN
    v_version := COALESCE(
        p_config_version,
        (SELECT version FROM bursar.bursar_config WHERE active = true LIMIT 1),
        1
    );

    IF p_config #>> '{ledger,buckets}' IS NOT NULL
       AND jsonb_typeof(p_config #> '{ledger,buckets}') = 'object' THEN
        UPDATE bursar.credit_buckets
        SET is_default = false, allow_overdraft = false, updated_at = now()
        WHERE status = 'active';
        SELECT array_agg(k) INTO v_config_keys
        FROM jsonb_object_keys(COALESCE(p_config #> '{ledger,buckets}', '{}'::jsonb)) k;
        v_config_keys := COALESCE(v_config_keys, ARRAY[]::text[]);

        IF v_config_keys IS NOT NULL THEN
            UPDATE bursar.credit_buckets
            SET status = 'retired', updated_at = now()
            WHERE bucket_key != ALL(v_config_keys)
              AND status = 'active';
        END IF;

        FOR v_bucket_key, v_bucket_def IN
            SELECT * FROM jsonb_each(p_config #> '{ledger,buckets}')
        LOOP
            INSERT INTO bursar.credit_buckets (
                bucket_key, label, priority, expires, ttl_days,
                allow_overdraft, is_default, config_version, status
            )
            VALUES (
                v_bucket_key,
                COALESCE(v_bucket_def->>'label', v_bucket_key),
                COALESCE((v_bucket_def->>'priority')::INTEGER, 0),
                COALESCE(
                    (v_bucket_def->>'expires')::BOOLEAN,
                    (v_bucket_def->>'ttl_days')::INTEGER IS NOT NULL
                ),
                (v_bucket_def->>'ttl_days')::INTEGER,
                COALESCE((v_bucket_def->>'allow_overdraft')::BOOLEAN, false),
                COALESCE((v_bucket_def->>'default')::BOOLEAN, false),
                v_version,
                'active'
            )
            ON CONFLICT (bucket_key) DO UPDATE SET
                label = EXCLUDED.label,
                priority = EXCLUDED.priority,
                expires = EXCLUDED.expires,
                ttl_days = EXCLUDED.ttl_days,
                allow_overdraft = EXCLUDED.allow_overdraft,
                is_default = EXCLUDED.is_default,
                config_version = EXCLUDED.config_version,
                status = 'active',
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;


--
-- Name: sync_plans_from_config(jsonb, integer); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.sync_plans_from_config(p_config jsonb, p_config_version integer) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_plan_key TEXT;
    v_plan_def JSONB;
    v_config_keys TEXT[];
BEGIN
    IF p_config ? 'plans' AND jsonb_typeof(p_config->'plans') = 'object' THEN
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(COALESCE(p_config->'plans', '{}'::jsonb)) k;
        v_config_keys := COALESCE(v_config_keys, ARRAY[]::text[]);

        IF v_config_keys IS NOT NULL THEN
            UPDATE bursar.credit_plans
            SET status = 'retired', updated_at = now()
            WHERE config_version = p_config_version
              AND plan_key IS NOT NULL
              AND plan_key != ALL(v_config_keys)
              AND status = 'active';
        END IF;

        FOR v_plan_key, v_plan_def IN SELECT * FROM jsonb_each(p_config->'plans')
        LOOP
            INSERT INTO bursar.credit_plans (
                plan_key, config_version, label, allowance_amount, rate_overrides,
                entitlements, billing_mode, per_operation, max_concurrent,
                overdraft_floor, allowance_period, status
            )
            VALUES (
                v_plan_key,
                p_config_version,
                v_plan_def->>'label',
                COALESCE((v_plan_def #>> '{allowance,amount}')::NUMERIC, 0),
                COALESCE(v_plan_def->'rate_overrides', '{}'::jsonb),
                COALESCE(v_plan_def->'entitlements', '{}'::jsonb),
                COALESCE(v_plan_def #>> '{safety,billing_mode}', 'strict'),
                v_plan_def #> '{safety,per_operation}',
                (v_plan_def #>> '{safety,max_concurrent}')::INTEGER,
                (v_plan_def #>> '{safety,overdraft_floor}')::NUMERIC,
                COALESCE(v_plan_def #>> '{allowance,period}', 'calendar_month'),
                'active'
            )
            ON CONFLICT (plan_key, config_version) WHERE plan_key IS NOT NULL
            DO UPDATE SET
                label = EXCLUDED.label,
                allowance_amount = EXCLUDED.allowance_amount,
                rate_overrides = EXCLUDED.rate_overrides,
                entitlements = EXCLUDED.entitlements,
                billing_mode = EXCLUDED.billing_mode,
                per_operation = EXCLUDED.per_operation,
                max_concurrent = EXCLUDED.max_concurrent,
                overdraft_floor = EXCLUDED.overdraft_floor,
                allowance_period = EXCLUDED.allowance_period,
                status = 'active',
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;


--
-- Name: top_users(integer, timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.top_users(p_limit integer, p_start timestamp with time zone, p_end timestamp with time zone) RETURNS TABLE(user_id text, total_spend numeric)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
    RETURN QUERY
    SELECT
        ct.user_id::TEXT,
        COALESCE(SUM(ABS(ct.amount)), 0)::NUMERIC AS total_spend
    FROM bursar.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at < p_end
    GROUP BY ct.user_id
    ORDER BY total_spend DESC
    LIMIT p_limit;
END;
$$;


--
-- Name: unset_user_plan(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.unset_user_plan(p_user_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
BEGIN
    UPDATE bursar.user_credits
    SET plan_id = NULL,
        plan_assigned_at = NULL,
        updated_at = now()
    WHERE user_id = p_user_id;

    RETURN jsonb_build_object('user_id', p_user_id);
END;
$$;


--
-- Name: upsert_billing_customer(text, text, uuid, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.upsert_billing_customer(p_provider text, p_provider_customer_id text, p_user_id uuid, p_email text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_existing_user UUID;
BEGIN
    SELECT user_id INTO v_existing_user
    FROM bursar.billing_customers
 WHERE provider = p_provider AND provider_customer_id = p_provider_customer_id FOR UPDATE;

    IF v_existing_user IS NOT NULL AND v_existing_user <> p_user_id THEN
        RETURN jsonb_build_object(
            'error', 'user_id_mismatch',
            'message', 'provider customer already mapped to a different user'
        );
    END IF;

    INSERT INTO bursar.billing_customers (provider, provider_customer_id, user_id, email)
    VALUES (p_provider, p_provider_customer_id, p_user_id, p_email)
 ON CONFLICT (provider, provider_customer_id) DO UPDATE SET
 user_id = COALESCE(billing_customers.user_id, EXCLUDED.user_id),
 email = COALESCE(EXCLUDED.email, billing_customers.email),
 updated_at = now()
 WHERE billing_customers.user_id IS NULL OR billing_customers.user_id = EXCLUDED.user_id;

    RETURN jsonb_build_object('status', 'ok');
END;
$$;


--
-- Name: upsert_billing_dispute(text, text, text, uuid, text, text, jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.upsert_billing_dispute(p_provider text, p_provider_dispute_id text, p_provider_payment_id text, p_user_id uuid, p_status text, p_reason text, p_metadata jsonb DEFAULT NULL::jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO bursar.billing_disputes (
        provider, provider_dispute_id, provider_payment_id, user_id,
        status, reason, metadata
    )
    VALUES (
        p_provider, p_provider_dispute_id, p_provider_payment_id, p_user_id,
        p_status, p_reason,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (provider, provider_dispute_id) DO UPDATE SET
        user_id = COALESCE(billing_disputes.user_id, EXCLUDED.user_id),
        provider_payment_id = EXCLUDED.provider_payment_id,
        status = EXCLUDED.status,
        reason = EXCLUDED.reason,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('id', v_id, 'provider_dispute_id', p_provider_dispute_id);
END;
$$;


--
-- Name: upsert_billing_invoice(text, text, text, uuid, text, integer, integer, text, timestamp with time zone, timestamp with time zone, jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.upsert_billing_invoice(p_provider text, p_provider_invoice_id text, p_provider_subscription_id text, p_user_id uuid, p_status text, p_amount_paid_minor bigint, p_amount_due_minor bigint, p_currency text, p_period_start timestamp with time zone, p_period_end timestamp with time zone, p_metadata jsonb DEFAULT NULL::jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO bursar.billing_invoices (
        provider, provider_invoice_id, provider_subscription_id, user_id,
        status, amount_paid_minor, amount_due_minor, currency,
        period_start, period_end, metadata
    )
    VALUES (
        p_provider, p_provider_invoice_id, p_provider_subscription_id, p_user_id,
        p_status, p_amount_paid_minor, p_amount_due_minor, p_currency,
        p_period_start, p_period_end,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (provider, provider_invoice_id) DO UPDATE SET
        user_id = COALESCE(billing_invoices.user_id, EXCLUDED.user_id),
        provider_subscription_id = EXCLUDED.provider_subscription_id,
        status = EXCLUDED.status,
        amount_paid_minor = EXCLUDED.amount_paid_minor,
        amount_due_minor = EXCLUDED.amount_due_minor,
        currency = EXCLUDED.currency,
        period_start = EXCLUDED.period_start,
        period_end = EXCLUDED.period_end,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('id', v_id, 'provider_invoice_id', p_provider_invoice_id);
END;
$$;


--
-- Name: upsert_billing_payment(text, text, text, uuid, integer, integer, text, text, jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.upsert_billing_payment(p_provider text, p_provider_payment_id text, p_provider_invoice_id text, p_user_id uuid, p_amount_minor bigint, p_tax_minor bigint, p_currency text, p_purpose text, p_metadata jsonb DEFAULT NULL::jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO bursar.billing_payments (
        provider, provider_payment_id, provider_invoice_id, user_id,
        amount_minor, tax_minor, currency, purpose, metadata
    )
    VALUES (
        p_provider, p_provider_payment_id, p_provider_invoice_id, p_user_id,
        p_amount_minor, p_tax_minor, p_currency, p_purpose,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (provider, provider_payment_id) DO UPDATE SET
        user_id = COALESCE(billing_payments.user_id, EXCLUDED.user_id),
        provider_invoice_id = EXCLUDED.provider_invoice_id,
        amount_minor = EXCLUDED.amount_minor,
        tax_minor = EXCLUDED.tax_minor,
        currency = EXCLUDED.currency,
        purpose = EXCLUDED.purpose,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('id', v_id, 'provider_payment_id', p_provider_payment_id);
END;
$$;


--
-- Name: upsert_billing_preferences(uuid, boolean, boolean, boolean, boolean, boolean, boolean); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.upsert_billing_preferences(p_user_id uuid, p_auto_recharge boolean DEFAULT false, p_overage_protection boolean DEFAULT true, p_email_notifications boolean DEFAULT true, p_usage_alerts boolean DEFAULT true, p_invoice_reminders boolean DEFAULT false, p_usage_limit_alerts boolean DEFAULT true) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
    INSERT INTO bursar.billing_preferences (
        user_id, auto_recharge, overage_protection,
        email_notifications, usage_alerts, invoice_reminders, usage_limit_alerts
    )
    VALUES (
        p_user_id, p_auto_recharge, p_overage_protection,
        p_email_notifications, p_usage_alerts, p_invoice_reminders, p_usage_limit_alerts
    )
    ON CONFLICT (user_id) DO UPDATE SET
        auto_recharge       = COALESCE(p_auto_recharge, billing_preferences.auto_recharge),
        overage_protection  = COALESCE(p_overage_protection, billing_preferences.overage_protection),
        email_notifications = COALESCE(p_email_notifications, billing_preferences.email_notifications),
        usage_alerts        = COALESCE(p_usage_alerts, billing_preferences.usage_alerts),
        invoice_reminders   = COALESCE(p_invoice_reminders, billing_preferences.invoice_reminders),
        usage_limit_alerts  = COALESCE(p_usage_limit_alerts, billing_preferences.usage_limit_alerts),
        updated_at          = now();

    RETURN jsonb_build_object('status', 'ok');
END;
$$;


--
-- Name: upsert_billing_refund(text, text, text, uuid, integer, text, text, jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.upsert_billing_refund(p_provider text, p_provider_refund_id text, p_provider_payment_id text, p_user_id uuid, p_amount_minor bigint, p_currency text, p_reason text, p_metadata jsonb DEFAULT NULL::jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO bursar.billing_refunds (
        provider, provider_refund_id, provider_payment_id, user_id,
        amount_minor, currency, reason, metadata
    )
    VALUES (
        p_provider, p_provider_refund_id, p_provider_payment_id, p_user_id,
        p_amount_minor, p_currency, p_reason,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (provider, provider_refund_id) DO UPDATE SET
        user_id = COALESCE(billing_refunds.user_id, EXCLUDED.user_id),
        provider_payment_id = EXCLUDED.provider_payment_id,
        amount_minor = EXCLUDED.amount_minor,
        currency = EXCLUDED.currency,
        reason = EXCLUDED.reason,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('id', v_id, 'provider_refund_id', p_provider_refund_id);
END;
$$;


--
-- Name: upsert_billing_subscription(jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.upsert_billing_subscription(p_state jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_existing_user UUID;
    v_catalog_version INTEGER;
    v_plan_version_id UUID;
BEGIN
    SELECT user_id INTO v_existing_user
    FROM bursar.billing_subscriptions
    WHERE provider = p_state->>'provider'
 AND provider_subscription_id = p_state->>'provider_subscription_id' FOR UPDATE;

    IF v_existing_user IS NOT NULL
       AND v_existing_user <> (p_state->>'user_id')::UUID THEN
        RETURN jsonb_build_object(
            'error', 'user_id_mismatch',
            'message', 'provider subscription already mapped to a different user'
        );
    END IF;

    v_catalog_version := COALESCE(
        (p_state->>'catalog_version')::INTEGER,
        (SELECT version FROM bursar.bursar_config WHERE active = true LIMIT 1)
    );

    IF p_state->>'plan' IS NOT NULL AND v_catalog_version IS NOT NULL THEN
        SELECT id INTO v_plan_version_id
        FROM bursar.credit_plans
        WHERE plan_key = p_state->>'plan' AND config_version = v_catalog_version
        LIMIT 1;
    END IF;

    INSERT INTO bursar.billing_subscriptions (
        user_id, provider, provider_subscription_id, provider_customer_id,
        offer_key, plan, status, current_period_start,
        current_period_end, cancel_at_period_end, interval, interval_count,
        metadata, catalog_version, plan_version_id
    )
    VALUES (
        (p_state->>'user_id')::UUID,
        p_state->>'provider',
        p_state->>'provider_subscription_id',
        p_state->>'provider_customer_id',
        p_state->>'offer_key',
        p_state->>'plan',
        COALESCE(p_state->>'status', 'incomplete'),
        (p_state->>'current_period_start')::TIMESTAMPTZ,
        (p_state->>'current_period_end')::TIMESTAMPTZ,
        COALESCE((p_state->>'cancel_at_period_end')::BOOLEAN, false),
        p_state->>'interval',
        (p_state->>'interval_count')::INTEGER,
        COALESCE((p_state->>'metadata')::JSONB, '{}'::jsonb),
        v_catalog_version,
        v_plan_version_id
    )
 ON CONFLICT (provider, provider_subscription_id) DO UPDATE SET
 user_id = COALESCE(billing_subscriptions.user_id, EXCLUDED.user_id),
        provider_customer_id = COALESCE(EXCLUDED.provider_customer_id, billing_subscriptions.provider_customer_id),
        offer_key = COALESCE(EXCLUDED.offer_key, billing_subscriptions.offer_key),
        plan = COALESCE(EXCLUDED.plan, billing_subscriptions.plan),
        status = EXCLUDED.status,
        current_period_start = COALESCE(EXCLUDED.current_period_start, billing_subscriptions.current_period_start),
        current_period_end = COALESCE(EXCLUDED.current_period_end, billing_subscriptions.current_period_end),
        cancel_at_period_end = EXCLUDED.cancel_at_period_end,
        interval = COALESCE(EXCLUDED.interval, billing_subscriptions.interval),
        interval_count = COALESCE(EXCLUDED.interval_count, billing_subscriptions.interval_count),
        metadata = CASE WHEN (p_state->>'metadata') IS NOT NULL THEN (p_state->>'metadata')::JSONB ELSE billing_subscriptions.metadata END,
        catalog_version = COALESCE(EXCLUDED.catalog_version, billing_subscriptions.catalog_version),
        plan_version_id = COALESCE(EXCLUDED.plan_version_id, billing_subscriptions.plan_version_id),
 updated_at = now()
 WHERE billing_subscriptions.user_id IS NULL OR billing_subscriptions.user_id = EXCLUDED.user_id;

    RETURN jsonb_build_object('status', 'ok');
END;
$$;


--
-- Name: validate_bursar_config(jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.validate_bursar_config(p_config jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_key text;
    v_value jsonb;
    v_mode text;
    v_amount numeric;
    v_ttl integer;
BEGIN
    IF jsonb_typeof(p_config) <> 'object' THEN
        RAISE EXCEPTION 'catalog config must be a JSON object' USING ERRCODE = '22023';
    END IF;

    IF p_config ? 'plans' AND jsonb_typeof(p_config->'plans') <> 'object' THEN
        RAISE EXCEPTION 'catalog plans must be an object' USING ERRCODE = '22023';
    END IF;

    FOR v_key, v_value IN SELECT * FROM jsonb_each(COALESCE(p_config->'plans', '{}'::jsonb))
    LOOP
        IF v_key = '' OR jsonb_typeof(v_value) <> 'object' THEN
            RAISE EXCEPTION 'each catalog plan needs a non-empty key and object value' USING ERRCODE = '22023';
        END IF;
        v_mode := COALESCE(v_value #>> '{safety,billing_mode}', 'strict');
        IF v_mode NOT IN ('strict', 'overdraft') THEN
            RAISE EXCEPTION 'plan % has invalid billing mode %', v_key, v_mode USING ERRCODE = '22023';
        END IF;
        BEGIN
            v_amount := COALESCE((v_value #>> '{allowance,amount}')::numeric, 0);
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'plan % allowance amount must be numeric', v_key USING ERRCODE = '22023';
        END;
        IF v_amount < 0 THEN
            RAISE EXCEPTION 'plan % allowance amount must not be negative', v_key USING ERRCODE = '22023';
        END IF;
    END LOOP;

    IF p_config #> '{ledger,buckets}' IS NOT NULL
       AND jsonb_typeof(p_config #> '{ledger,buckets}') <> 'object' THEN
        RAISE EXCEPTION 'catalog ledger.buckets must be an object' USING ERRCODE = '22023';
    END IF;

    FOR v_key, v_value IN SELECT * FROM jsonb_each(COALESCE(p_config #> '{ledger,buckets}', '{}'::jsonb))
    LOOP
        IF v_key = '' OR jsonb_typeof(v_value) <> 'object' THEN
            RAISE EXCEPTION 'each catalog bucket needs a non-empty key and object value' USING ERRCODE = '22023';
        END IF;
        IF COALESCE((v_value->>'expires')::boolean, false) THEN
            BEGIN
                v_ttl := COALESCE((v_value->>'ttl_days')::integer, (v_value->>'ttlDays')::integer);
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'bucket % ttl must be an integer', v_key USING ERRCODE = '22023';
            END;
            IF v_ttl IS NULL OR v_ttl <= 0 THEN
                RAISE EXCEPTION 'expiring bucket % needs a positive ttl_days', v_key USING ERRCODE = '22023';
            END IF;
        END IF;
    END LOOP;

    IF p_config ? 'billing' AND jsonb_typeof(p_config->'billing') <> 'object' THEN
        RAISE EXCEPTION 'catalog billing must be an object' USING ERRCODE = '22023';
    END IF;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
