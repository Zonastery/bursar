-- Billing event, grant, and refund RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.claim_billing_event(
    p_provider text,
    p_event_id text,
    p_event_type text,
    p_envelope jsonb DEFAULT '{}'::jsonb,
    p_lease_seconds integer DEFAULT 300,
    p_attempt_limit integer DEFAULT 3
)
RETURNS TABLE(
    result text,
    event_id uuid,
    claim_token uuid
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_event bursar.billing_events;

    v_token uuid:=gen_random_uuid();

BEGIN
    IF p_provider IS NULL OR p_provider='' OR p_event_id IS NULL OR p_event_id=''
       OR p_event_type IS NULL OR p_event_type=''
       OR p_lease_seconds<1 OR p_lease_seconds>3600
       OR p_attempt_limit<1 OR p_attempt_limit>20
    THEN
        RETURN QUERY SELECT 'invalid_request',NULL::uuid,NULL::uuid;

        RETURN;

    END IF;

    INSERT INTO bursar.billing_events(
        provider,provider_event_id,event_type,envelope,status,claim_token,claim_expires_at
    )
    VALUES (
        p_provider,p_event_id,p_event_type,COALESCE(p_envelope,'{}'::jsonb),
        'processing',v_token,now()+make_interval(secs=>p_lease_seconds)
    )
    ON CONFLICT DO NOTHING
    RETURNING * INTO v_event;

    IF FOUND THEN
        RETURN QUERY SELECT 'claimed',v_event.id,v_token;

        RETURN;

    END IF;

    SELECT * INTO v_event
    FROM bursar.billing_events
    WHERE provider=p_provider AND provider_event_id=p_event_id
    FOR UPDATE;

    IF v_event.event_type<>p_event_type
       OR v_event.envelope<>COALESCE(p_envelope,'{}'::jsonb)
    THEN
        RETURN QUERY SELECT 'idempotency_conflict',v_event.id,NULL::uuid;

        RETURN;

    END IF;

    IF v_event.status='completed' THEN
        RETURN QUERY SELECT 'duplicate',v_event.id,NULL::uuid;

        RETURN;

    END IF;

    IF v_event.retry_count>=p_attempt_limit THEN
        RETURN QUERY SELECT 'max_retries_exceeded',v_event.id,NULL::uuid;

        RETURN;

    END IF;

    IF v_event.status='processing' AND v_event.claim_expires_at>now() THEN
        RETURN QUERY SELECT 'busy',v_event.id,NULL::uuid;

        RETURN;

    END IF;

    UPDATE bursar.billing_events
    SET status='processing',
        retry_count=retry_count+1,
        claim_token=v_token,
        claim_expires_at=now()+make_interval(secs=>p_lease_seconds)
    WHERE id=v_event.id;

    RETURN QUERY SELECT 'claimed',v_event.id,v_token;

END $$;

CREATE FUNCTION bursar.complete_billing_event(
    p_provider text,
    p_event_id text,
    p_claim_token uuid
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path TO '' AS $$
    UPDATE bursar.billing_events
    SET status='completed',claim_token=NULL,claim_expires_at=NULL
    WHERE provider=p_provider
      AND provider_event_id=p_event_id
      AND status='processing'
      AND claim_token=p_claim_token
      AND claim_expires_at>now()
    RETURNING true
$$;

CREATE FUNCTION bursar.fail_billing_event(
    p_provider text,
    p_event_id text,
    p_claim_token uuid
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path TO '' AS $$
    UPDATE bursar.billing_events
    SET status='failed',claim_token=NULL,claim_expires_at=NULL
    WHERE provider=p_provider
      AND provider_event_id=p_event_id
      AND status='processing'
      AND claim_token=p_claim_token
    RETURNING true
$$;

CREATE FUNCTION bursar.select_entitlement_source(
    p_subject_id uuid,
    p_subscription_id uuid
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
    PERFORM 1
    FROM bursar.billing_subscriptions
    WHERE id=p_subscription_id AND subject_id=p_subject_id
    FOR UPDATE;

    IF NOT FOUND THEN RETURN false;
 END IF;

    UPDATE bursar.billing_entitlement_sources
    SET selected=false,selected_at=NULL
    WHERE subject_id=p_subject_id;

    INSERT INTO bursar.billing_entitlement_sources(
        subject_id,subscription_id,selected,selected_at
    )
    VALUES (p_subject_id,p_subscription_id,true,now())
    ON CONFLICT (subject_id,subscription_id)
    DO UPDATE SET selected=true,selected_at=now();

    RETURN true;

END $$;

CREATE FUNCTION bursar.grant_billing_credit(
    p_grant_id uuid,
    p_idempotency_key text
)
RETURNS TABLE(
    ledger_entry_id uuid,
    balance_after numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    g bursar.billing_credit_grants;

    v_result record;

    v_bucket text;

    v_kind bursar.ledger_entry_kind;

BEGIN
    IF p_idempotency_key IS NULL OR p_idempotency_key='' THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_request';

        RETURN;

    END IF;

    SELECT * INTO g
    FROM bursar.billing_credit_grants
    WHERE id=p_grant_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'missing_grant';

        RETURN;

    END IF;

    IF g.ledger_entry_id IS NOT NULL THEN
        RETURN QUERY
        SELECT g.ledger_entry_id,e.balance_after,true,NULL::text
        FROM bursar.credit_ledger_entries e
        WHERE e.id=g.ledger_entry_id;

        RETURN;

    END IF;

    IF g.payment_id IS NOT NULL THEN
        SELECT bucket_key INTO v_bucket
        FROM bursar.catalog_topups
        WHERE id=g.topup_id AND catalog_revision_id=g.catalog_revision_id;

        v_kind:='purchase';

    ELSE
        SELECT o.grant_bucket_key INTO v_bucket
        FROM bursar.billing_subscriptions s
        JOIN bursar.catalog_offers o ON o.id=s.offer_id
        WHERE s.id=g.subscription_id;

        v_kind:='grant';

    END IF;

    IF v_bucket IS NULL THEN
        SELECT bucket_key INTO v_bucket
        FROM bursar.catalog_buckets
        WHERE catalog_revision_id=g.catalog_revision_id AND is_default;

    END IF;

    IF v_bucket IS NULL THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'missing_grant_bucket';

        RETURN;

    END IF;

    SELECT * INTO v_result
    FROM bursar.post_credit(
        g.subject_id,v_kind,g.configured_credits*g.quantity,'billing_grant',
        p_idempotency_key,jsonb_build_object('grant_id',p_grant_id),
        v_bucket,g.catalog_revision_id
    );

    IF v_result.error_code IS NULL THEN
        UPDATE bursar.billing_credit_grants
        SET ledger_entry_id=v_result.entry_id
        WHERE id=g.id;

    END IF;

    RETURN QUERY
    SELECT v_result.entry_id,v_result.balance_after,v_result.replayed,v_result.error_code;

END $$;

CREATE FUNCTION bursar.post_billing_refund(
    p_refund_id uuid,
    p_grant_id uuid,
    p_amount_minor bigint,
    p_idempotency_key text
)
RETURNS TABLE(
    ledger_entry_id uuid,
    balance_after numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    r bursar.billing_refunds;

    g bursar.billing_credit_grants;

    a bursar.billing_refund_grants;

    v_entry uuid;

    v_balance numeric;

    v_result record;

BEGIN
    IF p_amount_minor<=0 OR p_idempotency_key IS NULL OR p_idempotency_key='' THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_request';

        RETURN;

    END IF;

    SELECT * INTO r FROM bursar.billing_refunds WHERE id=p_refund_id FOR UPDATE;

    SELECT * INTO g FROM bursar.billing_credit_grants WHERE id=p_grant_id FOR UPDATE;

    IF r.id IS NULL OR g.id IS NULL THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'missing_refund_or_grant';

        RETURN;

    END IF;

    SELECT * INTO a
    FROM bursar.billing_refund_grants
    WHERE refund_id=p_refund_id AND grant_id=p_grant_id
    FOR UPDATE;

    IF FOUND THEN
        IF a.amount_minor<>p_amount_minor THEN
            RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'idempotency_conflict';

            RETURN;

        END IF;

        IF a.ledger_entry_id IS NOT NULL THEN
            RETURN QUERY
            SELECT a.ledger_entry_id,e.balance_after,true,NULL::text
            FROM bursar.credit_ledger_entries e
            WHERE e.id=a.ledger_entry_id;

            RETURN;

        END IF;

    ELSE
        INSERT INTO bursar.billing_refund_grants(refund_id,grant_id,amount_minor)
        VALUES (p_refund_id,p_grant_id,p_amount_minor)
        RETURNING * INTO a;

    END IF;

    IF g.ledger_entry_id IS NULL THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'grant_not_posted';

        RETURN;

    END IF;

    -- A provider refund must not fail merely because the purchased credits have
    -- already been spent.  post_credit consumes any remaining source lots and
    -- records the remainder as an explicit negative balance (refund debt).
    SELECT * INTO v_result FROM bursar.post_credit(
        g.subject_id, 'refund_clawback', -a.credit_amount,
        'billing_refund_clawback', p_idempotency_key,
        jsonb_build_object('refund_id', p_refund_id, 'grant_id', p_grant_id)
    );

    IF v_result.error_code IS NOT NULL THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,v_result.error_code;

        RETURN;

    END IF;

    v_entry := v_result.entry_id;

    UPDATE bursar.billing_refund_grants
    SET ledger_entry_id=v_entry
    WHERE refund_id=p_refund_id AND grant_id=p_grant_id;

    SELECT e.balance_after INTO v_balance
    FROM bursar.credit_ledger_entries e
    WHERE e.id=v_entry;

    RETURN QUERY SELECT v_entry,v_balance,false,NULL::text;

EXCEPTION
    WHEN unique_violation THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'idempotency_conflict';

    WHEN check_violation OR foreign_key_violation THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_refund_allocation';

END $$;
