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

BEGIN
    IF num_nonnulls(p_payment_id,p_subscription_id)<>1
       OR p_configured_credits<=0 OR p_quantity<=0
    THEN
        RAISE EXCEPTION 'invalid billing credit grant' USING ERRCODE='22023';

    END IF;

    IF p_payment_id IS NOT NULL THEN
        SELECT p.subject_id,t.catalog_revision_id
        INTO v_subject,v_revision
        FROM bursar.billing_payments p
        JOIN bursar.catalog_topups t ON t.id=p_topup_id
        WHERE p.id=p_payment_id AND p.purpose='credit_topup';

        IF NOT FOUND THEN RAISE EXCEPTION 'invalid payment grant' USING ERRCODE='22023';
 END IF;

        SELECT * INTO v_existing
        FROM bursar.billing_credit_grants
        WHERE payment_id=p_payment_id AND topup_id=p_topup_id
        FOR UPDATE;

    ELSE
        SELECT subject_id,catalog_revision_id INTO v_subject,v_revision
        FROM bursar.billing_subscriptions
        WHERE id=p_subscription_id;

        IF NOT FOUND OR p_topup_id IS NOT NULL OR p_billing_event_id IS NULL THEN
            RAISE EXCEPTION 'invalid subscription grant' USING ERRCODE='22023';

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
    p_amount_minor bigint
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id uuid;

    v_provider text;

    v_currency char(3);

BEGIN
    SELECT provider,currency INTO v_provider,v_currency
    FROM bursar.billing_payments
    WHERE id=p_payment_id;

    IF NOT FOUND THEN RAISE EXCEPTION 'payment missing' USING ERRCODE='23503';
 END IF;

    INSERT INTO bursar.billing_refunds(
        payment_id,provider,provider_refund_id,amount_minor,currency
    )
    VALUES (
        p_payment_id,v_provider,p_provider_refund_id,p_amount_minor,v_currency
 )
 ON CONFLICT (provider,provider_refund_id) DO UPDATE
 SET provider_refund_id=EXCLUDED.provider_refund_id
 WHERE billing_refunds.payment_id=EXCLUDED.payment_id
 AND billing_refunds.currency=EXCLUDED.currency
 AND billing_refunds.amount_minor=EXCLUDED.amount_minor
 RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        RAISE EXCEPTION 'refund identity conflict' USING ERRCODE='23505';

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

    v_policy record;

BEGIN
    IF p_enabled THEN
        SELECT t.catalog_revision_id,t.topup_key INTO v_revision,v_topup_key
        FROM bursar.catalog_topups t
        JOIN bursar.catalog_revisions r ON r.id=t.catalog_revision_id AND r.status='active'
        WHERE t.id=p_topup_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'auto-recharge top-up is not active' USING ERRCODE='22023';

        END IF;

        SELECT * INTO v_policy
        FROM bursar.catalog_auto_recharge_policies
        WHERE catalog_revision_id=v_revision AND topup_key=v_topup_key;

        IF NOT FOUND
           OR p_quantity <> v_policy.quantity
           OR p_threshold <> v_policy.balance_below
           OR p_max_charges_per_window <> v_policy.max_purchases
           OR p_window_unit <> v_policy.period_unit
           OR p_window_count <> v_policy.period_count
           OR p_window_anchor <> v_policy.period_anchor
           OR p_window_timezone <> v_policy.period_timezone
        THEN
            RAISE EXCEPTION 'auto-recharge profile does not match active catalog policy' USING ERRCODE='22023';

        END IF;

    END IF;

    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_auto_recharge_profiles(
        subject_id,enabled,armed,state,provider,topup_id,quantity,threshold,
        max_charges_per_window,window_unit,window_count,window_anchor,window_timezone
    )
    VALUES (
        p_subject_id,p_enabled,true,'active',p_provider,p_topup_id,p_quantity,p_threshold,
        p_max_charges_per_window,p_window_unit,p_window_count,p_window_anchor,p_window_timezone
    )
    ON CONFLICT (subject_id) DO UPDATE
    SET enabled=EXCLUDED.enabled,
        armed=CASE WHEN NOT billing_auto_recharge_profiles.enabled AND EXCLUDED.enabled
                   THEN true ELSE billing_auto_recharge_profiles.armed END,
        provider=EXCLUDED.provider,
        topup_id=EXCLUDED.topup_id,
        quantity=EXCLUDED.quantity,
        threshold=EXCLUDED.threshold,
        max_charges_per_window=EXCLUDED.max_charges_per_window,
        window_unit=EXCLUDED.window_unit,
        window_count=EXCLUDED.window_count,
        window_anchor=EXCLUDED.window_anchor,
        window_timezone=EXCLUDED.window_timezone;

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
