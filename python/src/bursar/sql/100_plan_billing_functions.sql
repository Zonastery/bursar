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
    IF p_amount IS NULL OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric OR p_amount < 0 THEN
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

    -- Floor enforcement: clamp v_net so the post-settle balance stays ≥ floor.
    -- strict / strict_prepaid → floor is p_min_balance (engine's min_balance).
    -- overdraft → floor is the overdraft_floor stored on the lease (can be negative).
    IF v_billing_mode IN ('strict', 'strict_prepaid') THEN
        v_settle_floor := COALESCE(p_min_balance, 0);
    ELSE
        v_settle_floor := COALESCE(v_overdraft_floor, 0);
    END IF;
    v_max_debit := GREATEST(0, v_balance - v_settle_floor);
    IF v_net > v_max_debit THEN
        v_net := v_max_debit;
        -- Re-clamp allowance consume so it doesn't exceed amount - net.
        IF v_net < p_amount THEN
            v_consume := LEAST(v_consume, p_amount - v_net);
        END IF;
    END IF;

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
            || jsonb_build_object('allowance_consumed', v_consume, 'balance_after', v_balance - v_net, 'bucket_breakdown', v_bucket_breakdown);

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
        'bucket_breakdown', v_bucket_breakdown
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
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(p_config->'subscriptions') k;

        IF v_config_keys IS NOT NULL THEN
            UPDATE bursar.billing_offers SET status = 'archived', updated_at = now()
            WHERE offer_key != ALL(v_config_keys) AND status = 'active';
        END IF;

        UPDATE bursar.billing_provider_refs SET active = false, updated_at = now()
        WHERE resource_type = 'offer';

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
                (v_item#>>'{grant,credits}')::INTEGER,
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
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(p_config->'topups') k;

        IF v_config_keys IS NOT NULL THEN
            UPDATE bursar.billing_credit_topups SET status = 'archived', updated_at = now()
            WHERE topup_key != ALL(v_config_keys) AND status = 'active';
        END IF;

        UPDATE bursar.billing_provider_refs SET active = false, updated_at = now()
        WHERE resource_type = 'topup';

        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'topups')
        LOOP
            INSERT INTO bursar.billing_credit_topups (
                topup_key, deposit_to, credits_per_unit,
                min_amount_minor, max_amount_minor, tax_behavior
            )
            VALUES (
                v_key,
                v_item->>'deposit_to',
                COALESCE((v_item->>'credits_per_unit')::INTEGER, 1000),
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
        SELECT array_agg(k) INTO v_config_keys
        FROM jsonb_object_keys(p_config #> '{ledger,buckets}') k;

        IF v_config_keys IS NOT NULL THEN
            UPDATE bursar.credit_buckets
            SET status = 'retired', updated_at = now()
            WHERE bucket_key != ALL(v_config_keys)
              AND config_version = v_version
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
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(p_config->'plans') k;

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
    WHERE provider = p_provider AND provider_customer_id = p_provider_customer_id;

    IF v_existing_user IS NOT NULL AND v_existing_user <> p_user_id THEN
        RETURN jsonb_build_object(
            'error', 'user_id_mismatch',
            'message', 'provider customer already mapped to a different user'
        );
    END IF;

    INSERT INTO bursar.billing_customers (provider, provider_customer_id, user_id, email)
    VALUES (p_provider, p_provider_customer_id, p_user_id, p_email)
    ON CONFLICT (provider, provider_customer_id) DO UPDATE SET
        email = COALESCE(EXCLUDED.email, billing_customers.email),
        updated_at = now();

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

CREATE FUNCTION bursar.upsert_billing_invoice(p_provider text, p_provider_invoice_id text, p_provider_subscription_id text, p_user_id uuid, p_status text, p_amount_paid_minor integer, p_amount_due_minor integer, p_currency text, p_period_start timestamp with time zone, p_period_end timestamp with time zone, p_metadata jsonb DEFAULT NULL::jsonb) RETURNS jsonb
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

CREATE FUNCTION bursar.upsert_billing_payment(p_provider text, p_provider_payment_id text, p_provider_invoice_id text, p_user_id uuid, p_amount_minor integer, p_tax_minor integer, p_currency text, p_purpose text, p_metadata jsonb DEFAULT NULL::jsonb) RETURNS jsonb
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

CREATE FUNCTION bursar.upsert_billing_refund(p_provider text, p_provider_refund_id text, p_provider_payment_id text, p_user_id uuid, p_amount_minor integer, p_currency text, p_reason text, p_metadata jsonb DEFAULT NULL::jsonb) RETURNS jsonb
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
      AND provider_subscription_id = p_state->>'provider_subscription_id';

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
        updated_at = now();

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
