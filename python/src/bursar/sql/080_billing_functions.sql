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
