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
 v_environment text:=bursar.current_provider_environment();

BEGIN
    IF p_provider IS NULL OR p_provider='' OR p_provider_customer_id IS NULL OR p_provider_customer_id='' THEN
        RAISE EXCEPTION 'invalid billing customer' USING ERRCODE='22023';

    END IF;

    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;

    INSERT INTO bursar.billing_customers(
        subject_id,provider,provider_environment,provider_customer_id,email
    )
    VALUES (
        p_subject_id,p_provider,v_environment,p_provider_customer_id,
        CASE
            WHEN bursar.is_subject_pseudonymized(p_subject_id) THEN NULL
            ELSE p_email
        END
    )
    ON CONFLICT (subject_id,provider,provider_environment) DO UPDATE
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
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_trial_end timestamptz DEFAULT NULL,
    p_cancel_at timestamptz DEFAULT NULL,
    p_ended_at timestamptz DEFAULT NULL,
    p_provider_updated_at timestamptz DEFAULT now(),
    p_grace_ends_at timestamptz DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id uuid;
    v_revision uuid;
    v_environment text := bursar.current_provider_environment();
    v_effective_ended_at timestamptz;
    v_existing bursar.billing_subscriptions;
    v_expiry timestamptz;
    v_lot record;
    v_metadata jsonb;
BEGIN
    SELECT catalog_revision_id INTO v_revision
    FROM bursar.catalog_offers
    WHERE id=p_offer_id;

    IF NOT FOUND
       OR NOT bursar.is_nonempty_text(p_provider)
       OR NOT bursar.is_nonempty_text(p_provider_subscription_id)
       OR p_provider_updated_at IS NULL
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
       OR (p_status <> 'past_due' AND p_grace_ends_at IS NOT NULL)
    THEN
        RAISE EXCEPTION 'invalid billing subscription' USING ERRCODE='22023';
    END IF;

    v_effective_ended_at := COALESCE(
        p_ended_at,
        CASE
            WHEN p_status IN (
                'incomplete_expired',
                'canceled',
                'expired'
            )
            THEN p_provider_updated_at
        END
    );

    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;
    v_metadata := CASE
        WHEN bursar.is_subject_pseudonymized(p_subject_id) THEN '{}'::jsonb
        ELSE COALESCE(p_metadata, '{}'::jsonb)
    END;

    -- Serialize subscription expiry changes with account debits and grants.
    PERFORM 1
    FROM bursar.credit_accounts
    WHERE subject_id = p_subject_id
      AND account_kind = 'personal'
    FOR UPDATE;

    INSERT INTO bursar.billing_subscriptions(
        subject_id,provider,provider_subscription_id,provider_customer_id,
        provider_environment,offer_id,catalog_revision_id,status,
        current_period_start,current_period_end,trial_end,cancel_at,
        cancel_at_period_end,ended_at,grace_ends_at,provider_updated_at,
        status_changed_at,metadata
    )
    VALUES (
        p_subject_id,p_provider,p_provider_subscription_id,p_provider_customer_id,
        v_environment,p_offer_id,v_revision,p_status,p_current_period_start,p_current_period_end,
        p_trial_end,p_cancel_at,p_cancel_at_period_end,v_effective_ended_at,
        p_grace_ends_at,p_provider_updated_at,p_provider_updated_at,
        v_metadata
    )
    ON CONFLICT (provider,provider_environment,provider_subscription_id) DO UPDATE
    SET provider_customer_id=EXCLUDED.provider_customer_id,
        offer_id=EXCLUDED.offer_id,
        catalog_revision_id=EXCLUDED.catalog_revision_id,
        status=EXCLUDED.status,
        current_period_start=EXCLUDED.current_period_start,
        current_period_end=EXCLUDED.current_period_end,
        trial_end=EXCLUDED.trial_end,
        cancel_at=EXCLUDED.cancel_at,
        cancel_at_period_end=EXCLUDED.cancel_at_period_end,
        ended_at=EXCLUDED.ended_at,
        grace_expired_at=CASE
            WHEN EXCLUDED.status = 'past_due'
             AND COALESCE(
                 EXCLUDED.grace_ends_at,
                 billing_subscriptions.grace_ends_at
             ) IS NOT DISTINCT FROM billing_subscriptions.grace_ends_at
            THEN billing_subscriptions.grace_expired_at
            ELSE NULL
        END,
        grace_ends_at=CASE
            WHEN EXCLUDED.status = 'past_due'
            THEN COALESCE(
                EXCLUDED.grace_ends_at,
                billing_subscriptions.grace_ends_at
            )
            ELSE NULL
        END,
        provider_updated_at=EXCLUDED.provider_updated_at,
        metadata=EXCLUDED.metadata,
        status_changed_at=CASE
            WHEN billing_subscriptions.status IS DISTINCT FROM EXCLUDED.status
                THEN EXCLUDED.provider_updated_at
            ELSE billing_subscriptions.status_changed_at
        END
    WHERE billing_subscriptions.subject_id=EXCLUDED.subject_id
      AND EXCLUDED.provider_updated_at
            > billing_subscriptions.provider_updated_at
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT *
        INTO v_existing
        FROM bursar.billing_subscriptions
        WHERE provider = p_provider
          AND provider_environment = v_environment
          AND provider_subscription_id = p_provider_subscription_id;

        IF NOT FOUND
           OR v_existing.subject_id <> p_subject_id
        THEN
            RAISE EXCEPTION 'subscription identity conflict'
                USING ERRCODE='23505';
        END IF;

        IF v_existing.provider_updated_at = p_provider_updated_at
           AND ROW(
               v_existing.provider_customer_id,
               v_existing.offer_id,
               v_existing.status,
               v_existing.current_period_start,
               v_existing.current_period_end,
               v_existing.trial_end,
               v_existing.cancel_at,
               v_existing.cancel_at_period_end,
               v_existing.ended_at,
               v_existing.grace_ends_at,
               v_existing.metadata
           ) IS DISTINCT FROM ROW(
               p_provider_customer_id,
               p_offer_id,
               p_status,
               p_current_period_start,
               p_current_period_end,
               p_trial_end,
               p_cancel_at,
               p_cancel_at_period_end,
               v_effective_ended_at,
               CASE
                   WHEN p_status = 'past_due'
                   THEN COALESCE(p_grace_ends_at, v_existing.grace_ends_at)
               END,
               v_metadata
           )
        THEN
            RAISE EXCEPTION
                'conflicting subscription state at provider timestamp'
                USING ERRCODE='23514';
        END IF;

        v_id := v_existing.id;
    END IF;

    SELECT *
    INTO v_existing
    FROM bursar.billing_subscriptions
    WHERE id = v_id;

    v_expiry := COALESCE(
        v_existing.ended_at,
        v_existing.cancel_at,
        CASE
            WHEN v_existing.cancel_at_period_end
            THEN v_existing.current_period_end
        END
    );

    PERFORM set_config('bursar.mutation_context', 'internal', true);

    UPDATE bursar.credit_lots AS lot
    SET expires_at = v_expiry
    FROM bursar.billing_credit_grants AS credit_grant
    WHERE credit_grant.subscription_id = v_id
      AND lot.source_type = 'subscription_cycle'
      AND lot.source_id = credit_grant.id
      AND lot.expiry_policy_snapshot->>'type' = 'subscription_end'
      AND lot.consumed < lot.granted
      AND lot.expires_at IS DISTINCT FROM v_expiry;

    IF v_expiry IS NOT NULL AND v_expiry <= clock_timestamp() THEN
        FOR v_lot IN
            SELECT lot.id, lot.granted - lot.consumed AS amount
            FROM bursar.credit_lots AS lot
            JOIN bursar.billing_credit_grants AS credit_grant
              ON credit_grant.id = lot.source_id
             AND credit_grant.subscription_id = v_id
            WHERE lot.source_type = 'subscription_cycle'
              AND lot.expiry_policy_snapshot->>'type' = 'subscription_end'
              AND lot.consumed < lot.granted
              AND lot.expires_at <= clock_timestamp()
            ORDER BY lot.id
            FOR UPDATE OF lot
        LOOP
            PERFORM bursar.targeted_lot_debit(
                v_lot.id,
                'expiry',
                v_lot.amount,
                'subscription_end:' || v_id::text || ':' || v_lot.id::text
            );
        END LOOP;
    END IF;

    RETURN v_id;
END $$;

CREATE FUNCTION bursar.list_expired_grace_subscriptions(
    p_as_of timestamptz DEFAULT now(),
    p_limit integer DEFAULT 100
)
RETURNS SETOF bursar.billing_subscriptions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT subscription.*
    FROM bursar.billing_subscriptions AS subscription
    WHERE subscription.status = 'past_due'
      AND subscription.grace_ends_at IS NOT NULL
      AND subscription.grace_ends_at <= p_as_of
      AND subscription.grace_expired_at IS NULL
    ORDER BY subscription.grace_ends_at, subscription.id
    LIMIT LEAST(GREATEST(p_limit, 1), 1000)
$$;

CREATE FUNCTION bursar.mark_subscription_grace_expired(
    p_subscription_id uuid,
    p_expected_grace_ends_at timestamptz,
    p_expired_at timestamptz DEFAULT now()
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path TO '' AS $$
    UPDATE bursar.billing_subscriptions
    SET grace_expired_at = GREATEST(p_expired_at, grace_ends_at)
    WHERE id = p_subscription_id
      AND status = 'past_due'
      AND grace_ends_at = p_expected_grace_ends_at
      AND grace_ends_at <= p_expired_at
      AND grace_expired_at IS NULL
    RETURNING true
$$;

CREATE FUNCTION bursar.upsert_billing_payment(
    p_subject_id uuid,
    p_provider text,
    p_provider_payment_id text,
    p_amount_minor bigint,
    p_tax_minor bigint,
    p_currency char(3),
    p_purpose text,
    p_status bursar.billing_payment_status,
    p_provider_updated_at timestamptz DEFAULT now(),
    p_provider_invoice_id text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id uuid;
    v_environment text := bursar.current_provider_environment();
    v_existing bursar.billing_payments;
    v_metadata jsonb;
BEGIN
    IF p_provider_updated_at IS NULL
       OR (p_provider_invoice_id IS NOT NULL
           AND NOT bursar.is_nonempty_text(p_provider_invoice_id))
       OR p_metadata IS NULL
       OR jsonb_typeof(p_metadata) <> 'object'
    THEN
        RAISE EXCEPTION 'invalid billing payment' USING ERRCODE='22023';
    END IF;

    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;
    v_metadata := CASE
        WHEN bursar.is_subject_pseudonymized(p_subject_id) THEN '{}'::jsonb
        ELSE p_metadata
    END;

    INSERT INTO bursar.billing_payments(
        subject_id,provider,provider_environment,provider_payment_id,provider_invoice_id,
        amount_minor,tax_minor,currency,purpose,status,provider_updated_at,status_changed_at,
        metadata
    )
    VALUES (
        p_subject_id,p_provider,v_environment,p_provider_payment_id,p_provider_invoice_id,
        p_amount_minor,p_tax_minor,p_currency,p_purpose,p_status,p_provider_updated_at,
        p_provider_updated_at,v_metadata
    )
    ON CONFLICT (provider,provider_environment,provider_payment_id) DO UPDATE
    SET status=EXCLUDED.status,
        tax_minor=EXCLUDED.tax_minor,
        provider_invoice_id=COALESCE(
            EXCLUDED.provider_invoice_id,
            billing_payments.provider_invoice_id
        ),
        metadata=EXCLUDED.metadata,
        provider_updated_at=EXCLUDED.provider_updated_at,
        status_changed_at=CASE
            WHEN billing_payments.status IS DISTINCT FROM EXCLUDED.status
                THEN EXCLUDED.provider_updated_at
            ELSE billing_payments.status_changed_at
        END
    WHERE billing_payments.subject_id=EXCLUDED.subject_id
      AND billing_payments.amount_minor=EXCLUDED.amount_minor
      AND billing_payments.currency=EXCLUDED.currency
      AND billing_payments.purpose=EXCLUDED.purpose
      AND (
          billing_payments.provider_invoice_id IS NULL
          OR EXCLUDED.provider_invoice_id IS NULL
          OR billing_payments.provider_invoice_id=EXCLUDED.provider_invoice_id
      )
      AND EXCLUDED.provider_updated_at > billing_payments.provider_updated_at
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT *
        INTO v_existing
        FROM bursar.billing_payments
        WHERE provider = p_provider
          AND provider_environment = v_environment
          AND provider_payment_id = p_provider_payment_id;

        IF NOT FOUND
           OR ROW(
               v_existing.subject_id,
               v_existing.amount_minor,
               v_existing.currency,
               v_existing.purpose,
               CASE
                   WHEN v_existing.provider_invoice_id IS NOT NULL
                        AND p_provider_invoice_id IS NOT NULL
                   THEN v_existing.provider_invoice_id
               END
           ) IS DISTINCT FROM ROW(
               p_subject_id,
               p_amount_minor,
               p_currency,
               p_purpose,
               CASE
                   WHEN v_existing.provider_invoice_id IS NOT NULL
                        AND p_provider_invoice_id IS NOT NULL
                   THEN p_provider_invoice_id
               END
           )
        THEN
            RAISE EXCEPTION 'payment identity conflict' USING ERRCODE='23505';
        END IF;

        IF v_existing.provider_updated_at = p_provider_updated_at
           AND ROW(
               v_existing.tax_minor,
               v_existing.status,
               v_existing.metadata,
               v_existing.provider_invoice_id
           ) IS DISTINCT FROM ROW(
               p_tax_minor,
               p_status,
               v_metadata,
               COALESCE(p_provider_invoice_id, v_existing.provider_invoice_id)
           )
        THEN
            RAISE EXCEPTION
                'conflicting payment state at provider timestamp'
                USING ERRCODE='23514';
        END IF;

        v_id := v_existing.id;
    END IF;

    RETURN v_id;

END $$;
