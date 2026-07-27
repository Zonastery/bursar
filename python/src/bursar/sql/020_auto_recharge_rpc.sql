-- Auto-recharge claim and state RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.claim_auto_recharge_attempt(
    p_subject_id uuid,
    p_idempotency_key text
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

 IF p_idempotency_key IS NULL OR p_idempotency_key='' OR NOT FOUND THEN RETURN;
 END IF;

 SELECT * INTO v_attempt
 FROM bursar.billing_auto_recharge_attempts
 WHERE subject_id=p_subject_id AND idempotency_key=p_idempotency_key;

 IF FOUND THEN RETURN NEXT v_attempt;
 RETURN;
 END IF;

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

 IF v_balance>=v_profile.threshold THEN RETURN;
 END IF;

    SELECT * INTO v_attempt
    FROM bursar.billing_auto_recharge_attempts
    WHERE subject_id=p_subject_id
      AND state IN ('claimed','submitted','processing','unknown','action_required')
    ORDER BY created_at DESC
    LIMIT 1;

    IF FOUND THEN RETURN NEXT v_attempt;
 RETURN;
 END IF;

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

    IF NOT FOUND THEN RETURN false;
 END IF;

    IF v_old=p_state THEN RETURN true;
 END IF;

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
