CREATE FUNCTION bursar.claim_billing_event(
    p_provider text,
    p_event_id text,
    p_event_type text,
    p_envelope jsonb DEFAULT '{}'::jsonb,
    p_lease_seconds integer DEFAULT 300,
    p_attempt_limit integer DEFAULT 3
)
RETURNS TABLE(result text,event_id uuid,claim_token uuid)
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
    p_provider text,p_event_id text,p_claim_token uuid
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
    p_provider text,p_event_id text,p_claim_token uuid
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
    p_subject_id uuid,p_subscription_id uuid
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
    PERFORM 1
    FROM bursar.billing_subscriptions
    WHERE id=p_subscription_id AND subject_id=p_subject_id
    FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
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
    p_grant_id uuid,p_idempotency_key text
)
RETURNS TABLE(
    ledger_entry_id uuid,balance_after numeric,replayed boolean,error_code text
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
    p_refund_id uuid,p_grant_id uuid,p_amount_minor bigint,p_idempotency_key text
)
RETURNS TABLE(
    ledger_entry_id uuid,balance_after numeric,replayed boolean,error_code text
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

CREATE FUNCTION bursar.claim_auto_recharge_attempt(
    p_subject_id uuid,p_idempotency_key text
)
RETURNS SETOF bursar.billing_auto_recharge_attempts
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_profile bursar.billing_auto_recharge_profiles;
    v_attempt bursar.billing_auto_recharge_attempts;
    v_count integer;
    v_balance numeric;
    v_step interval;
    v_window_start timestamptz;
    v_window_end timestamptz;
BEGIN
    SELECT * INTO v_profile
    FROM bursar.billing_auto_recharge_profiles
    WHERE subject_id=p_subject_id
    FOR UPDATE;
 IF p_idempotency_key IS NULL OR p_idempotency_key='' OR NOT FOUND THEN RETURN; END IF;
 SELECT * INTO v_attempt
 FROM bursar.billing_auto_recharge_attempts
 WHERE subject_id=p_subject_id AND idempotency_key=p_idempotency_key;
 IF FOUND THEN RETURN NEXT v_attempt; RETURN; END IF;
 IF NOT v_profile.enabled OR NOT v_profile.armed
 OR v_profile.state<>'active' OR v_profile.topup_id IS NULL
 THEN
  RETURN;
 END IF;
 SELECT balance INTO v_balance
 FROM bursar.credit_accounts
 WHERE subject_id=p_subject_id AND account_kind='personal'
 FOR UPDATE;
 v_balance:=COALESCE(v_balance,0);
 IF v_balance>=v_profile.threshold THEN RETURN; END IF;
    SELECT * INTO v_attempt
    FROM bursar.billing_auto_recharge_attempts
    WHERE subject_id=p_subject_id
      AND state IN ('claimed','submitted','processing','unknown','action_required')
    ORDER BY created_at DESC
    LIMIT 1;
    IF FOUND THEN RETURN NEXT v_attempt; RETURN; END IF;

    v_step:=CASE v_profile.window_unit
        WHEN 'day' THEN make_interval(days=>v_profile.window_count)
        WHEN 'week' THEN make_interval(weeks=>v_profile.window_count)
        WHEN 'month' THEN make_interval(months=>v_profile.window_count)
        WHEN 'year' THEN make_interval(years=>v_profile.window_count)
    END;
    IF v_profile.window_anchor='rolling' THEN
        v_window_end:=now();
        v_window_start:=(now() AT TIME ZONE v_profile.window_timezone-v_step)
                        AT TIME ZONE v_profile.window_timezone;
    ELSE
        v_window_start:=date_trunc(
            v_profile.window_unit,
            now() AT TIME ZONE v_profile.window_timezone
        ) AT TIME ZONE v_profile.window_timezone;
        v_window_end:=(v_window_start AT TIME ZONE v_profile.window_timezone+v_step)
                      AT TIME ZONE v_profile.window_timezone;
    END IF;

    SELECT count(*) INTO v_count
    FROM bursar.billing_auto_recharge_attempts
    WHERE subject_id=p_subject_id
      AND created_at>=v_window_start
      AND created_at<v_window_end
      AND state IN ('submitted','processing','succeeded','action_required');
    IF v_profile.max_charges_per_window IS NOT NULL
       AND v_count>=v_profile.max_charges_per_window
    THEN
        RETURN;
    END IF;
    INSERT INTO bursar.billing_auto_recharge_attempts(
        subject_id,provider,idempotency_key,topup_id,quantity,window_start,window_end
    )
    VALUES (
        p_subject_id,v_profile.provider,p_idempotency_key,
        v_profile.topup_id,v_profile.quantity,v_window_start,v_window_end
    )
    RETURNING * INTO v_attempt;
    UPDATE bursar.billing_auto_recharge_profiles
    SET armed=false
    WHERE subject_id=p_subject_id;
    RETURN NEXT v_attempt;
END $$;

CREATE FUNCTION bursar.advance_auto_recharge_attempt(
    p_attempt_id uuid,
    p_state bursar.recharge_attempt_status,
    p_provider_attempt_id text DEFAULT NULL,
    p_failure_code text DEFAULT NULL,
    p_failure_message text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_old bursar.recharge_attempt_status;
    v_subject uuid;
BEGIN
    SELECT state,subject_id INTO v_old,v_subject
    FROM bursar.billing_auto_recharge_attempts
    WHERE id=p_attempt_id
    FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF v_old=p_state THEN RETURN true; END IF;
    IF (v_old,p_state) NOT IN (
        ('claimed','submitted'),
        ('submitted','processing'),
        ('processing','succeeded'),
        ('processing','failed'),
        ('processing','unknown'),
        ('unknown','processing'),
        ('unknown','action_required'),
        ('action_required','processing')
    ) THEN
        RETURN false;
    END IF;
    UPDATE bursar.billing_auto_recharge_attempts
    SET state=p_state,
        provider_attempt_id=COALESCE(p_provider_attempt_id,provider_attempt_id),
        failure_code=COALESCE(p_failure_code,failure_code),
        failure_message=COALESCE(p_failure_message,failure_message),
        metadata=COALESCE(p_metadata,'{}'::jsonb)
    WHERE id=p_attempt_id;
    -- A successful attempt remains disarmed until its credit grant updates the
    -- balance. Rearming here permits a second attempt before the first grant
    -- is posted.
    IF p_state='failed' THEN
        UPDATE bursar.billing_auto_recharge_profiles
        SET armed=true
        WHERE subject_id=v_subject AND enabled;
    END IF;
    RETURN true;
END $$;
