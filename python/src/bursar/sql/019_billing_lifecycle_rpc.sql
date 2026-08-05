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
RETURNS TABLE (
    result text,
    event_id uuid,
    claim_token uuid
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_event bursar.billing_events;

    v_token uuid:=gen_random_uuid();
    v_environment text:=bursar.current_provider_environment();
    v_envelope jsonb:=COALESCE(p_envelope,'{}'::jsonb);
    v_digest bytea;
    v_received_at timestamptz := now();

BEGIN
    IF p_provider IS NULL OR p_provider='' OR p_event_id IS NULL OR p_event_id=''
       OR p_event_type IS NULL OR p_event_type=''
       OR p_lease_seconds<1 OR p_lease_seconds>3600
       OR p_attempt_limit<1 OR p_attempt_limit>20
    THEN
        RETURN QUERY SELECT 'invalid_request',NULL::uuid,NULL::uuid;

        RETURN;

    END IF;

    IF NOT bursar.is_bounded_json_object(v_envelope, 1048576)
       OR NOT bursar.is_bounded_text(p_provider, 100)
       OR NOT bursar.is_bounded_text(p_event_id, 255)
       OR NOT bursar.is_bounded_text(p_event_type, 255)
    THEN
        RETURN QUERY SELECT 'invalid_request',NULL::uuid,NULL::uuid;
        RETURN;
    END IF;

    v_digest:=extensions.digest(convert_to(v_envelope::text,'UTF8'),'sha256');

    INSERT INTO bursar.billing_events(
        provider,provider_environment,provider_event_id,event_type,
        envelope_digest,payload_received_at,status,claim_token,claim_expires_at
    )
    VALUES (
        p_provider,v_environment,p_event_id,p_event_type,v_digest,v_received_at,
        'processing',v_token,now()+make_interval(secs=>p_lease_seconds)
    )
    ON CONFLICT (
        tenant_id,
        provider,
        provider_environment,
        provider_event_id
    ) DO NOTHING
    RETURNING * INTO v_event;

    IF FOUND THEN
        IF bursar.current_billing_payload_backend() = 'postgres' THEN
            INSERT INTO bursar.billing_event_payloads(
                event_id,
                received_at,
                envelope
            )
            VALUES(
                v_event.id,
                v_received_at,
                v_envelope
            );
        ELSE
            INSERT INTO bursar.event_outbox(
                topic,
                aggregate_type,
                aggregate_id,
                idempotency_key,
                payload
            )
            VALUES(
                'billing.webhook_received',
                'billing_event',
                v_event.id,
                'billing-event-received:' || v_event.id::text,
                jsonb_build_object(
                    'delivery_required', true,
                    'tenant_id', v_event.tenant_id,
                    'event_id', v_event.id,
                    'provider', v_event.provider,
                    'provider_environment', v_event.provider_environment,
                    'provider_event_id', v_event.provider_event_id,
                    'event_type', v_event.event_type,
                    'status', v_event.status,
                    'received_at', v_event.payload_received_at,
                    'completed_at', v_event.completed_at,
                    'envelope', v_envelope
                )
            );
        END IF;

        RETURN QUERY SELECT 'claimed',v_event.id,v_token;

        RETURN;

    END IF;

    SELECT * INTO v_event
    FROM bursar.billing_events
    WHERE provider=p_provider
      AND provider_environment=v_environment
      AND provider_event_id=p_event_id
    FOR UPDATE;

    IF v_event.event_type<>p_event_type
       OR v_event.envelope_digest<>v_digest
    THEN
        RETURN QUERY SELECT 'idempotency_conflict',v_event.id,NULL::uuid;

        RETURN;

    END IF;

    IF v_event.status='completed' THEN
        RETURN QUERY SELECT 'duplicate',v_event.id,NULL::uuid;

        RETURN;

    END IF;

    IF v_event.attempt_count>=p_attempt_limit THEN
        RETURN QUERY SELECT 'max_retries_exceeded',v_event.id,NULL::uuid;

        RETURN;

    END IF;

    IF v_event.status='processing' AND v_event.claim_expires_at>now() THEN
        -- Do not acknowledge an in-flight delivery as completed. If the active
        -- worker dies after this request returns, a provider that received a
        -- successful duplicate response may never retry the event.
        RETURN QUERY SELECT 'busy',v_event.id,NULL::uuid;

        RETURN;

    END IF;

    UPDATE bursar.billing_events
    SET status='processing',
        attempt_count=attempt_count+1,
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
    SET status='completed',claim_token=NULL,claim_expires_at=NULL,
        completed_at=now()
    WHERE provider=p_provider
      AND provider_environment=bursar.current_provider_environment()
      AND provider_event_id=p_event_id
      AND status='processing'
      AND claim_token=p_claim_token
      AND claim_expires_at>now()
    RETURNING true
$$;

CREATE FUNCTION bursar.fail_billing_event(
    p_provider text,
    p_event_id text,
    p_claim_token uuid,
    p_error text DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_updated boolean;
BEGIN
    IF p_error IS NOT NULL
       AND NOT bursar.is_nonempty_bounded_text(p_error, 8192)
    THEN
        RETURN false;
    END IF;

    UPDATE bursar.billing_events
    SET status='failed',
        claim_token=NULL,
        claim_expires_at=NULL,
        last_error=p_error
    WHERE provider=p_provider
      AND provider_environment=bursar.current_provider_environment()
      AND provider_event_id=p_event_id
      AND status='processing'
      AND claim_token=p_claim_token
    RETURNING true INTO v_updated;

    RETURN COALESCE(v_updated, false);
END
$$;

CREATE FUNCTION bursar.record_subscription_conflict(
    p_subject_id uuid,
    p_provider text,
    p_duplicate_provider_subscription_id text,
    p_existing_provider_subscription_id text DEFAULT NULL,
    p_provider_event_id text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id bigint;
    v_existing_subscription_id uuid;
    v_billing_event_id uuid;
    v_environment text := bursar.current_provider_environment();
    v_existing bursar.billing_subscription_conflicts;
BEGIN
    IF NOT bursar.is_nonempty_text(p_provider)
       OR NOT bursar.is_nonempty_text(p_duplicate_provider_subscription_id)
       OR NOT bursar.is_bounded_json_object(p_metadata, 16384)
    THEN
        RAISE EXCEPTION 'invalid subscription conflict'
            USING ERRCODE = '22023';
    END IF;

    IF p_existing_provider_subscription_id IS NOT NULL THEN
        SELECT subscription.id
        INTO v_existing_subscription_id
        FROM bursar.billing_subscriptions AS subscription
        WHERE subscription.provider = p_provider
          AND subscription.provider_environment = v_environment
          AND subscription.provider_subscription_id =
              p_existing_provider_subscription_id
          AND (
              p_subject_id IS NULL
              OR subscription.subject_id = p_subject_id
          );

        IF NOT FOUND THEN
            RAISE EXCEPTION 'existing subscription missing for conflict'
                USING ERRCODE = '23503';
        END IF;
    END IF;

    IF p_provider_event_id IS NOT NULL THEN
        SELECT event.id
        INTO v_billing_event_id
        FROM bursar.billing_events AS event
        WHERE event.provider = p_provider
          AND event.provider_environment = v_environment
          AND event.provider_event_id = p_provider_event_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'billing event missing for conflict'
                USING ERRCODE = '23503';
        END IF;
    END IF;

    INSERT INTO bursar.billing_subscription_conflicts(
        subject_id,
        provider,
        provider_environment,
        duplicate_provider_subscription_id,
        existing_subscription_id,
        billing_event_id,
        metadata
    )
    VALUES (
        p_subject_id,
        p_provider,
        v_environment,
        p_duplicate_provider_subscription_id,
        v_existing_subscription_id,
        v_billing_event_id,
        p_metadata
    )
    ON CONFLICT (
        tenant_id,
        provider,
        provider_environment,
        duplicate_provider_subscription_id
    ) DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT *
        INTO v_existing
        FROM bursar.billing_subscription_conflicts AS conflict
        WHERE conflict.provider = p_provider
          AND conflict.provider_environment = v_environment
          AND conflict.duplicate_provider_subscription_id =
              p_duplicate_provider_subscription_id;

        IF NOT FOUND
           OR ROW(
               v_existing.subject_id,
               v_existing.existing_subscription_id
           ) IS DISTINCT FROM ROW(
               p_subject_id,
               v_existing_subscription_id
           )
        THEN
            RAISE EXCEPTION 'subscription conflict identity mismatch'
                USING ERRCODE = '23505';
        END IF;

        v_id := v_existing.id;
    END IF;

    RETURN v_id;
END $$;

CREATE FUNCTION bursar.select_entitlement_source(
    p_subject_id uuid,
    p_subscription_id uuid
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_environment text := bursar.current_provider_environment();
BEGIN
    PERFORM 1
    FROM bursar.billing_subscriptions
    WHERE id = p_subscription_id
      AND subject_id = p_subject_id
      AND provider_environment = v_environment
    FOR UPDATE;

    IF NOT FOUND THEN RETURN false;
 END IF;

    UPDATE bursar.billing_entitlement_sources
    SET selected = false,
        deselected_at = now()
    WHERE subject_id = p_subject_id
      AND provider_environment = v_environment
      AND selected;

    INSERT INTO bursar.billing_entitlement_sources(
        subject_id,
        provider_environment,
        subscription_id,
        selected,
        selected_at
    )
    VALUES (p_subject_id, v_environment, p_subscription_id, true, now())
    ON CONFLICT (
        tenant_id,
        subject_id,
        provider_environment,
        subscription_id
    )
    DO UPDATE
    SET selected = true,
        selected_at = now(),
        deselected_at = NULL;

    RETURN true;

END $$;

CREATE FUNCTION bursar.grant_billing_credit(
    p_grant_id uuid,
    p_idempotency_key text
)
RETURNS TABLE (
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
    v_expiry_policy jsonb;
    v_expires_at timestamptz;
    v_subscription uuid;
    v_lot_behavior text:='separate_lots';
    v_new_lot uuid;
    v_target_lot uuid;
    v_lot_amount numeric;
    v_account uuid;
    v_cycle_renewal text;
    v_prior_lot record;

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
        SELECT
            topup.bucket_key,
            COALESCE(topup.expiry_policy,bucket.expiry_policy),
            topup.lot_behavior
        INTO v_bucket,v_expiry_policy,v_lot_behavior
        FROM bursar.catalog_topups AS topup
        JOIN bursar.catalog_buckets AS bucket
          ON bucket.catalog_revision_id=topup.catalog_revision_id
         AND bucket.bucket_key=topup.bucket_key
        WHERE topup.id=g.topup_id
          AND topup.catalog_revision_id=g.catalog_revision_id;

        v_kind:='purchase';

    ELSE
        SELECT
            o.cycle_grant_bucket_key,
            o.cycle_grant_expiry_policy,
            s.id,
            o.cycle_grant_renewal
        INTO
            v_bucket,
            v_expiry_policy,
            v_subscription,
            v_cycle_renewal
        FROM bursar.billing_subscriptions s
        JOIN bursar.catalog_offers o ON o.id=s.offer_id
        WHERE s.id=g.subscription_id;

        v_kind:='grant';

    END IF;

    IF v_bucket IS NULL THEN
        SELECT bucket_key,expiry_policy INTO v_bucket,v_expiry_policy
        FROM bursar.catalog_buckets
        WHERE catalog_revision_id=g.catalog_revision_id AND is_default;

    END IF;

    IF v_bucket IS NULL THEN
        RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'missing_grant_bucket';

        RETURN;

    END IF;

    IF v_expiry_policy IS NULL THEN
        SELECT expiry_policy INTO v_expiry_policy
        FROM bursar.catalog_buckets
        WHERE catalog_revision_id=g.catalog_revision_id
          AND bucket_key=v_bucket;
    END IF;

    v_expires_at:=bursar.expiry_policy_at(
        g.subject_id,g.catalog_revision_id,v_expiry_policy,now(),v_subscription
    );

    SELECT * INTO v_result
    FROM bursar.post_credit(
        g.subject_id,v_kind,g.configured_credits*g.quantity,'billing_grant',
        p_idempotency_key,jsonb_build_object('grant_id',p_grant_id),
        v_bucket,g.catalog_revision_id,v_expires_at,NULL
    );

    IF v_result.error_code IS NULL THEN
        PERFORM set_config('bursar.mutation_context','internal',true);

        UPDATE bursar.billing_credit_grants
        SET ledger_entry_id=v_result.entry_id,
            expiry_policy_snapshot=v_expiry_policy
        WHERE id=g.id;

        SELECT lot.id,lot.account_id,lot.granted
        INTO v_new_lot,v_account,v_lot_amount
        FROM bursar.credit_lots AS lot
        WHERE lot.source_entry_id=v_result.entry_id
        FOR UPDATE;

        IF v_new_lot IS NOT NULL THEN
            UPDATE bursar.credit_lots
            SET source_type=CASE
                    WHEN g.payment_id IS NOT NULL THEN 'topup'
                    ELSE 'subscription_cycle'
                END,
                source_id=g.id,
                expiry_policy_snapshot=v_expiry_policy
            WHERE id=v_new_lot;

            UPDATE bursar.credit_lot_sources AS source
            SET source_type=CASE
                    WHEN g.payment_id IS NOT NULL THEN 'topup'
                    ELSE 'subscription_cycle'
                END,
                source_id=g.id
            WHERE source.ledger_entry_id=v_result.entry_id;
        END IF;

        IF v_new_lot IS NOT NULL AND v_lot_behavior='merge_and_refresh' THEN
            SELECT id INTO v_target_lot
            FROM bursar.credit_lots
            WHERE account_id=v_account
              AND catalog_revision_id=g.catalog_revision_id
              AND bucket_key=v_bucket
              AND id<>v_new_lot
              AND consumed<granted
              AND (expires_at IS NULL OR expires_at>now())
            ORDER BY created_at DESC,id DESC
            LIMIT 1
            FOR UPDATE;

            IF v_target_lot IS NOT NULL THEN
                DELETE FROM bursar.credit_lots WHERE id=v_new_lot;

                UPDATE bursar.credit_lots
                SET granted=granted+v_lot_amount,
                    expires_at=v_expires_at,
                    expiry_policy_snapshot=v_expiry_policy
                WHERE id=v_target_lot;

                INSERT INTO bursar.credit_lot_sources(
                    lot_id,ledger_entry_id,amount,source_type,source_id
                )
                VALUES(
                    v_target_lot,v_result.entry_id,v_lot_amount,
                    'topup',g.id
                );
            END IF;
        END IF;

        IF g.subscription_id IS NOT NULL
           AND v_cycle_renewal = 'replace_previous'
        THEN
            FOR v_prior_lot IN
                SELECT
                    lot.id,
                    lot.granted - lot.consumed AS amount
                FROM bursar.billing_credit_grants AS prior_grant
                JOIN bursar.credit_lots AS lot
                  ON lot.source_type = 'subscription_cycle'
                 AND lot.source_id = prior_grant.id
                WHERE prior_grant.subscription_id = g.subscription_id
                  AND prior_grant.id <> g.id
                  AND prior_grant.ledger_entry_id IS NOT NULL
                  AND lot.consumed < lot.granted
                ORDER BY lot.created_at, lot.id
                FOR UPDATE OF lot
            LOOP
                PERFORM bursar.targeted_lot_debit(
                    v_prior_lot.id,
                    'revocation',
                    v_prior_lot.amount,
                    'cycle_replace:' || g.id::text
                        || ':' || v_prior_lot.id::text
                );
            END LOOP;
        END IF;
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
RETURNS TABLE (
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

    IF r.status <> 'succeeded' THEN
        RETURN QUERY
        SELECT NULL::uuid,NULL::numeric,false,'refund_not_succeeded';
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

    -- Claw back the refunded grant's own remaining credits. If those credits
    -- were already consumed, the remainder becomes explicit refund debt
    -- instead of silently taking credits purchased by a different grant.
    SELECT * INTO v_result FROM bursar.clawback_credit_source(
        g.ledger_entry_id,
        a.credit_amount,
        p_idempotency_key,
        jsonb_build_object(
            'refund_id', p_refund_id,
            'grant_id', p_grant_id,
            'amount_minor', p_amount_minor
        )
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
