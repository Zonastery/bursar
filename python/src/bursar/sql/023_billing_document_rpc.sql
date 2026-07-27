-- Billing preferences, invoices, disputes, and checkout.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.upsert_billing_invoice(
    p_subject_id uuid,
    p_provider text,
    p_provider_invoice_id text,
    p_subscription_id uuid,
    p_status text,
    p_amount_due_minor bigint,
    p_amount_paid_minor bigint,
    p_currency char(3),
    p_period_start timestamptz DEFAULT NULL,
    p_period_end timestamptz DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_id uuid;

BEGIN
    IF p_subscription_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM bursar.billing_subscriptions
        WHERE id=p_subscription_id AND subject_id=p_subject_id
    ) THEN
        RAISE EXCEPTION 'invoice subscription mismatch' USING ERRCODE='23514';

    END IF;

    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_invoices(
        subject_id,provider,provider_invoice_id,subscription_id,status,
        amount_due_minor,amount_paid_minor,currency,period_start,period_end,metadata
    )
    VALUES (
        p_subject_id,p_provider,p_provider_invoice_id,p_subscription_id,p_status,
        p_amount_due_minor,p_amount_paid_minor,p_currency,p_period_start,p_period_end,
        COALESCE(p_metadata,'{}'::jsonb)
    )
    ON CONFLICT (provider,provider_invoice_id) DO UPDATE
    SET status=EXCLUDED.status,
        amount_due_minor=EXCLUDED.amount_due_minor,
        amount_paid_minor=EXCLUDED.amount_paid_minor,
        period_start=EXCLUDED.period_start,
        period_end=EXCLUDED.period_end,
        metadata=EXCLUDED.metadata
    WHERE billing_invoices.subject_id=EXCLUDED.subject_id
      AND billing_invoices.currency=EXCLUDED.currency
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN RAISE EXCEPTION 'invoice identity conflict' USING ERRCODE='23505';
 END IF;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.upsert_billing_dispute(
    p_provider text,
    p_provider_dispute_id text,
    p_payment_id uuid,
    p_status text,
    p_reason text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_id uuid;
 v_subject uuid;

BEGIN
    SELECT subject_id INTO v_subject
    FROM bursar.billing_payments
    WHERE id=p_payment_id;

    IF NOT FOUND THEN RAISE EXCEPTION 'dispute payment missing' USING ERRCODE='23503';
 END IF;

    INSERT INTO bursar.billing_disputes(
        subject_id,provider,provider_dispute_id,payment_id,status,reason,metadata
    )
    VALUES (
        v_subject,p_provider,p_provider_dispute_id,p_payment_id,p_status,p_reason,
        COALESCE(p_metadata,'{}'::jsonb)
    )
    ON CONFLICT (provider,provider_dispute_id) DO UPDATE
    SET status=EXCLUDED.status,reason=EXCLUDED.reason,metadata=EXCLUDED.metadata
    WHERE billing_disputes.payment_id=EXCLUDED.payment_id
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN RAISE EXCEPTION 'dispute identity conflict' USING ERRCODE='23505';
 END IF;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.create_checkout_intent(
    p_subject_id uuid,
    p_provider text,
    p_checkout_kind text,
    p_product_key text,
    p_request_digest bytea,
    p_expires_at timestamptz,
    p_provider_session_id text DEFAULT NULL,
    p_checkout_url text DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_id uuid;

BEGIN
    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_checkout_intents(
        subject_id,provider,checkout_kind,product_key,request_digest,expires_at,
        provider_session_id,checkout_url
    )
    VALUES (
        p_subject_id,p_provider,p_checkout_kind,p_product_key,p_request_digest,
        p_expires_at,p_provider_session_id,p_checkout_url
    )
    ON CONFLICT (subject_id,provider,checkout_kind,product_key,request_digest)
    DO UPDATE SET updated_at=now()
    RETURNING id INTO v_id;

    RETURN v_id;

END $$;
