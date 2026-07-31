-- Billing preferences, invoices, disputes, and checkout.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.pseudonymize_financial_subject(
    p_subject_id uuid
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
    PERFORM 1
    FROM bursar.subjects
    WHERE id = p_subject_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    DELETE FROM bursar.external_identities
    WHERE subject_id = p_subject_id;

    UPDATE bursar.billing_customers
    SET email = NULL,
        metadata = '{}'::jsonb
    WHERE subject_id = p_subject_id;

    UPDATE bursar.billing_subscriptions
    SET metadata = '{}'::jsonb
    WHERE subject_id = p_subject_id;

    UPDATE bursar.billing_payments
    SET metadata = '{}'::jsonb
    WHERE subject_id = p_subject_id;

    UPDATE bursar.billing_refunds AS refund
    SET metadata = '{}'::jsonb
    FROM bursar.billing_payments AS payment
    WHERE payment.id = refund.payment_id
      AND payment.subject_id = p_subject_id;

    UPDATE bursar.billing_invoices
    SET metadata = '{}'::jsonb
    WHERE subject_id = p_subject_id;

    UPDATE bursar.billing_disputes
    SET metadata = '{}'::jsonb
    WHERE subject_id = p_subject_id;

    UPDATE bursar.billing_disputes AS dispute
    SET metadata = '{}'::jsonb
    FROM bursar.billing_payments AS payment
    WHERE payment.id = dispute.payment_id
      AND payment.subject_id = p_subject_id;

    UPDATE bursar.billing_auto_recharge_attempts
    SET metadata = '{}'::jsonb,
        failure_message = NULL
    WHERE subject_id = p_subject_id;

    UPDATE bursar.billing_checkout_intents
    SET checkout_url = NULL
    WHERE subject_id = p_subject_id;

    UPDATE bursar.billing_event_payloads AS payload
    SET envelope = jsonb_build_object('pseudonymized', true)
    FROM bursar.billing_events AS event
    WHERE event.id = payload.event_id
      AND event.payload_received_at = payload.received_at
      AND payload.envelope->>'userId' = p_subject_id::text;

    UPDATE bursar.subjects
    SET pseudonymized_at = COALESCE(pseudonymized_at, now())
    WHERE id = p_subject_id;

    RETURN true;
END $$;

CREATE FUNCTION bursar.upsert_billing_invoice(
    p_subject_id uuid,
    p_provider text,
    p_provider_invoice_id text,
    p_subscription_id uuid,
    p_status text,
    p_amount_due_minor bigint,
    p_amount_paid_minor bigint,
    p_currency text,
    p_period_start timestamptz DEFAULT NULL,
    p_period_end timestamptz DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_provider_updated_at timestamptz DEFAULT now()
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id uuid;
    v_environment text := bursar.current_provider_environment();
    v_existing bursar.billing_invoices;
    v_metadata jsonb;
BEGIN
    IF p_provider_updated_at IS NULL
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
    THEN
        RAISE EXCEPTION 'invalid invoice state' USING ERRCODE='22023';
    END IF;

    IF p_subscription_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM bursar.billing_subscriptions
        WHERE id=p_subscription_id AND subject_id=p_subject_id
    ) THEN
        RAISE EXCEPTION 'invoice subscription mismatch' USING ERRCODE='23514';

    END IF;

    INSERT INTO bursar.subjects(id)
    VALUES (p_subject_id)
    ON CONFLICT (tenant_id, id) DO NOTHING;
    v_metadata := CASE
        WHEN bursar.is_subject_pseudonymized(p_subject_id) THEN '{}'::jsonb
        ELSE COALESCE(p_metadata, '{}'::jsonb)
    END;

    INSERT INTO bursar.billing_invoices(
        subject_id,provider,provider_environment,provider_invoice_id,
        subscription_id,status,
        amount_due_minor,amount_paid_minor,currency,period_start,period_end,
        provider_updated_at,metadata
    )
    VALUES (
        p_subject_id,p_provider,v_environment,p_provider_invoice_id,
        p_subscription_id,p_status,
        p_amount_due_minor,p_amount_paid_minor,p_currency,p_period_start,p_period_end,
        p_provider_updated_at,v_metadata
    )
    ON CONFLICT (
        tenant_id,
        provider,
        provider_environment,
        provider_invoice_id
    ) DO UPDATE
    SET status=EXCLUDED.status,
        amount_due_minor=EXCLUDED.amount_due_minor,
        amount_paid_minor=EXCLUDED.amount_paid_minor,
        period_start=EXCLUDED.period_start,
        period_end=EXCLUDED.period_end,
        provider_updated_at=EXCLUDED.provider_updated_at,
        metadata=EXCLUDED.metadata
    WHERE billing_invoices.subject_id=EXCLUDED.subject_id
      AND billing_invoices.currency=EXCLUDED.currency
      AND billing_invoices.subscription_id IS NOT DISTINCT FROM
          EXCLUDED.subscription_id
      AND EXCLUDED.provider_updated_at > billing_invoices.provider_updated_at
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT *
        INTO v_existing
        FROM bursar.billing_invoices
        WHERE provider = p_provider
          AND provider_environment = v_environment
          AND provider_invoice_id = p_provider_invoice_id;

        IF NOT FOUND
           OR ROW(
               v_existing.subject_id,
               v_existing.subscription_id,
               v_existing.currency
           ) IS DISTINCT FROM ROW(
               p_subject_id,
               p_subscription_id,
               p_currency
           )
        THEN
            RAISE EXCEPTION 'invoice identity conflict' USING ERRCODE='23505';
        END IF;

        IF v_existing.provider_updated_at = p_provider_updated_at
           AND ROW(
               v_existing.status,
               v_existing.amount_due_minor,
               v_existing.amount_paid_minor,
               v_existing.period_start,
               v_existing.period_end,
               v_existing.metadata
           ) IS DISTINCT FROM ROW(
               p_status,
               p_amount_due_minor,
               p_amount_paid_minor,
               p_period_start,
               p_period_end,
               v_metadata
           )
        THEN
            RAISE EXCEPTION
                'conflicting invoice state at provider timestamp'
                USING ERRCODE='23514';
        END IF;

        v_id := v_existing.id;
    END IF;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.upsert_billing_dispute(
    p_provider text,
    p_provider_dispute_id text,
    p_payment_id uuid,
    p_status text,
    p_reason text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_provider_updated_at timestamptz DEFAULT now()
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id uuid;
    v_subject uuid;
    v_environment text := bursar.current_provider_environment();
    v_existing bursar.billing_disputes;
    v_metadata jsonb;
BEGIN
    IF p_provider_updated_at IS NULL
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
    THEN
        RAISE EXCEPTION 'invalid dispute state' USING ERRCODE='22023';
    END IF;

    SELECT subject_id INTO v_subject
    FROM bursar.billing_payments
    WHERE id=p_payment_id;

    IF NOT FOUND THEN RAISE EXCEPTION 'dispute payment missing' USING ERRCODE='23503';
 END IF;

    v_metadata := CASE
        WHEN bursar.is_subject_pseudonymized(v_subject) THEN '{}'::jsonb
        ELSE COALESCE(p_metadata, '{}'::jsonb)
    END;

    INSERT INTO bursar.billing_disputes(
        subject_id,provider,provider_environment,provider_dispute_id,
        payment_id,status,reason,provider_updated_at,metadata
    )
    VALUES (
        v_subject,p_provider,v_environment,p_provider_dispute_id,
        p_payment_id,p_status,p_reason,p_provider_updated_at,
        v_metadata
    )
    ON CONFLICT (
        tenant_id,
        provider,
        provider_environment,
        provider_dispute_id
    ) DO UPDATE
    SET status=EXCLUDED.status,
        reason=EXCLUDED.reason,
        provider_updated_at=EXCLUDED.provider_updated_at,
        metadata=EXCLUDED.metadata
    WHERE billing_disputes.payment_id=EXCLUDED.payment_id
      AND EXCLUDED.provider_updated_at > billing_disputes.provider_updated_at
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT *
        INTO v_existing
        FROM bursar.billing_disputes
        WHERE provider = p_provider
          AND provider_environment = v_environment
          AND provider_dispute_id = p_provider_dispute_id;

        IF NOT FOUND OR v_existing.payment_id <> p_payment_id THEN
            RAISE EXCEPTION 'dispute identity conflict' USING ERRCODE='23505';
        END IF;

        IF v_existing.provider_updated_at = p_provider_updated_at
           AND ROW(
               v_existing.status,
               v_existing.reason,
               v_existing.metadata
           ) IS DISTINCT FROM ROW(
               p_status,
               p_reason,
               v_metadata
           )
        THEN
            RAISE EXCEPTION
                'conflicting dispute state at provider timestamp'
                USING ERRCODE='23514';
        END IF;

        v_id := v_existing.id;
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
    p_checkout_url text DEFAULT NULL,
    p_region text DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_id uuid;
 v_revision uuid;
 v_environment text:=bursar.current_provider_environment();
 v_availability jsonb;
 v_object_type text;

BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_nonempty_text(p_provider)
       OR p_checkout_kind NOT IN ('subscription', 'credit_topup')
       OR NOT bursar.is_nonempty_text(p_product_key)
       OR octet_length(p_request_digest) <> 32
       OR p_expires_at <= now()
       OR (
           p_region IS NOT NULL
           AND upper(p_region) !~ '^[A-Z]{2,3}$'
       )
    THEN
        RAISE EXCEPTION 'invalid checkout intent'
            USING ERRCODE='22023';
    END IF;

    INSERT INTO bursar.subjects(id)
    VALUES (p_subject_id)
    ON CONFLICT (tenant_id, id) DO NOTHING;

    IF bursar.is_subject_pseudonymized(p_subject_id) THEN
        RAISE EXCEPTION 'pseudonymized subject cannot create checkout'
            USING ERRCODE = '55000';
    END IF;

    SELECT id INTO v_revision
    FROM bursar.catalog_revisions
    WHERE status='active';

    IF v_revision IS NULL THEN
        RAISE EXCEPTION 'active catalog missing' USING ERRCODE='23503';
    END IF;

    v_object_type := CASE p_checkout_kind
        WHEN 'subscription' THEN 'offer'
        ELSE 'topup'
    END;

    IF p_checkout_kind = 'subscription' THEN
        SELECT offer.availability
        INTO v_availability
        FROM bursar.catalog_offers AS offer
        WHERE offer.catalog_revision_id = v_revision
          AND offer.offer_key = p_product_key;
    ELSE
        SELECT topup.availability
        INTO v_availability
        FROM bursar.catalog_topups AS topup
        WHERE topup.catalog_revision_id = v_revision
          AND topup.topup_key = p_product_key;
    END IF;

    IF NOT FOUND
       OR NOT EXISTS (
           SELECT 1
           FROM bursar.catalog_provider_refs AS reference
           WHERE reference.catalog_revision_id = v_revision
             AND reference.provider = p_provider
             AND reference.provider_environment = v_environment
             AND reference.object_type = v_object_type
             AND reference.object_key = p_product_key
       )
    THEN
        RAISE EXCEPTION 'checkout product is not available from provider'
            USING ERRCODE='22023';
    END IF;

    IF (
        v_availability->>'starts_at' IS NOT NULL
        AND (v_availability->>'starts_at')::timestamptz > now()
    ) OR (
        v_availability->>'ends_at' IS NOT NULL
        AND (v_availability->>'ends_at')::timestamptz <= now()
    ) OR (
        jsonb_array_length(
            COALESCE(v_availability->'regions', '[]'::jsonb)
        ) > 0
        AND (
            p_region IS NULL
            OR NOT (v_availability->'regions' ? upper(p_region))
        )
    )
    THEN
        RAISE EXCEPTION 'checkout product is outside its availability'
            USING ERRCODE='22023';
    END IF;

    INSERT INTO bursar.billing_checkout_intents AS intent(
        subject_id,provider,provider_environment,checkout_kind,product_key,
        region,catalog_revision_id,request_digest,expires_at,provider_session_id,
        checkout_url
    )
    VALUES (
        p_subject_id,p_provider,v_environment,p_checkout_kind,p_product_key,
        upper(p_region),v_revision,p_request_digest,p_expires_at,p_provider_session_id,
        p_checkout_url
    )
    ON CONFLICT (
        tenant_id,subject_id,provider,provider_environment,checkout_kind,product_key,
        catalog_revision_id,request_digest
    )
    DO UPDATE SET
        -- A caller may deliberately expire an abandoned checkout before its
        -- natural expiry. Reopen it without a provider session so the next
        -- request creates a fresh provider session. Completed and failed
        -- intents remain terminal, preserving webhook idempotency.
        status = CASE
            WHEN intent.status = 'open'
                 AND intent.expires_at <= now() THEN 'open'
            WHEN intent.status = 'expired' THEN 'open'
            ELSE intent.status
        END,
        expires_at = CASE
            WHEN intent.status = 'open' AND intent.expires_at <= now() THEN EXCLUDED.expires_at
            WHEN intent.status = 'expired' THEN EXCLUDED.expires_at
            ELSE intent.expires_at
        END,
        provider_session_id = CASE
            WHEN intent.status = 'open' AND intent.expires_at <= now() THEN NULL
            WHEN intent.status = 'expired' THEN NULL
            ELSE intent.provider_session_id
        END,
        checkout_url = CASE
            WHEN intent.status = 'open' AND intent.expires_at <= now() THEN NULL
            WHEN intent.status = 'expired' THEN NULL
            ELSE intent.checkout_url
        END,
        updated_at=now()
    RETURNING id INTO v_id;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.advance_checkout_intent(
    p_intent_id uuid,
    p_status text DEFAULT NULL,
    p_provider_session_id text DEFAULT NULL,
    p_checkout_url text DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_intent bursar.billing_checkout_intents;
BEGIN
    IF p_status IS NOT NULL
       AND p_status NOT IN ('open', 'completed', 'failed', 'expired')
    THEN
        RETURN false;
    END IF;

    SELECT *
    INTO v_intent
    FROM bursar.billing_checkout_intents
    WHERE id = p_intent_id
      AND provider_environment = bursar.current_provider_environment()
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF v_intent.status <> 'open'
       AND p_status IS NOT NULL
       AND p_status <> v_intent.status
    THEN
        RETURN false;
    END IF;

    UPDATE bursar.billing_checkout_intents
    SET status = COALESCE(p_status, status),
        provider_session_id = COALESCE(
            CASE
                WHEN bursar.is_subject_pseudonymized(v_intent.subject_id) THEN NULL
                ELSE p_provider_session_id
            END,
            provider_session_id
        ),
        checkout_url = CASE
            WHEN bursar.is_subject_pseudonymized(v_intent.subject_id) THEN NULL
            ELSE COALESCE(p_checkout_url, checkout_url)
        END,
        updated_at = now()
    WHERE id = p_intent_id;

    RETURN true;
END
$$;
