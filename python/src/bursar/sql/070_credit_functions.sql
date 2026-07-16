-- Name: _upsert_billing_provider_ref(text, text, text, text, text, text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar._upsert_billing_provider_ref(p_resource_type text, p_provider text, p_price_id text DEFAULT NULL::text, p_product_id text DEFAULT NULL::text, p_variant_id text DEFAULT NULL::text, p_lookup_key text DEFAULT NULL::text, p_resource_key text DEFAULT NULL::text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_ref_id UUID;
    v_price_id TEXT := p_price_id;
    v_product_id TEXT := p_product_id;
    v_variant_id TEXT := p_variant_id;
    v_lookup_key TEXT := p_lookup_key;
BEGIN
    SELECT id INTO v_ref_id FROM bursar.billing_provider_refs
    WHERE provider = p_provider AND resource_type = p_resource_type
    AND (
        (v_price_id IS NOT NULL AND price_id = v_price_id)
        OR (v_product_id IS NOT NULL AND product_id = v_product_id)
        OR (v_lookup_key IS NOT NULL AND lookup_key = v_lookup_key)
    )
    ORDER BY updated_at DESC
    LIMIT 1;

    IF v_ref_id IS NOT NULL THEN
        UPDATE bursar.billing_provider_refs SET
            price_id = COALESCE(v_price_id, price_id),
            product_id = COALESCE(v_product_id, product_id),
            variant_id = COALESCE(v_variant_id, variant_id),
            lookup_key = COALESCE(v_lookup_key, lookup_key),
            resource_key = COALESCE(p_resource_key, resource_key),
            active = true,
            updated_at = now()
        WHERE id = v_ref_id;
    ELSE
        INSERT INTO bursar.billing_provider_refs (
            provider, price_id, product_id, variant_id,
            lookup_key, resource_type, resource_key, active
        ) VALUES (
            p_provider, v_price_id, v_product_id, v_variant_id,
            v_lookup_key, p_resource_type, COALESCE(p_resource_key, ''), true
        );
    END IF;
END;
$$;


--
-- Name: _walk_and_debit_buckets(uuid, numeric); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar._walk_and_debit_buckets(p_user_id uuid, p_amount numeric) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_bucket_breakdown JSONB := '{}'::jsonb;
    v_bucket_remaining NUMERIC;
    v_walk RECORD;
    v_bucket_balance NUMERIC;
    v_take NUMERIC;
    v_sink_bucket TEXT;
BEGIN
    v_bucket_remaining := p_amount;

    FOR v_walk IN
        SELECT bucket_key, priority, 0 AS grp FROM bursar.credit_buckets
        UNION ALL
        SELECT uct.bucket_key, 0, 1 AS grp
        FROM bursar.user_credit_buckets uct
        WHERE uct.user_id = p_user_id
          AND NOT EXISTS (SELECT 1 FROM bursar.credit_buckets ct WHERE ct.bucket_key = uct.bucket_key)
        ORDER BY grp ASC, priority ASC, bucket_key ASC
    LOOP
        EXIT WHEN v_bucket_remaining <= 0;

        SELECT balance INTO v_bucket_balance
        FROM bursar.user_credit_buckets
        WHERE user_id = p_user_id AND bucket_key = v_walk.bucket_key
        FOR UPDATE;
        v_bucket_balance := COALESCE(v_bucket_balance, 0);

        v_take := LEAST(v_bucket_balance, v_bucket_remaining);
        IF v_take > 0 THEN
            UPDATE bursar.user_credit_buckets
            SET balance = balance - v_take, updated_at = now()
            WHERE user_id = p_user_id AND bucket_key = v_walk.bucket_key;

            v_bucket_breakdown := v_bucket_breakdown || jsonb_build_object(v_walk.bucket_key, v_take);
            v_bucket_remaining := v_bucket_remaining - v_take;
        END IF;
    END LOOP;

    IF v_bucket_remaining > 0 THEN
        SELECT bucket_key INTO v_sink_bucket FROM bursar.credit_buckets WHERE allow_overdraft = true ORDER BY priority DESC, bucket_key DESC LIMIT 1;
        IF v_sink_bucket IS NULL THEN
            SELECT bucket_key INTO v_sink_bucket FROM bursar.credit_buckets ORDER BY priority DESC, bucket_key DESC LIMIT 1;
        END IF;
        IF v_sink_bucket IS NULL THEN
            v_sink_bucket := 'default';
        END IF;

        INSERT INTO bursar.user_credit_buckets (user_id, bucket_key, balance)
        VALUES (p_user_id, v_sink_bucket, -v_bucket_remaining)
        ON CONFLICT (user_id, bucket_key) DO UPDATE SET
            balance = bursar.user_credit_buckets.balance - v_bucket_remaining,
            updated_at = now();

        v_bucket_breakdown := v_bucket_breakdown || jsonb_build_object(
            v_sink_bucket, COALESCE((v_bucket_breakdown->>v_sink_bucket)::numeric, 0) + v_bucket_remaining
        );
        v_bucket_remaining := 0;
    END IF;

    RETURN jsonb_build_object(
        'bucket_breakdown', v_bucket_breakdown
    );
END;
$$;


--
-- Name: activate_bursar_config(integer); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.activate_bursar_config(p_version integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_target_id uuid;
    v_config jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));
    SELECT id, config INTO v_target_id, v_config FROM bursar.bursar_config WHERE version = p_version;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'version_not_found');
    END IF;
    PERFORM bursar.validate_bursar_config(v_config);
    UPDATE bursar.bursar_config SET active = false WHERE active;
    UPDATE bursar.bursar_config SET active = true WHERE version = p_version;
    PERFORM bursar.sync_plans_from_config(v_config, p_version);
    PERFORM bursar.sync_buckets_from_config(v_config, p_version);
    PERFORM bursar.sync_billing_from_config(v_config->'billing');
    RETURN jsonb_build_object('id', v_target_id, 'version', p_version, 'active', true);
END;
$$;


--
-- Name: add_team_member(uuid, uuid, text, numeric); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.add_team_member(p_team_id uuid, p_user_id uuid, p_role text DEFAULT 'member'::text, p_spend_cap numeric DEFAULT NULL::numeric) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
  -- Existence check up front: without it, a nonexistent p_team_id surfaces as
  -- a raw FK unique_violation/foreign_key_violation exception instead of the
  -- structured {"error": "team_not_found"} envelope deduct_team already
  -- returns for the same condition.
  IF NOT EXISTS (SELECT 1 FROM bursar.credit_teams WHERE id = p_team_id) THEN
    RETURN jsonb_build_object('error', 'team_not_found');
  END IF;

  INSERT INTO bursar.credit_team_members (team_id, user_id, role, spend_cap, total_spent)
  VALUES (p_team_id, p_user_id, p_role, p_spend_cap, 0)
  ON CONFLICT (team_id, user_id) DO UPDATE SET
    role = p_role,
    spend_cap = COALESCE(p_spend_cap, credit_team_members.spend_cap);

  UPDATE bursar.credit_teams
  SET member_count = (SELECT COUNT(*) FROM bursar.credit_team_members WHERE team_id = p_team_id),
      updated_at = now()
  WHERE id = p_team_id;

  RETURN jsonb_build_object(
    'team_id', p_team_id,
    'user_id', p_user_id,
    'role', p_role
  );
END;
$$;


--
-- Name: aggregate_stats(timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.aggregate_stats(p_start timestamp with time zone, p_end timestamp with time zone) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    result JSON;
    day_count BIGINT;
BEGIN
    -- Count distinct days in the window (UTC buckets for determinism)
    SELECT COUNT(DISTINCT (created_at AT TIME ZONE 'UTC')::DATE) INTO day_count
    FROM bursar.credit_transactions
    WHERE type = 'usage'
      AND amount < 0
      AND created_at >= p_start
      AND created_at < p_end;

    -- Money is NUMERIC(18,4): consumed total and the average stay NUMERIC.
    -- avg_daily_spend uses NUMERIC division (not integer division) so sub-credit
    -- daily averages are not truncated to 0. The SUM is cast to NUMERIC
    -- explicitly so dividing by a BIGINT day_count yields a NUMERIC quotient.
    SELECT jsonb_build_object(
        'total_credits_consumed', COALESCE(SUM(ABS(amount)), 0)::NUMERIC,
        'active_users', COUNT(DISTINCT user_id)::BIGINT,
        'avg_daily_spend', CASE WHEN day_count > 0
            THEN COALESCE(SUM(ABS(amount))::NUMERIC / day_count::NUMERIC, 0)
            ELSE 0::NUMERIC END,
        'top_model', COALESCE(
            (SELECT metadata->>'model'
             FROM bursar.credit_transactions
             WHERE type = 'usage'
               AND amount < 0
               AND created_at >= p_start
               AND created_at < p_end
             GROUP BY metadata->>'model'
             ORDER BY SUM(ABS(amount)) DESC
             LIMIT 1),
            ''
        ),
        'top_user', COALESCE(
            (SELECT user_id::TEXT
             FROM bursar.credit_transactions
             WHERE type = 'usage'
               AND amount < 0
               AND created_at >= p_start
               AND created_at < p_end
             GROUP BY user_id
             ORDER BY SUM(ABS(amount)) DESC
             LIMIT 1),
            ''
        )
    ) INTO result
    FROM bursar.credit_transactions
    WHERE type = 'usage'
      AND amount < 0
      AND created_at >= p_start
      AND created_at < p_end;

    RETURN result;
END;
$$;


--
-- Name: allocate_ledger_entry_lots(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.allocate_ledger_entry_lots() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
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
    IF NEW.amount > 0 THEN
        INSERT INTO bursar.credit_lots (account_id, source_entry_id, granted, expires_at, bucket)
        VALUES (
            NEW.account_id, NEW.id, NEW.amount,
            NULLIF(NEW.metadata->>'expires_at', '')::timestamptz,
            COALESCE(v_bucket, 'default')
        );
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
        ORDER BY
          CASE WHEN v_is_expiry THEN expires_at END NULLS LAST,
          CASE WHEN NOT v_is_expiry THEN expires_at END NULLS LAST,
          created_at, id
        FOR UPDATE
    LOOP
        EXIT WHEN v_remaining = 0;
        v_available := v_lot.granted - v_lot.consumed;
        v_take := LEAST(v_available, v_remaining);
        UPDATE bursar.credit_lots SET consumed = consumed + v_take WHERE id = v_lot.id;
        INSERT INTO bursar.credit_lot_allocations (debit_entry_id, lot_id, amount)
        VALUES (NEW.id, v_lot.id, v_take);
        v_remaining := v_remaining - v_take;
    END LOOP;
    IF v_remaining > 0 THEN
        INSERT INTO bursar.credit_lot_allocations (debit_entry_id, lot_id, amount)
        VALUES (NEW.id, NULL, v_remaining);
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: assign_credit_account(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.assign_credit_account() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_team_id uuid;
BEGIN
  NEW.acting_user_id := coalesce(NEW.acting_user_id, NEW.user_id);
  IF NEW.account_id IS NOT NULL THEN RETURN NEW; END IF;
  IF NEW.type::text = 'team_usage' AND NEW.metadata ? 'team_id' THEN
    v_team_id := (NEW.metadata->>'team_id')::uuid;
    INSERT INTO bursar.credit_accounts(account_type, team_id)
    VALUES ('team', v_team_id) ON CONFLICT DO NOTHING;
    SELECT id INTO NEW.account_id FROM bursar.credit_accounts
      WHERE account_type = 'team' AND team_id = v_team_id;
  ELSE
    INSERT INTO bursar.credit_accounts(account_type, user_id)
    VALUES ('personal', NEW.user_id) ON CONFLICT DO NOTHING;
    SELECT id INTO NEW.account_id FROM bursar.credit_accounts
      WHERE account_type = 'personal' AND user_id = NEW.user_id;
  END IF;
  RETURN NEW;
END
$$;


--
-- Name: capture_activated_catalog_snapshot(); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.capture_activated_catalog_snapshot() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
  IF NEW.active THEN
    PERFORM bursar.snapshot_catalog_objects(NEW.version, NEW.config);
  END IF;
  RETURN NEW;
END;
$$;


--
-- Name: check_balance_invariant(uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.check_balance_invariant(p_user_id uuid DEFAULT NULL::uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_row RECORD;
    v_drift JSONB := '[]'::jsonb;
BEGIN
    FOR v_row IN
        SELECT
            uc.user_id,
            uc.balance AS aggregate_balance,
            COALESCE(SUM(ucb.balance), 0) AS bucket_sum
        FROM bursar.user_credits uc
        LEFT JOIN bursar.user_credit_buckets ucb ON ucb.user_id = uc.user_id
        WHERE p_user_id IS NULL OR uc.user_id = p_user_id
        GROUP BY uc.user_id, uc.balance
        HAVING uc.balance <> COALESCE(SUM(ucb.balance), 0)
    LOOP
        v_drift := v_drift || jsonb_build_array(jsonb_build_object(
            'user_id', v_row.user_id,
            'aggregate_balance', v_row.aggregate_balance,
            'bucket_sum', v_row.bucket_sum,
            'delta', v_row.aggregate_balance - v_row.bucket_sum
        ));
    END LOOP;

    RETURN jsonb_build_object(
        'ok', jsonb_array_length(v_drift) = 0,
        'drift_count', jsonb_array_length(v_drift),
        'drift', v_drift
    );
END;
$$;


--
-- Name: check_feature_limit(uuid, text, integer, date, date); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.check_feature_limit(p_user_id uuid, p_feature text, p_max_calls integer, p_period_start date, p_period_end date) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_used      INT;
  v_remaining INT;
BEGIN
  -- Windows are pinned to UTC (dates are treated as UTC midnight) for
  -- deterministic bucketing, matching resolve_calendar_window's contract.
  -- Deliberately no `amount < 0` filter (unlike check_spend_cap, which only
  -- cares about actual dollars spent): a call fully covered by free allowance
  -- nets to amount = 0 but is still one invocation and must still count.
  SELECT COUNT(*) INTO v_used
  FROM bursar.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND ct.type = 'usage'
    AND ct.metadata->>'feature' = p_feature
    AND ct.created_at >= (p_period_start::timestamp AT TIME ZONE 'UTC')
    AND ct.created_at < (p_period_end::timestamp AT TIME ZONE 'UTC');

  v_used := COALESCE(v_used, 0);
  v_remaining := GREATEST(p_max_calls - v_used, 0);

  RETURN jsonb_build_object(
    'limited', v_used >= p_max_calls,
    'limit', p_max_calls,
    'used', v_used,
    'remaining', v_remaining,
    'period_start', p_period_start,
    'period_end', p_period_end
  );
END;
$$;


--
-- Name: check_plan_allowance(uuid, date); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.check_plan_allowance(p_user_id uuid, p_period_start date DEFAULT NULL::date) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_plan_id UUID;
    v_allowance_amount NUMERIC;
    v_allowance_period TEXT;
    v_current_usage NUMERIC;
    v_period_start DATE;
    v_period_end DATE;
BEGIN
    SELECT uc.plan_id, cp.allowance_amount, cp.allowance_period
    INTO v_plan_id, v_allowance_amount, v_allowance_period
    FROM bursar.user_credits uc
    LEFT JOIN bursar.credit_plans cp ON cp.id = uc.plan_id
    WHERE uc.user_id = p_user_id;

    IF v_plan_id IS NULL THEN
        RETURN jsonb_build_object(
            'plan_id', NULL::UUID,
            'allowance_remaining', 0,
            'period_start', NULL::TEXT,
            'period_end', NULL::TEXT
        );
    END IF;

    v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);

    -- Inclusive end-of-period date, derived from v_period_start (not `now()`,
    -- so a call for a past period reports that period's own end, not the
    -- current month's) per allowance_period:
    --   rolling_30d  -> a fixed 30-day window: start + 29.
    --   calendar_month / anniversary -> last day of the month starting at
    --     v_period_start. Exact for calendar_month; an approximation for
    --     anniversary (the true reset day, clamped per month, needs the
    --     plan-assignment anchor that this RPC doesn't receive — resolved
    --     precisely by allowance.py's resolve_allowance_window instead).
    v_period_end := CASE v_allowance_period
        WHEN 'rolling_30d' THEN v_period_start + 29
        ELSE (date_trunc('month', v_period_start) + interval '1 month' - interval '1 day')::DATE
    END;

    SELECT COALESCE(SUM(usage), 0) INTO v_current_usage
    FROM bursar.credit_usage_window
    WHERE user_id = p_user_id
      AND plan_id = v_plan_id
      AND billing_period = v_period_start;

    RETURN jsonb_build_object(
        'plan_id', v_plan_id,
        'allowance_remaining', GREATEST(v_allowance_amount - v_current_usage, 0),
        'period_start', v_period_start::TEXT,
        'period_end', v_period_end::TEXT
    );
END;
$$;


--
-- Name: check_spend_cap(uuid, text, numeric); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.check_spend_cap(p_user_id uuid, p_model text DEFAULT NULL::text, p_amount numeric DEFAULT 0) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_cap RECORD;
  v_spend NUMERIC;
  v_window TIMESTAMPTZ;
  v_capped TEXT := NULL;
  v_soft_spend NUMERIC := 0;
  v_soft_limit NUMERIC := 0;
  v_soft_model TEXT := NULL;
  v_soft_action TEXT := NULL;
BEGIN
  -- Single pass over ALL caps (deny first by ORDER BY, then warn/notify).
  -- First matched breach wins; deny takes precedence over soft.
  FOR v_cap IN
    SELECT action, cap_type, model, cap_limit
    FROM bursar.credit_spend_caps
    WHERE user_id = p_user_id
      AND (model IS NULL OR model = p_model)
    ORDER BY (action = 'deny') DESC, cap_limit ASC
  LOOP
    v_window := CASE v_cap.cap_type
      WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
      ELSE date_trunc('month', now() AT TIME ZONE 'UTC')
    END;

    SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_spend
    FROM bursar.credit_transactions ct
    WHERE ct.user_id = p_user_id
      AND ct.type IN ('usage', 'team_usage')
      AND ct.amount < 0
      AND ct.created_at >= v_window
      AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);

    IF v_spend + p_amount > v_cap.cap_limit THEN
      IF v_cap.action = 'deny' THEN
        RETURN jsonb_build_object('capped', true, 'current_spend', v_spend, 'cap_limit', v_cap.cap_limit, 'action', v_cap.action, 'model', v_cap.model);
      ELSE
        -- First soft breach wins (warn/notify); capture that cap's metadata.
        IF v_capped IS NULL THEN
          v_capped := v_cap.action;
          v_soft_spend := v_spend;
          v_soft_limit := v_cap.cap_limit;
          v_soft_model := v_cap.model;
          v_soft_action := v_cap.action;
        END IF;
      END IF;
    END IF;
  END LOOP;

  IF v_capped IS NOT NULL THEN
    RETURN jsonb_build_object(
      'capped', false,
      'current_spend', v_soft_spend,
      'cap_limit', v_soft_limit,
      'action', v_soft_action,
      'model', v_soft_model
    );
  END IF;

  RETURN jsonb_build_object('capped', false, 'current_spend', 0, 'cap_limit', 0, 'action', null);
END;
$$;


--
-- Name: claim_billing_event(text, text, text, jsonb, integer, integer); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.claim_billing_event(p_provider text, p_event_id text, p_event_type text, p_envelope jsonb DEFAULT '{}'::jsonb, p_lease_seconds integer DEFAULT 300, p_attempt_limit integer DEFAULT 3) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_event bursar.billing_events;
  v_token uuid := gen_random_uuid();
BEGIN
  INSERT INTO bursar.billing_events(provider, provider_event_id, event_type, status, envelope, claim_token, claim_expires_at)
  VALUES (p_provider, p_event_id, p_event_type, 'processing',
          jsonb_strip_nulls(jsonb_build_object('id', p_envelope->>'id', 'type', p_envelope->>'type', 'created_at', p_envelope->>'created_at')),
          v_token, now() + make_interval(secs => greatest(p_lease_seconds, 1)))
  ON CONFLICT (provider, provider_event_id) DO NOTHING
  RETURNING * INTO v_event;
  IF v_event.id IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'claimed', 'event_id', v_event.id, 'claim_token', v_token);
  END IF;
  SELECT * INTO v_event FROM bursar.billing_events
  WHERE provider = p_provider AND provider_event_id = p_event_id FOR UPDATE;
  IF v_event.status = 'completed' THEN RETURN jsonb_build_object('status', 'duplicate'); END IF;
  IF v_event.retry_count >= greatest(p_attempt_limit, 1) THEN RETURN jsonb_build_object('status', 'max_retries_exceeded'); END IF;
  IF v_event.status = 'processing' AND v_event.claim_expires_at >= now() THEN RETURN jsonb_build_object('status', 'retry'); END IF;
  UPDATE bursar.billing_events
  SET status = 'processing', retry_count = retry_count + 1, claim_token = v_token,
      claim_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 1)), updated_at = now()
  WHERE id = v_event.id;
  RETURN jsonb_build_object('status', 'claimed', 'event_id', v_event.id, 'claim_token', v_token);
END
$$;


--
-- Name: complete_billing_event(text, text, uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.complete_billing_event(p_provider text, p_event_id text, p_claim_token uuid) RETURNS boolean
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO ''
    AS $$
  UPDATE bursar.billing_events SET status = 'completed', claim_token = NULL, claim_expires_at = NULL, updated_at = now()
  WHERE provider = p_provider AND provider_event_id = p_event_id AND status = 'processing'
    AND claim_token = p_claim_token AND claim_expires_at >= now()
  RETURNING true
$$;


--
-- Name: create_lease(uuid, numeric, text, text, numeric, integer, integer, text, numeric, jsonb, date, text, integer, text, date, date); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.create_lease(p_user_id uuid, p_amount numeric, p_operation_type text, p_billing_mode text DEFAULT 'strict'::text, p_floor numeric DEFAULT 0, p_max_concurrent integer DEFAULT NULL::integer, p_ttl_seconds integer DEFAULT 600, p_model text DEFAULT NULL::text, p_overdraft_floor numeric DEFAULT NULL::numeric, p_metadata jsonb DEFAULT '{}'::jsonb, p_period_start date DEFAULT NULL::date, p_feature text DEFAULT NULL::text, p_feature_max_calls integer DEFAULT NULL::integer, p_feature_action text DEFAULT NULL::text, p_feature_period_start date DEFAULT NULL::date, p_feature_period_end date DEFAULT NULL::date) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_balance         NUMERIC;
    v_plan_id         UUID;
    v_allowance_amount  NUMERIC;
    v_period_start    DATE;
    v_used            NUMERIC;
    v_allowance_avail NUMERIC := 0;
    v_active_cnt      INTEGER;
    v_reserved        NUMERIC;
    v_available       NUMERIC;
    v_cap             RECORD;
    v_cap_window      TIMESTAMPTZ;
    v_cap_spend       NUMERIC;
    v_feature_count   INT;
    v_lease_id        UUID;
    v_expires_at      TIMESTAMPTZ;
    v_existing        RECORD;
BEGIN
    -- Idempotent lease admission: concurrent retries return the original
    -- reservation rather than surfacing a unique-constraint error.
    IF COALESCE(p_metadata, '{}'::jsonb) ? 'idempotency_key'
       AND NULLIF(p_metadata->>'idempotency_key', '') IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                p_user_id::text || ':' || p_operation_type || ':' ||
                NULLIF(p_metadata->>'idempotency_key', ''), 0
            )
        );
        SELECT id, amount, billing_mode, expires_at, status
        INTO v_existing
        FROM bursar.credit_reservations
        WHERE user_id = p_user_id
          AND operation_type = p_operation_type
          AND idempotency_key = NULLIF(p_metadata->>'idempotency_key', '')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        FOR UPDATE;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'lease_id', v_existing.id,
                'user_id', p_user_id,
                'amount', v_existing.amount,
                'billing_mode', v_existing.billing_mode,
                'expires_at', v_existing.expires_at,
                'status', v_existing.status,
                'replayed', true
            );
        END IF;
    END IF;

    IF p_amount IS NULL OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric OR p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- Lock the balance row (and capture plan_id), creating it if missing.
    SELECT balance, plan_id INTO v_balance, v_plan_id
    FROM bursar.user_credits WHERE user_id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO bursar.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0) ON CONFLICT (user_id) DO NOTHING;
        SELECT balance, plan_id INTO v_balance, v_plan_id
        FROM bursar.user_credits WHERE user_id = p_user_id FOR UPDATE;
    END IF;

    -- (1A) Allowance headroom: remaining free allowance counts toward available
    --      funds at admission. v_period_start: explicit p_period_start else
    --      the current UTC calendar month (unchanged).
    IF v_plan_id IS NOT NULL THEN
        SELECT allowance_amount INTO v_allowance_amount
        FROM bursar.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM bursar.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_allowance_avail := GREATEST(COALESCE(v_allowance_amount, 0) - COALESCE(v_used, 0), 0);
    END IF;

    -- (2) Concurrency: count active, unexpired leases for this operation type.
    IF p_max_concurrent IS NOT NULL THEN
        SELECT COUNT(*) INTO v_active_cnt
        FROM bursar.credit_reservations
        WHERE user_id = p_user_id AND operation_type = p_operation_type
          AND status = 'active' AND expires_at > now();
        IF v_active_cnt >= p_max_concurrent THEN
            RETURN jsonb_build_object('error', 'concurrency_limit', 'billing_mode', p_billing_mode);
        END IF;
    END IF;

    -- (3) Deny spend cap at admission (a blocked user can't even start).
    FOR v_cap IN
        SELECT cap_type, model, cap_limit FROM bursar.credit_spend_caps
        WHERE user_id = p_user_id AND action = 'deny' AND (model IS NULL OR model = p_model)
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
        IF v_cap_spend + p_amount > v_cap.cap_limit THEN
            RETURN jsonb_build_object('error', 'cap_reached', 'billing_mode', p_billing_mode);
        END IF;
    END LOOP;

    -- (3b) Deny-only feature limit at admission — same ledger-derived count as
    -- deduct_with_allowance/settle_lease, but only ever enforces 'deny'
    -- (warn/notify are not checked here: nothing has been charged yet, so
    -- there is nothing to warn about). Skipped when no feature/limit was
    -- resolved by the caller (manager).
    --
    -- Best-effort, not exact: unlike deduct_with_allowance (which counts AND
    -- inserts the counted `usage` row under the same `user_credits FOR
    -- UPDATE` lock, making the count-then-charge atomic), create_lease counts
    -- committed `usage` rows but inserts none itself — a lease is a hold, not
    -- a charge; the `usage` row lands later at settle_lease. Two concurrent
    -- create_lease calls at the deny boundary can therefore both read the
    -- same count and both be admitted, exceeding p_feature_max_calls by one.
    -- This mirrors the existing spend-cap admission check three blocks above
    -- (also a pre-check, not a lock), and is judged acceptable for the same
    -- reason: settle_lease still enforces the limit (advisory, via
    -- v_feature_limit_warning) on the actual charge.
    IF p_feature IS NOT NULL AND p_feature_max_calls IS NOT NULL AND p_feature_action = 'deny' THEN
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
            RETURN jsonb_build_object('error', 'feature_limit_reached', 'billing_mode', p_billing_mode);
        END IF;
    END IF;

    -- (4) effective_available = balance − Σ active holds + allowance headroom.
    --     Allowance covers the gap so free-tier users aren't falsely rejected.
    SELECT COALESCE(SUM(amount), 0) INTO v_reserved
    FROM bursar.credit_reservations
    WHERE user_id = p_user_id AND status = 'active' AND expires_at > now();

    v_available := v_balance - v_reserved + v_allowance_avail;
    IF v_available - p_amount < p_floor THEN
        RETURN jsonb_build_object(
            'error', 'insufficient_credits',
            'available', v_available, 'reserved', v_reserved, 'billing_mode', p_billing_mode
        );
    END IF;

    -- (5) Insert the active lease.
    v_expires_at := now() + make_interval(secs => p_ttl_seconds);
    INSERT INTO bursar.credit_reservations
        (user_id, amount, operation_type, metadata, expires_at, status, billing_mode, overdraft_floor)
    VALUES
        (p_user_id, p_amount, p_operation_type, COALESCE(p_metadata, '{}'::jsonb),
         v_expires_at, 'active', p_billing_mode, p_overdraft_floor)
    RETURNING id INTO v_lease_id;

    RETURN jsonb_build_object(
        'lease_id', v_lease_id,
        'user_id', p_user_id,
        'amount', p_amount,
        'available', v_available - p_amount,
        'reserved', v_reserved + p_amount,
        'billing_mode', p_billing_mode,
        'expires_at', v_expires_at
    );
END;
$$;


--
-- Name: create_team(text, numeric); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.create_team(p_name text, p_initial_balance numeric DEFAULT 0) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_team_id UUID;
BEGIN
  INSERT INTO bursar.credit_teams (name, balance)
  VALUES (p_name, p_initial_balance)
  RETURNING id INTO v_team_id;

  RETURN jsonb_build_object(
    'team_id', v_team_id,
    'name', p_name
  );
END;
$$;


--
-- Name: credits_add(uuid, numeric, bursar.credit_tx_type, jsonb, text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.credits_add(p_user_id uuid, p_amount numeric, p_type bursar.credit_tx_type DEFAULT 'purchase'::bursar.credit_tx_type, p_metadata jsonb DEFAULT NULL::jsonb, p_bucket text DEFAULT NULL::text, p_idempotency_key text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
    IF current_setting('request.jwt.claim.role', true) IS NOT NULL
       AND current_setting('request.jwt.claim.role', true) <> 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    RETURN bursar.credits_add_internal(
        p_user_id, p_amount, p_type, p_metadata, p_bucket, p_idempotency_key
    );
END;
$$;


--
-- Name: credits_add_internal(uuid, numeric, bursar.credit_tx_type, jsonb, text, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.credits_add_internal(p_user_id uuid, p_amount numeric, p_type bursar.credit_tx_type DEFAULT 'adjustment'::bursar.credit_tx_type, p_metadata jsonb DEFAULT NULL::jsonb, p_bucket text DEFAULT NULL::text, p_idempotency_key text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_new_balance NUMERIC;
    v_lifetime NUMERIC;
    v_transaction_id UUID;
    v_buckets_configured BOOLEAN;
    v_resolved_bucket TEXT;
    v_bucket_expires BOOLEAN;
    v_bucket_ttl_days INTEGER;
    v_has_expires_at BOOLEAN;
    v_computed_expires_at TIMESTAMPTZ;
    v_metadata JSONB;
    v_existing_amount NUMERIC;
    v_existing_bucket TEXT;
BEGIN
    -- No auth.role() guard here by design — see the header comment above.
    -- Callable only from within the database (this function, credits_add
    -- below, and grant_signup_bonus); EXECUTE is REVOKEd from
    -- PUBLIC/anon/authenticated below.

    -- Reject non-finite amounts (NaN / +-Infinity) outright.
    IF p_amount IS NULL OR NOT (p_amount = p_amount) OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- Purchases (and other credit grants) must be strictly positive.
    -- Negative/zero amounts are only allowed via an explicit 'adjustment' or 'refund'.
    IF p_type NOT IN ('adjustment', 'refund') AND p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- ── Idempotency replay (user-scoped) ─────────────────────────────────
    -- Runs before tier resolution so a replay never trips tier_not_found/
    -- tier_required/expires_at validation on a redelivered call — the
    -- original grant already happened; nothing further is validated.
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id, amount, COALESCE(metadata->>'bucket', 'default')
        INTO v_transaction_id, v_existing_amount, v_existing_bucket
        FROM bursar.credit_transactions
        WHERE user_id = p_user_id
          AND type = p_type
          AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;

        IF FOUND THEN
            SELECT balance, lifetime_purchased INTO v_new_balance, v_lifetime
            FROM bursar.user_credits
            WHERE user_id = p_user_id;

            RETURN jsonb_build_object(
                'id', v_transaction_id,
                'user_id', p_user_id,
                'amount', v_existing_amount,
                'new_balance', COALESCE(v_new_balance, 0),
                'lifetime_purchased', COALESCE(v_lifetime, 0),
                'bucket', v_existing_bucket
            );
        END IF;
    END IF;

    -- ── Bucket resolution ────────────────────────────────────────────────
    v_buckets_configured := EXISTS (SELECT 1 FROM bursar.credit_buckets);

    IF NOT v_buckets_configured THEN
        IF p_bucket IS NOT NULL AND p_bucket <> 'default' THEN
            RETURN jsonb_build_object('error', 'tier_not_found', 'bucket',p_bucket);
        END IF;
        v_resolved_bucket := 'default';
    ELSIF p_bucket IS NOT NULL THEN
        SELECT bucket_key, expires, ttl_days
        INTO v_resolved_bucket, v_bucket_expires, v_bucket_ttl_days
        FROM bursar.credit_buckets
        WHERE bucket_key = p_bucket;

        IF NOT FOUND THEN
            RETURN jsonb_build_object('error', 'tier_not_found', 'bucket',p_bucket);
        END IF;
    ELSE
        SELECT bucket_key, expires, ttl_days
        INTO v_resolved_bucket, v_bucket_expires, v_bucket_ttl_days
        FROM bursar.credit_buckets
        WHERE is_default = true
        ORDER BY priority ASC, bucket_key ASC
        LIMIT 1;

        IF NOT FOUND THEN
            RETURN jsonb_build_object('error', 'tier_required');
        END IF;
    END IF;

    v_metadata := COALESCE(p_metadata, '{}'::jsonb);

    -- ── expires_at reconciliation against the resolved bucket ───────────
    -- Only applies when buckets are configured (v_bucket_expires is NULL, i.e.
    -- falsy, in the no-buckets-configured branch above, so this whole block
    -- is skipped there).
    IF v_buckets_configured THEN
        v_has_expires_at := v_metadata ? 'expires_at';

        IF NOT COALESCE(v_bucket_expires, false) THEN
            IF v_has_expires_at THEN
                RETURN jsonb_build_object('error', 'bucket_does_not_expire', 'bucket', v_resolved_bucket);
            END IF;
        ELSE
            IF NOT v_has_expires_at THEN
                IF v_bucket_ttl_days IS NULL THEN
                    RETURN jsonb_build_object('error', 'expires_at_required', 'bucket', v_resolved_bucket);
                END IF;
                v_computed_expires_at := now() + (v_bucket_ttl_days || ' days')::interval;
                v_metadata := v_metadata || jsonb_build_object('expires_at', to_jsonb(v_computed_expires_at));
            ELSE
                -- Parity with MemoryStore (Python/JS): an explicit expires_at
                -- must be in the future, not just present.
                IF (v_metadata->>'expires_at')::timestamptz <= now() THEN
                    RETURN jsonb_build_object(
                        'error', 'invalid_expires_at',
                        'bucket', v_resolved_bucket,
                        'expires_at', v_metadata->>'expires_at'
                    );
                END IF;
            END IF;
        END IF;
    END IF;

    v_metadata := v_metadata || jsonb_build_object('bucket', v_resolved_bucket);
    IF p_idempotency_key IS NOT NULL THEN
        v_metadata := v_metadata || jsonb_build_object('idempotency_key', p_idempotency_key);
    END IF;

    INSERT INTO bursar.user_credits (user_id, balance, lifetime_purchased)
    VALUES (p_user_id, p_amount, CASE WHEN p_type = 'purchase' THEN p_amount ELSE 0 END)
    ON CONFLICT (user_id) DO UPDATE SET
        balance = bursar.user_credits.balance + p_amount,
        lifetime_purchased = CASE WHEN p_type = 'purchase'
            THEN bursar.user_credits.lifetime_purchased + p_amount
            ELSE bursar.user_credits.lifetime_purchased
        END,
        updated_at = now()
    RETURNING balance, lifetime_purchased INTO v_new_balance, v_lifetime;

    -- Per-bucket balance: lazily created on first touch.
    INSERT INTO bursar.user_credit_buckets (user_id, bucket_key, balance)
    VALUES (p_user_id, v_resolved_bucket, p_amount)
    ON CONFLICT (user_id, bucket_key) DO UPDATE SET
        balance = bursar.user_credit_buckets.balance + p_amount,
        updated_at = now();

    INSERT INTO bursar.credit_transactions (user_id, amount, type, metadata)
    VALUES (p_user_id, p_amount, p_type, v_metadata)
    RETURNING id INTO v_transaction_id;

    RETURN jsonb_build_object(
        'id', v_transaction_id,
        'user_id', p_user_id,
        'amount', p_amount,
        'new_balance', v_new_balance,
        'lifetime_purchased', v_lifetime,
        'bucket', v_resolved_bucket
    );
END;
$$;


--
-- Name: daily_spend(timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.daily_spend(p_start timestamp with time zone, p_end timestamp with time zone) RETURNS TABLE(date text, total_spend numeric, transaction_count bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
    RETURN QUERY
    SELECT
        (ct.created_at AT TIME ZONE 'UTC')::DATE::TEXT AS date,
        COALESCE(SUM(ABS(ct.amount)), 0)::NUMERIC AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM bursar.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at < p_end
    GROUP BY (ct.created_at AT TIME ZONE 'UTC')::DATE
    ORDER BY (ct.created_at AT TIME ZONE 'UTC')::DATE;
END;
$$;


--
-- Name: deactivate_other_provider_subscriptions(uuid, text); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.deactivate_other_provider_subscriptions(p_user_id uuid, p_keep_provider text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE bursar.billing_subscriptions
    SET status = 'canceled',
        cancel_at_period_end = true,
        updated_at = now()
    WHERE user_id = p_user_id
      AND provider != p_keep_provider
      AND status IN ('active', 'trialing');

    GET DIAGNOSTICS v_count = ROW_COUNT;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'keep_provider', p_keep_provider,
        'deactivated_count', v_count
    );
END;
$$;


--
-- Name: deduct_team(uuid, uuid, numeric, jsonb); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.deduct_team(p_team_id uuid, p_user_id uuid, p_amount numeric, p_metadata jsonb DEFAULT '{}'::jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_balance NUMERIC;
  v_spend_cap NUMERIC;
  v_is_member BOOLEAN;
  v_month_spent NUMERIC;
  v_tx_id UUID;
  v_idempotency_key TEXT;
  v_window TIMESTAMPTZ;
  v_replay_amount NUMERIC;
BEGIN
  IF p_amount IS NULL OR p_amount <= 0 THEN
    RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
  END IF;

  v_idempotency_key := p_metadata->>'idempotency_key';

  -- Idempotency replay (user + team scoped): return the original team_usage tx.
  IF v_idempotency_key IS NOT NULL THEN
    SELECT id, ABS(amount) INTO v_tx_id, v_replay_amount
    FROM bursar.credit_transactions
    WHERE user_id = p_user_id
      AND type = 'team_usage'
      AND metadata->>'idempotency_key' = v_idempotency_key
      AND metadata->>'team_id' = p_team_id::text;
    IF FOUND THEN
      RETURN jsonb_build_object(
        'transaction_id', v_tx_id,
        'team_id', p_team_id,
        'user_id', p_user_id,
        'amount', -v_replay_amount,
        'team_balance_after', (SELECT balance FROM bursar.credit_teams WHERE id = p_team_id),
        'idempotent', true
      );
    END IF;
  END IF;

  -- Get current team balance (locked to prevent concurrent deductions).
  -- Locked BEFORE the per-user spend-cap check below: two concurrent
  -- deduct_team calls for the same (user, team) both compute v_month_spent
  -- against the same committed rows only if they serialize first — taking
  -- this lock up front means the second caller blocks here until the first
  -- commits its team_usage ledger row, so its own cap re-check then sees the
  -- first debit and cannot also pass a cap that only room for one of them.
  SELECT balance INTO v_balance
  FROM bursar.credit_teams
  WHERE id = p_team_id
  FOR UPDATE;

  IF v_balance IS NULL THEN
    RETURN jsonb_build_object('error', 'team_not_found');
  END IF;

  -- Check user is a member and get spend cap
  SELECT ctm.spend_cap, true INTO v_spend_cap, v_is_member
  FROM bursar.credit_team_members ctm
  WHERE ctm.team_id = p_team_id AND ctm.user_id = p_user_id;

  IF v_is_member IS NULL THEN
    RETURN jsonb_build_object('error', 'user_not_in_team');
  END IF;

  -- Enforce per-user spend cap against the current monthly team spend (UTC).
  IF v_spend_cap IS NOT NULL THEN
    v_window := date_trunc('month', now() AT TIME ZONE 'UTC');
    SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_month_spent
    FROM bursar.credit_transactions ct
    WHERE ct.user_id = p_user_id
      AND ct.type = 'team_usage'
      AND ct.metadata->>'team_id' = p_team_id::text
      AND ct.created_at >= v_window;

    IF (v_month_spent + p_amount) > v_spend_cap THEN
      RETURN jsonb_build_object('error', 'cap_reached', 'current_spend', v_month_spent, 'cap_limit', v_spend_cap);
    END IF;
  END IF;

  IF v_balance < p_amount THEN
    RETURN jsonb_build_object('error', 'insufficient_credits');
  END IF;

  -- Log transaction + update team balance atomically. Both the balance UPDATEs
  -- and the INSERT sit inside the BEGIN/EXCEPTION block so a unique_violation
  -- (concurrent idempotency key) rolls back the balance change — preventing
  -- the double-deduction bug that existed when the UPDATE ran outside.
  BEGIN
    UPDATE bursar.credit_teams
    SET balance = balance - p_amount,
        updated_at = now()
    WHERE id = p_team_id
    RETURNING balance INTO v_balance;

    UPDATE bursar.credit_team_members
    SET total_spent = total_spent + p_amount
    WHERE team_id = p_team_id AND user_id = p_user_id;

    INSERT INTO bursar.credit_transactions (user_id, amount, type, metadata)
    VALUES (p_user_id, -p_amount, 'team_usage', p_metadata || jsonb_build_object('team_id', p_team_id))
    RETURNING id INTO v_tx_id;
  EXCEPTION WHEN unique_violation THEN
    SELECT id, ABS(amount) INTO v_tx_id, v_replay_amount
    FROM bursar.credit_transactions
    WHERE user_id = p_user_id
      AND type = 'team_usage'
      AND metadata->>'team_id' = p_team_id::text
      AND metadata->>'idempotency_key' = v_idempotency_key;
    RETURN jsonb_build_object(
      'transaction_id', v_tx_id,
      'team_id', p_team_id,
      'user_id', p_user_id,
      'amount', -v_replay_amount,
      'team_balance_after', (SELECT balance FROM bursar.credit_teams WHERE id = p_team_id),
      'idempotent', true
    );
  END;

  RETURN jsonb_build_object(
    'transaction_id', v_tx_id,
    'team_id', p_team_id,
    'user_id', p_user_id,
    'amount', -p_amount,
    'team_balance_after', v_balance,
    'idempotent', false
  );
END;
$$;


--
-- Name: deduct_with_allowance(uuid, numeric, text, numeric, text, jsonb, boolean, date, text, integer, text, date, date); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.deduct_with_allowance(p_user_id uuid, p_amount numeric, p_idempotency_key text DEFAULT NULL::text, p_min_balance numeric DEFAULT 0, p_model text DEFAULT NULL::text, p_metadata jsonb DEFAULT '{}'::jsonb, p_skip_allowance boolean DEFAULT false, p_period_start date DEFAULT NULL::date, p_feature text DEFAULT NULL::text, p_feature_max_calls integer DEFAULT NULL::integer, p_feature_action text DEFAULT NULL::text, p_feature_period_start date DEFAULT NULL::date, p_feature_period_end date DEFAULT NULL::date) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_balance              NUMERIC;
    v_plan_id              UUID;
    v_allowance_amount       NUMERIC;
    v_period_start         DATE;
    v_used                 NUMERIC;
    v_remaining            NUMERIC;
    v_consume              NUMERIC := 0;
    v_net                  NUMERIC;
    v_cap                  RECORD;
    v_cap_spend            NUMERIC;
    v_cap_window           TIMESTAMPTZ;
    v_cap_warning          TEXT := NULL;
    v_feature_count        INT;
    v_feature_limit_warning TEXT := NULL;
    v_new_balance          NUMERIC;
    v_transaction_id       UUID;
    v_metadata             JSONB;
    v_existing_id          UUID;
    v_existing_amt         NUMERIC;
    v_existing_cons        NUMERIC;
    v_existing_bal_after   NUMERIC;
    v_existing_bucket_bd     JSONB;
    v_bucket_breakdown       JSONB := '{}'::jsonb;
BEGIN
    IF p_amount IS NULL
       OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric
       OR p_amount = '-Infinity'::numeric
       OR p_amount < 0 THEN
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

    -- (2) Idempotency replay: return the original balance_after/bucket_breakdown
    --     from tx metadata rather than the (wrong) current balance.
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id,
               ABS(amount),
               COALESCE((metadata->>'allowance_consumed')::numeric, 0),
               COALESCE((metadata->>'balance_after')::numeric, v_balance),
               COALESCE(metadata->'bucket_breakdown', '{}'::jsonb)
        INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after, v_existing_bucket_bd
        FROM bursar.credit_transactions
        WHERE user_id = p_user_id
          AND type = 'usage'
          AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id,
                'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons,
                'balance_after', v_existing_bal_after,
                'idempotent', true,
                'cap_warning', NULL,
                'feature_limit_warning', NULL,
                'bucket_breakdown', v_existing_bucket_bd
            );
        END IF;
    END IF;

    -- (3) Allowance: skipped for fixed-cost jobs (p_skip_allowance = TRUE).
    -- v_period_start: explicit p_period_start (rolling_30d/anniversary,
    -- resolved by the manager) else the current UTC calendar month (unchanged).
    IF NOT p_skip_allowance AND v_plan_id IS NOT NULL THEN
        SELECT allowance_amount INTO v_allowance_amount
        FROM bursar.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM bursar.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_remaining := GREATEST(COALESCE(v_allowance_amount, 0) - COALESCE(v_used, 0), 0);
        v_consume   := LEAST(v_remaining, p_amount);
    END IF;

    v_net := p_amount - v_consume;

    BEGIN
        IF v_consume > 0 THEN
            INSERT INTO bursar.credit_usage_window (user_id, plan_id, billing_period, usage)
            VALUES (p_user_id, v_plan_id, v_period_start, v_consume)
            ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
                usage = bursar.credit_usage_window.usage + v_consume,
                updated_at = now();
        END IF;

        FOR v_cap IN
            SELECT action, cap_type, model, cap_limit
            FROM bursar.credit_spend_caps
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
            IF v_cap_spend + v_net > v_cap.cap_limit THEN
                IF v_cap.action = 'deny' THEN
                    RAISE EXCEPTION 'bursa_cap_reached' USING ERRCODE = 'DU001';
                ELSE
                    IF v_cap_warning IS NULL THEN v_cap_warning := v_cap.action; END IF;
                END IF;
            END IF;
        END LOOP;

        -- (4b) Feature limit: ledger-derived count of prior committed `usage`
        -- transactions tagged metadata.feature = p_feature within
        -- [p_feature_period_start, p_feature_period_end). Skipped entirely
        -- when no feature/limit was resolved by the caller (manager).
        IF p_feature IS NOT NULL AND p_feature_max_calls IS NOT NULL THEN
            -- Deliberately no `amount < 0` filter (unlike the spend-cap window
            -- query above, which only cares about actual dollars spent): a
            -- call fully covered by free allowance nets to amount = 0 but is
            -- still one invocation and must still count.
            SELECT COUNT(*) INTO v_feature_count
            FROM bursar.credit_transactions ct
            WHERE ct.user_id = p_user_id
              AND ct.type = 'usage'
              AND ct.metadata->>'feature' = p_feature
              AND ct.created_at >= (p_feature_period_start::timestamp AT TIME ZONE 'UTC')
              AND ct.created_at < (p_feature_period_end::timestamp AT TIME ZONE 'UTC');

            IF v_feature_count >= p_feature_max_calls THEN
                IF p_feature_action = 'deny' THEN
                    RAISE EXCEPTION 'bursa_feature_limit_reached' USING ERRCODE = 'DU003';
                ELSE
                    v_feature_limit_warning := p_feature_action;
                END IF;
            END IF;
        END IF;

        IF v_balance - v_net < p_min_balance THEN
            RAISE EXCEPTION 'bursa_insufficient_credits' USING ERRCODE = 'DU002';
        END IF;

        -- ── Bucket walk (delegated to shared helper) ─────────────────────
        -- Uses _walk_and_debit_buckets for the priority-ordered walk + overdraft sink.
        -- The helper returns bucket_breakdown; the amount was already floor-clamped
        -- by the checks above, so remaining > 0 after the walk should not occur
        -- in strict mode (handled via the overdraft sink path inside the helper).
        SELECT (result->>'bucket_breakdown')::jsonb INTO v_bucket_breakdown
        FROM bursar._walk_and_debit_buckets(p_user_id, v_net) AS result;

        UPDATE bursar.user_credits
        SET balance = balance - v_net, updated_at = now()
        WHERE user_id = p_user_id
        RETURNING balance INTO v_new_balance;

        -- Store balance_after/bucket_breakdown in metadata for correct
        -- idempotent replay.
        -- Tag metadata.feature whenever p_feature is given, regardless of
        -- whether a limit is currently configured (p_feature_max_calls may be
        -- NULL) — this is what makes the ledger-derived count accurate once a
        -- limit is enabled later, and is what future check_feature_limit /
        -- enforcement queries count against.
        v_metadata := COALESCE(p_metadata, '{}'::jsonb)
            || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model, 'feature', p_feature))
            || jsonb_build_object('allowance_consumed', v_consume, 'balance_after', v_new_balance, 'bucket_breakdown', v_bucket_breakdown);

        INSERT INTO bursar.credit_transactions (user_id, amount, type, reference_type, metadata)
        VALUES (p_user_id, -v_net, 'usage', p_metadata->>'reference_type', v_metadata)
        RETURNING id INTO v_transaction_id;

    EXCEPTION
        WHEN SQLSTATE 'DU001' THEN
            -- Custom SQLSTATE: DU001 = spend cap reached (deny), DU002 = insufficient credits,
            -- DU003 = feature limit reached. These are raised WITHIN the subtransaction so
            -- the EXCEPTION block rolls back any partial changes (allowance consumption,
            -- bucket debits) before returning a structured error envelope.
            RETURN jsonb_build_object('error', 'cap_reached', 'action', 'deny');
        WHEN SQLSTATE 'DU002' THEN
            RETURN jsonb_build_object('error', 'insufficient_credits');
        WHEN SQLSTATE 'DU003' THEN
            RETURN jsonb_build_object('error', 'feature_limit_reached', 'action', 'deny');
        WHEN unique_violation THEN
            SELECT id,
                   ABS(amount),
                   COALESCE((metadata->>'allowance_consumed')::numeric, 0),
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
                'idempotent', true, 'cap_warning', NULL, 'feature_limit_warning', NULL, 'bucket_breakdown', v_existing_bucket_bd
            );
    END;

    RETURN jsonb_build_object(
        'transaction_id', v_transaction_id,
        'amount', v_net,
        'allowance_consumed', v_consume,
        'balance_after', v_new_balance,
        'idempotent', false,
        'cap_warning', v_cap_warning,
        'feature_limit_warning', v_feature_limit_warning,
        'bucket_breakdown', v_bucket_breakdown
    );
END;
$$;


--
-- Name: expire_credits(boolean, uuid); Type: FUNCTION; Schema: bursar; Owner: -
--

CREATE FUNCTION bursar.expire_credits(p_dry_run boolean DEFAULT false, p_user_id uuid DEFAULT NULL::uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_expired_count INTEGER := 0;
    v_expired_amount NUMERIC := 0;
    v_expired_by_bucket JSONB := '{}'::jsonb;
    v_group RECORD;
    v_group_expired NUMERIC;
    v_current_bucket_balance NUMERIC;
    v_current_balance NUMERIC;
BEGIN
    -- A grant is "sweepable" when it has an expires_at in the past AND has not
    -- already been swept (no 'swept_at' marker). Marking swept grants is what
    -- makes the sweep idempotent: a second run finds nothing and never
    -- double-debits. Mirrors MemoryStore, which nulls expires_at on sweep.
    -- Grouping is per-(user_id, bucket) instead of per-user_id, reading bucket
    -- straight off each grant's own metadata->>'bucket' (a bucket's `expires` flag
    -- is only consulted at add_credits time; once stamped, a transaction's
    -- fate is fixed regardless of later config changes).
    -- p_user_id (lazy per-user sweep): when given, only that user's rows are
    -- ever considered; every other user's expired grants are left untouched.
    --
    -- Type filter covers every grant type credits_add can stamp expires_at
    -- onto (matches the bucket backfill's type set in 010_credit_buckets.sql
    -- L151) — not just purchase/adjustment. A signup_bonus or subscription
    -- grant into an expiring bucket gets an expires_at too, and must be
    -- sweepable like any other grant, or it never expires.
    FOR v_group IN
        SELECT DISTINCT user_id, COALESCE(metadata->>'bucket', 'default') AS bucket_key
        FROM bursar.credit_transactions
        WHERE type IN ('purchase', 'subscription', 'signup_bonus', 'adjustment')
          AND metadata ? 'expires_at'
          AND NOT (metadata ? 'swept_at')
          AND (metadata->>'expires_at')::timestamptz <= now()
          AND (p_user_id IS NULL OR user_id = p_user_id)
    LOOP
        -- Total un-swept expired grants for this (user, bucket).
        SELECT COALESCE(SUM(amount), 0) INTO v_group_expired
        FROM bursar.credit_transactions
        WHERE user_id = v_group.user_id
          AND COALESCE(metadata->>'bucket', 'default') = v_group.bucket_key
          AND type IN ('purchase', 'subscription', 'signup_bonus', 'adjustment')
          AND metadata ? 'expires_at'
          AND NOT (metadata ? 'swept_at')
          AND (metadata->>'expires_at')::timestamptz <= now();

        -- Lock the aggregate balance row (prevents racing a concurrent deduction).
        SELECT COALESCE(balance, 0) INTO v_current_balance
        FROM bursar.user_credits
        WHERE user_id = v_group.user_id
        FOR UPDATE;

        -- Lock (if present) this bucket's own balance row for this user.
        SELECT balance INTO v_current_bucket_balance
        FROM bursar.user_credit_buckets
        WHERE user_id = v_group.user_id AND bucket_key = v_group.bucket_key
        FOR UPDATE;
        v_current_bucket_balance := COALESCE(v_current_bucket_balance, 0);

        -- Cap at both the bucket's own balance and the aggregate (never expire
        -- money that isn't actually there under either ceiling).
        v_group_expired := LEAST(v_group_expired, v_current_bucket_balance, v_current_balance);

        IF v_group_expired > 0 THEN
            v_expired_count := v_expired_count + 1;
            v_expired_amount := v_expired_amount + v_group_expired;
            v_expired_by_bucket := v_expired_by_bucket || jsonb_build_object(
                v_group.bucket_key,
                COALESCE((v_expired_by_bucket->>v_group.bucket_key)::numeric, 0) + v_group_expired
            );

            IF NOT p_dry_run THEN
                -- Deduct expired amount from both the bucket and aggregate balances.
                UPDATE bursar.user_credit_buckets
                SET balance = balance - v_group_expired, updated_at = now()
                WHERE user_id = v_group.user_id AND bucket_key = v_group.bucket_key;

                UPDATE bursar.user_credits
                SET balance = balance - v_group_expired,
                    updated_at = now()
                WHERE user_id = v_group.user_id;

                -- Log one adjustment transaction per (user, bucket).
                INSERT INTO bursar.credit_transactions (user_id, amount, type, metadata)
                VALUES (v_group.user_id, -v_group_expired, 'adjustment',
                        jsonb_build_object('reason', 'credit_expired', 'expired_amount', v_group_expired, 'bucket', v_group.bucket_key));

                -- Mark swept only when we actually debited the expired amount.
                UPDATE bursar.credit_transactions
                SET metadata = metadata || jsonb_build_object('swept_at', to_jsonb(now()))
                WHERE user_id = v_group.user_id
                  AND COALESCE(metadata->>'bucket', 'default') = v_group.bucket_key
                  AND type IN ('purchase', 'subscription', 'signup_bonus', 'adjustment')
                  AND metadata ? 'expires_at'
                  AND NOT (metadata ? 'swept_at')
                  AND (metadata->>'expires_at')::timestamptz <= now();
            END IF;
        END IF;
    END LOOP;

    RETURN jsonb_build_object(
        'expired_count', v_expired_count,
        'expired_amount', v_expired_amount,
        'expired_by_bucket', v_expired_by_bucket,
        'dry_run', p_dry_run
    );
END;
$$;


--
