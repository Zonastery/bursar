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

  v_grant := v_config #> '{ledger,signup_grant}';

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
    IF p_amount <= 0 THEN
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
  ON CONFLICT (account_id, entry_type, idempotency_key) DO NOTHING;
  IF FOUND THEN
    UPDATE bursar.credit_accounts SET balance = balance + NEW.amount, updated_at = now() WHERE id = account.id;
  END IF;
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
    SET status = 'processing', updated_at = now(), retry_count = v_existing.retry_count + 1
    WHERE id = v_existing.id;

    RETURN jsonb_build_object('status', 'reclaimed', 'event_id', v_existing.id);
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
    WHERE provider = p_provider AND lookup_key = p_lookup_key
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
        WHERE provider = p_provider AND price_id = p_price_id
          AND resource_type = 'offer' AND active = true
        LIMIT 1;
    ELSIF p_product_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM bursar.billing_provider_refs
        WHERE provider = p_provider AND product_id = p_product_id
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
    WHERE provider = p_provider AND lookup_key = p_lookup_key
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
        WHERE provider = p_provider AND price_id = p_price_id
          AND resource_type = 'topup' AND active = true
        LIMIT 1;
    ELSIF p_product_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM bursar.billing_provider_refs
        WHERE provider = p_provider AND product_id = p_product_id
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
    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM bursar.bursar_config;

    UPDATE bursar.bursar_config SET active = false WHERE active = true;

    INSERT INTO bursar.bursar_config (config, active, version, label)
    VALUES (p_config, true, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    PERFORM bursar.sync_plans_from_config(p_config, v_next_version);
    PERFORM bursar.sync_buckets_from_config(p_config, v_next_version);
    PERFORM bursar.sync_billing_from_config(p_config->'billing');

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true
    );
END;
$$;


--
