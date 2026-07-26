






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






SET default_tablespace = '';

SET default_table_access_method = heap;
