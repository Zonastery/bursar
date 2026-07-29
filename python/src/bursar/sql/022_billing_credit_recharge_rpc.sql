-- Billing credit, refund, and recharge records.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.create_billing_credit_grant(
    p_payment_id uuid,
    p_subscription_id uuid,
    p_topup_id uuid,
    p_configured_credits numeric,
    p_quantity integer,
    p_billing_event_id uuid DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id uuid;

    v_subject uuid;

    v_revision uuid;

    v_existing bursar.billing_credit_grants;
    v_expected_credits numeric;
    v_quantity_min integer;
    v_quantity_max integer;

BEGIN
    IF (
           p_topup_id IS NOT NULL
           AND (
               p_payment_id IS NULL
               OR p_subscription_id IS NOT NULL
           )
       )
       OR (
           p_topup_id IS NULL
           AND p_subscription_id IS NULL
       )
       OR NOT bursar.is_finite_numeric(p_configured_credits)
       OR p_configured_credits <= 0
       OR p_quantity <= 0
    THEN
        RAISE EXCEPTION 'invalid billing credit grant' USING ERRCODE='22023';

    END IF;

    IF p_topup_id IS NOT NULL THEN
        SELECT
            p.subject_id,
            t.catalog_revision_id,
            t.credits_per_unit,
            t.min_quantity,
            t.max_quantity
        INTO
            v_subject,
            v_revision,
            v_expected_credits,
            v_quantity_min,
            v_quantity_max
        FROM bursar.billing_payments p
        JOIN bursar.catalog_topups t ON t.id=p_topup_id
        WHERE p.id = p_payment_id
          AND p.purpose = 'credit_topup'
          AND p.status = 'succeeded'
          AND p.currency = t.currency
          AND p.amount_minor::numeric =
              t.amount_minor::numeric * p_quantity;

        IF NOT FOUND
           OR p_configured_credits <> v_expected_credits
           OR p_quantity NOT BETWEEN v_quantity_min AND v_quantity_max
        THEN
            RAISE EXCEPTION 'invalid payment grant' USING ERRCODE='22023';
        END IF;

        SELECT * INTO v_existing
        FROM bursar.billing_credit_grants
        WHERE payment_id=p_payment_id AND topup_id=p_topup_id
        FOR UPDATE;

    ELSE
        SELECT
            subscription.subject_id,
            subscription.catalog_revision_id,
            offer.cycle_grant_amount
        INTO v_subject, v_revision, v_expected_credits
        FROM bursar.billing_subscriptions AS subscription
        JOIN bursar.catalog_offers AS offer
          ON offer.id = subscription.offer_id
         AND offer.catalog_revision_id =
             subscription.catalog_revision_id
        JOIN bursar.billing_events AS billing_event
          ON billing_event.id = p_billing_event_id
         AND billing_event.provider = subscription.provider
         AND billing_event.provider_environment =
             subscription.provider_environment
         AND billing_event.status = 'processing'
        WHERE subscription.id = p_subscription_id
          AND subscription.status IN (
              'trialing', 'active', 'past_due'
          );

        IF NOT FOUND
           OR p_topup_id IS NOT NULL
           OR p_billing_event_id IS NULL
           OR v_expected_credits IS NULL
           OR p_configured_credits <> v_expected_credits
           OR p_quantity <> 1
        THEN
            RAISE EXCEPTION 'invalid subscription grant' USING ERRCODE='22023';

        END IF;

        IF p_payment_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
               FROM bursar.billing_payments AS payment
               JOIN bursar.billing_subscriptions AS subscription
                 ON subscription.id = p_subscription_id
                AND subscription.subject_id = payment.subject_id
                AND subscription.provider = payment.provider
                AND subscription.provider_environment =
                    payment.provider_environment
               WHERE payment.id = p_payment_id
                 AND payment.purpose = 'subscription'
                 AND payment.status = 'succeeded'
           )
        THEN
            RAISE EXCEPTION 'invalid subscription payment grant'
                USING ERRCODE='22023';
        END IF;

        SELECT * INTO v_existing
        FROM bursar.billing_credit_grants
        WHERE billing_event_id=p_billing_event_id
        FOR UPDATE;

    END IF;

    IF FOUND THEN
        IF v_existing.configured_credits<>p_configured_credits
           OR v_existing.quantity<>p_quantity
           OR v_existing.subject_id<>v_subject
        THEN
            RAISE EXCEPTION 'billing grant identity conflict' USING ERRCODE='23505';

        END IF;

        RETURN v_existing.id;

    END IF;

    INSERT INTO bursar.billing_credit_grants(
        payment_id,subject_id,topup_id,subscription_id,catalog_revision_id,
        configured_credits,quantity,billing_event_id
    )
    VALUES (
        p_payment_id,v_subject,p_topup_id,p_subscription_id,v_revision,
        p_configured_credits,p_quantity,p_billing_event_id
    )
    RETURNING id INTO v_id;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.upsert_billing_refund(
    p_payment_id uuid,
    p_provider_refund_id text,
    p_amount_minor bigint,
    p_status text DEFAULT 'pending',
    p_reason text DEFAULT NULL,
    p_provider_updated_at timestamptz DEFAULT now(),
    p_expected_subject_id uuid DEFAULT NULL,
    p_expected_currency char(3) DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id uuid;

    v_provider text;

    v_currency char(3);
    v_subject_id uuid;
    v_environment text;
    v_existing bursar.billing_refunds;
    v_metadata jsonb;

BEGIN
    IF p_status NOT IN ('pending', 'succeeded', 'failed', 'canceled')
       OR p_provider_updated_at IS NULL
       OR p_metadata IS NULL
       OR jsonb_typeof(p_metadata) <> 'object'
    THEN
        RAISE EXCEPTION 'invalid refund state' USING ERRCODE='22023';
    END IF;

    SELECT provider,provider_environment,currency,subject_id
    INTO v_provider,v_environment,v_currency,v_subject_id
    FROM bursar.billing_payments
    WHERE id=p_payment_id;

    IF NOT FOUND THEN RAISE EXCEPTION 'payment missing' USING ERRCODE='23503';
    END IF;

    IF p_expected_subject_id IS NOT NULL
       AND p_expected_subject_id <> v_subject_id
    THEN
        RAISE EXCEPTION 'refund subject does not match payment'
            USING ERRCODE='23514';
    END IF;

    IF p_expected_currency IS NOT NULL
       AND upper(p_expected_currency::text)::char(3) <> v_currency
    THEN
        RAISE EXCEPTION 'refund currency does not match payment'
            USING ERRCODE='23514';
    END IF;

    v_metadata := CASE
        WHEN bursar.is_subject_pseudonymized(v_subject_id) THEN '{}'::jsonb
        ELSE p_metadata
    END;

    INSERT INTO bursar.billing_refunds(
        payment_id,provider,provider_environment,provider_refund_id,
        amount_minor,currency,status,reason,metadata,provider_updated_at
    )
    VALUES (
        p_payment_id,v_provider,v_environment,p_provider_refund_id,
        p_amount_minor,v_currency,p_status,p_reason,v_metadata,p_provider_updated_at
    )
    ON CONFLICT (provider,provider_environment,provider_refund_id) DO UPDATE
    SET status=EXCLUDED.status,
        reason=EXCLUDED.reason,
        metadata=EXCLUDED.metadata,
        provider_updated_at=EXCLUDED.provider_updated_at
    WHERE billing_refunds.payment_id=EXCLUDED.payment_id
      AND billing_refunds.currency=EXCLUDED.currency
      AND billing_refunds.amount_minor=EXCLUDED.amount_minor
      AND EXCLUDED.provider_updated_at > billing_refunds.provider_updated_at
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT *
        INTO v_existing
        FROM bursar.billing_refunds
        WHERE provider = v_provider
          AND provider_environment = v_environment
          AND provider_refund_id = p_provider_refund_id;

        IF NOT FOUND
           OR ROW(
               v_existing.payment_id,
               v_existing.amount_minor,
               v_existing.currency
           ) IS DISTINCT FROM ROW(
               p_payment_id,
               p_amount_minor,
               v_currency
           )
        THEN
            RAISE EXCEPTION 'refund identity conflict' USING ERRCODE='23505';
        END IF;

        IF v_existing.provider_updated_at = p_provider_updated_at
           AND ROW(v_existing.status, v_existing.reason, v_existing.metadata)
               IS DISTINCT FROM ROW(p_status, p_reason, v_metadata)
        THEN
            RAISE EXCEPTION
                'conflicting refund state at provider timestamp'
                USING ERRCODE='23514';
        END IF;

        v_id := v_existing.id;
    END IF;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.upsert_auto_recharge_profile(
    p_subject_id uuid,
    p_enabled boolean,
    p_provider text,
    p_topup_id uuid,
    p_quantity integer,
    p_threshold numeric,
    p_max_charges_per_window integer,
    p_window_unit text,
    p_window_count integer,
    p_window_anchor text,
    p_window_timezone text
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_revision uuid;

    v_topup_key text;
    v_amount_minor bigint;

    v_policy record;
    v_environment text:=bursar.current_provider_environment();

BEGIN
    IF p_enabled THEN
        SELECT t.catalog_revision_id,t.topup_key,t.amount_minor
        INTO v_revision,v_topup_key,v_amount_minor
        FROM bursar.catalog_topups t
        JOIN bursar.catalog_revisions r ON r.id=t.catalog_revision_id AND r.status='active'
        WHERE t.id=p_topup_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'auto-recharge top-up is not active' USING ERRCODE='22023';

        END IF;

        SELECT policy.* INTO v_policy
        FROM bursar.catalog_auto_recharge_policies AS policy
        WHERE policy.catalog_revision_id = v_revision;

        IF NOT FOUND
           OR NOT (v_topup_key=ANY(v_policy.eligible_topup_keys))
           OR p_quantity NOT BETWEEN v_policy.quantity_min AND v_policy.quantity_max
           OR p_threshold NOT BETWEEN v_policy.balance_min AND v_policy.balance_max
           OR p_max_charges_per_window <> v_policy.max_purchases
           OR p_window_unit <> v_policy.period_unit
           OR p_window_count <> v_policy.period_count
           OR p_window_anchor <> v_policy.period_anchor
           OR p_window_timezone <> v_policy.period_timezone
           OR (
               v_policy.max_charge_minor IS NOT NULL
               AND v_amount_minor::numeric * p_quantity
                    > v_policy.max_charge_minor
           )
           OR NOT EXISTS (
               SELECT 1
               FROM bursar.catalog_provider_refs AS provider_ref
               WHERE provider_ref.catalog_revision_id = v_revision
                 AND provider_ref.provider_environment = v_environment
                 AND provider_ref.provider = p_provider
                 AND provider_ref.object_type = 'topup'
                 AND provider_ref.object_key = v_topup_key
           )
        THEN
            RAISE EXCEPTION 'auto-recharge profile does not match active catalog policy' USING ERRCODE='22023';

        END IF;

    END IF;

    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_auto_recharge_profiles(
        subject_id,enabled,armed,state,provider,provider_environment,
        catalog_revision_id,topup_id,quantity,threshold,rearm_above,
        max_charges_per_window,max_charge_minor,cooldown_seconds,
        max_consecutive_failures,window_unit,window_count,window_anchor,
        window_timezone
    )
    VALUES (
        p_subject_id,p_enabled,true,
        CASE WHEN p_enabled THEN 'active' ELSE 'disabled' END,
        p_provider,v_environment,v_revision,p_topup_id,p_quantity,p_threshold,
        CASE WHEN p_enabled THEN v_policy.rearm_above ELSE p_threshold+1 END,
        p_max_charges_per_window,
        CASE WHEN p_enabled THEN v_policy.max_charge_minor END,
        CASE WHEN p_enabled THEN v_policy.cooldown_seconds ELSE 0 END,
        CASE WHEN p_enabled THEN v_policy.max_consecutive_failures ELSE 3 END,
        p_window_unit,p_window_count,p_window_anchor,p_window_timezone
    )
    ON CONFLICT (subject_id,provider_environment) DO UPDATE
    SET enabled=EXCLUDED.enabled,
        armed=CASE WHEN NOT billing_auto_recharge_profiles.enabled AND EXCLUDED.enabled
                   THEN true ELSE billing_auto_recharge_profiles.armed END,
        provider=EXCLUDED.provider,
        provider_environment=EXCLUDED.provider_environment,
        catalog_revision_id=EXCLUDED.catalog_revision_id,
        topup_id=EXCLUDED.topup_id,
        quantity=EXCLUDED.quantity,
        threshold=EXCLUDED.threshold,
        rearm_above=EXCLUDED.rearm_above,
        max_charges_per_window=EXCLUDED.max_charges_per_window,
        max_charge_minor=EXCLUDED.max_charge_minor,
        cooldown_seconds=EXCLUDED.cooldown_seconds,
        max_consecutive_failures=EXCLUDED.max_consecutive_failures,
        window_unit=EXCLUDED.window_unit,
        window_count=EXCLUDED.window_count,
        window_anchor=EXCLUDED.window_anchor,
        window_timezone=EXCLUDED.window_timezone,
        state=EXCLUDED.state;

    RETURN true;

END $$;

CREATE FUNCTION bursar.upsert_billing_preferences(
    p_subject_id uuid,
    p_auto_recharge boolean,
    p_overage_protection boolean,
    p_email_notifications boolean,
    p_usage_alerts boolean,
    p_invoice_reminders boolean
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_preferences(
        subject_id,auto_recharge,overage_protection,email_notifications,
        usage_alerts,invoice_reminders
    )
    VALUES (
        p_subject_id,p_auto_recharge,p_overage_protection,p_email_notifications,
        p_usage_alerts,p_invoice_reminders
    )
    ON CONFLICT (subject_id) DO UPDATE
    SET auto_recharge=EXCLUDED.auto_recharge,
        overage_protection=EXCLUDED.overage_protection,
        email_notifications=EXCLUDED.email_notifications,
        usage_alerts=EXCLUDED.usage_alerts,
        invoice_reminders=EXCLUDED.invoice_reminders;

    RETURN true;

END $$;
