-- Billing customer, subscription, and payment records.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.upsert_billing_customer(
    p_subject_id uuid,
    p_provider text,
    p_provider_customer_id text,
    p_email text DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_id uuid;

BEGIN
    IF p_provider IS NULL OR p_provider='' OR p_provider_customer_id IS NULL OR p_provider_customer_id='' THEN
        RAISE EXCEPTION 'invalid billing customer' USING ERRCODE='22023';

    END IF;

    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_customers(subject_id,provider,provider_customer_id,email)
    VALUES (p_subject_id,p_provider,p_provider_customer_id,p_email)
    ON CONFLICT (subject_id,provider) DO UPDATE
    SET provider_customer_id=EXCLUDED.provider_customer_id,email=EXCLUDED.email
    RETURNING id INTO v_id;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.upsert_billing_subscription(
    p_subject_id uuid,
    p_provider text,
    p_provider_subscription_id text,
    p_provider_customer_id text,
    p_offer_id uuid,
    p_status bursar.billing_subscription_status,
    p_current_period_start timestamptz DEFAULT NULL,
    p_current_period_end timestamptz DEFAULT NULL,
    p_cancel_at_period_end boolean DEFAULT false,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_id uuid;
 v_revision uuid;

BEGIN
    SELECT catalog_revision_id INTO v_revision
    FROM bursar.catalog_offers
    WHERE id=p_offer_id;

    IF NOT FOUND OR p_provider IS NULL OR p_provider='' OR p_provider_subscription_id IS NULL OR p_provider_subscription_id='' THEN
        RAISE EXCEPTION 'invalid billing subscription' USING ERRCODE='22023';

    END IF;

    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_subscriptions(
        subject_id,provider,provider_subscription_id,provider_customer_id,
        offer_id,catalog_revision_id,status,current_period_start,current_period_end,
        cancel_at_period_end,metadata
    )
    VALUES (
        p_subject_id,p_provider,p_provider_subscription_id,p_provider_customer_id,
        p_offer_id,v_revision,p_status,p_current_period_start,p_current_period_end,
        p_cancel_at_period_end,COALESCE(p_metadata,'{}'::jsonb)
    )
    ON CONFLICT (provider,provider_subscription_id) DO UPDATE
    SET provider_customer_id=EXCLUDED.provider_customer_id,
        offer_id=EXCLUDED.offer_id,
        catalog_revision_id=EXCLUDED.catalog_revision_id,
        status=EXCLUDED.status,
        current_period_start=EXCLUDED.current_period_start,
        current_period_end=EXCLUDED.current_period_end,
        cancel_at_period_end=EXCLUDED.cancel_at_period_end,
        metadata=EXCLUDED.metadata
    WHERE billing_subscriptions.subject_id=EXCLUDED.subject_id
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        RAISE EXCEPTION 'subscription identity conflict' USING ERRCODE='23505';

    END IF;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.upsert_billing_payment(
    p_subject_id uuid,
    p_provider text,
    p_provider_payment_id text,
    p_amount_minor bigint,
    p_tax_minor bigint,
    p_currency char(3),
    p_purpose text,
    p_status bursar.billing_payment_status
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_id uuid;

BEGIN
    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_payments(
        subject_id,provider,provider_payment_id,amount_minor,tax_minor,
        currency,purpose,status
    )
    VALUES (
        p_subject_id,p_provider,p_provider_payment_id,p_amount_minor,p_tax_minor,
        p_currency,p_purpose,p_status
    )
    ON CONFLICT (provider,provider_payment_id) DO UPDATE
    SET status=EXCLUDED.status,tax_minor=EXCLUDED.tax_minor
    WHERE billing_payments.subject_id=EXCLUDED.subject_id
      AND billing_payments.amount_minor=EXCLUDED.amount_minor
      AND billing_payments.currency=EXCLUDED.currency
      AND billing_payments.purpose=EXCLUDED.purpose
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        RAISE EXCEPTION 'payment identity conflict' USING ERRCODE='23505';

    END IF;

    RETURN v_id;

END $$;
