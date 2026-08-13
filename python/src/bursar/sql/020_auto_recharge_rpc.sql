-- Migration: 020_auto_recharge_rpc.sql
-- Purpose: Define idempotent auto-recharge claim and terminal-state transitions.
-- Depends on: Auto-recharge profiles, attempts, catalog policies, and credit accounts.
-- Security: SECURITY DEFINER RPCs require tenant context; claims use the current
--   provider environment and serialize threshold/window admission.

-- Claim or replay one recharge attempt under the active profile, provider
-- environment, balance threshold, cooldown, and window charge-count fences.

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

    v_window_start timestamptz;

    v_window_end timestamptz;
    v_environment text:=bursar.current_provider_environment();

BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_nonempty_bounded_text(p_idempotency_key, 255)
    THEN
        RETURN;
    END IF;

    SELECT * INTO v_profile
    FROM bursar.billing_auto_recharge_profiles
    WHERE subject_id=p_subject_id
      AND provider_environment=v_environment
    FOR UPDATE;

 IF NOT FOUND THEN RETURN;
 END IF;

 SELECT * INTO v_attempt
 FROM bursar.billing_auto_recharge_attempts
 WHERE subject_id=p_subject_id
   AND provider_environment=v_environment
   AND idempotency_key=p_idempotency_key;

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
      AND provider_environment=v_environment
      AND state IN ('claimed','submitted','processing','unknown','action_required')
    ORDER BY created_at DESC
    LIMIT 1;

    IF FOUND THEN RETURN NEXT v_attempt;
 RETURN;
 END IF;

 IF v_profile.last_attempt_at IS NOT NULL
    AND v_profile.last_attempt_at
        + make_interval(secs=>v_profile.cooldown_seconds)>now()
 THEN
  RETURN;
 END IF;

    SELECT window_start, window_end
    INTO v_window_start, v_window_end
    FROM bursar.policy_period_window(
        -- Auto-recharge windows are calendar- or statement-time rolling
        -- periods, never plan-assignment anchored. NULL is therefore the
        -- intentional sentinel for the unused assignment anchor.
        NULL,
        v_profile.window_unit,
        v_profile.window_count,
        v_profile.window_anchor,
        v_profile.window_timezone
    );

    SELECT count(*) INTO v_count
    FROM bursar.billing_auto_recharge_attempts
    WHERE subject_id=p_subject_id
      AND provider_environment=v_environment
      AND created_at>=v_window_start
      AND created_at<v_window_end
      AND state IN ('submitted','processing','succeeded','action_required');

    IF v_profile.max_charges_per_window IS NOT NULL
       AND v_count>=v_profile.max_charges_per_window
    THEN
        RETURN;

    END IF;

    INSERT INTO bursar.billing_auto_recharge_attempts(
        subject_id,provider,provider_environment,idempotency_key,topup_id,
        catalog_revision_id,quantity,window_start,window_end
    )
    VALUES (
        p_subject_id,v_profile.provider,v_profile.provider_environment,
        p_idempotency_key,v_profile.topup_id,v_profile.catalog_revision_id,
        v_profile.quantity,v_window_start,v_window_end
    )
    RETURNING * INTO v_attempt;

    UPDATE bursar.billing_auto_recharge_profiles
    SET armed=false,last_attempt_at=now()
    WHERE subject_id=p_subject_id
      AND provider_environment=v_environment;

    RETURN NEXT v_attempt;

END $$;

-- Advance one tenant-owned attempt through its legal provider lifecycle without
-- reopening terminal outcomes or replacing an established provider attempt ID.
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
    v_environment text;
    v_metadata jsonb;
    v_failure_message text;

BEGIN
    IF p_attempt_id IS NULL
       OR p_state IS NULL
       OR (
           p_provider_attempt_id IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(
               p_provider_attempt_id,
               255
           )
       )
       OR (
           p_failure_code IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_failure_code, 255)
       )
       OR (
           p_failure_message IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_failure_message, 8192)
       )
       OR NOT bursar.is_bounded_json_object(
           COALESCE(p_metadata, '{}'::jsonb),
           16384
       )
    THEN
        RETURN false;
    END IF;

    SELECT state,subject_id,provider_environment
    INTO v_old,v_subject,v_environment
    FROM bursar.billing_auto_recharge_attempts
    WHERE id=p_attempt_id
    FOR UPDATE;

    IF NOT FOUND THEN RETURN false;
 END IF;

    v_metadata := CASE
        WHEN bursar.is_subject_pseudonymized(v_subject) THEN '{}'::jsonb
        ELSE COALESCE(p_metadata, '{}'::jsonb)
    END;
    v_failure_message := CASE
        WHEN bursar.is_subject_pseudonymized(v_subject) THEN NULL
        ELSE p_failure_message
    END;

    IF p_provider_attempt_id IS NOT NULL
       AND EXISTS (
           SELECT 1
           FROM bursar.billing_auto_recharge_attempts AS attempt
           WHERE attempt.id = p_attempt_id
             AND attempt.provider_attempt_id IS NOT NULL
             AND attempt.provider_attempt_id <> p_provider_attempt_id
       )
    THEN
        RETURN false;
    END IF;

    IF v_old=p_state THEN
        UPDATE bursar.billing_auto_recharge_attempts
        SET provider_attempt_id = COALESCE(
                provider_attempt_id,
                p_provider_attempt_id
            ),
            failure_code = COALESCE(p_failure_code, failure_code),
            failure_message = COALESCE(v_failure_message, failure_message),
            metadata = v_metadata
        WHERE id = p_attempt_id;

        RETURN true;
    END IF;

    IF (v_old,p_state) NOT IN (
        ('claimed','submitted'),
        ('submitted','processing'),
        ('submitted','action_required'),
        ('processing','succeeded'),
        ('processing','failed'),
        ('processing','unknown'),
        ('processing','action_required'),
        ('unknown','processing'),
        ('unknown','action_required'),
        ('unknown','succeeded'),
        ('unknown','failed'),
        ('action_required','processing'),
        ('action_required','succeeded'),
        ('action_required','failed'),
        ('claimed','canceled'),
        ('submitted','canceled'),
        ('processing','canceled'),
        ('unknown','canceled'),
        ('action_required','canceled')
    ) THEN
        RETURN false;

    END IF;

    UPDATE bursar.billing_auto_recharge_attempts
    SET state=p_state,
        provider_attempt_id=COALESCE(p_provider_attempt_id,provider_attempt_id),
        failure_code=COALESCE(p_failure_code,failure_code),
        failure_message=COALESCE(v_failure_message,failure_message),
        metadata=v_metadata
    WHERE id=p_attempt_id;

    -- A successful attempt remains disarmed until its credit grant updates the
    -- balance. Rearming here permits a second attempt before the first grant
    -- is posted.
    IF p_state='action_required' THEN
        UPDATE bursar.billing_auto_recharge_profiles
        SET state='paused',
            armed=false
        WHERE subject_id=v_subject
          AND provider_environment=v_environment
          AND enabled;

    ELSIF p_state='failed' THEN
        UPDATE bursar.billing_auto_recharge_profiles
        SET consecutive_failures=consecutive_failures+1,
            armed=CASE
                WHEN consecutive_failures+1>=max_consecutive_failures
                    THEN false
                ELSE true
            END,
            state=CASE
                WHEN consecutive_failures+1>=max_consecutive_failures
                    THEN 'paused'
                ELSE state
            END
        WHERE subject_id=v_subject
          AND provider_environment=v_environment
          AND enabled;

    ELSIF p_state='canceled' AND v_old <> 'action_required' THEN
        -- A cancellation before customer action is requested is safe to retry;
        -- action-required cancellation keeps the profile paused for review.
        UPDATE bursar.billing_auto_recharge_profiles
        SET armed=true
        WHERE subject_id=v_subject
          AND provider_environment=v_environment
          AND enabled
          AND state='active';

    ELSIF p_state='succeeded' THEN
        UPDATE bursar.billing_auto_recharge_profiles
        SET consecutive_failures=0,
            state='active',
            armed=billing_auto_recharge_profiles.armed OR COALESCE((
                SELECT account.balance >= billing_auto_recharge_profiles.rearm_above
                FROM bursar.credit_accounts AS account
                WHERE account.subject_id=v_subject
                  AND account.account_kind='personal'
            ), false)
        WHERE subject_id=v_subject
          AND provider_environment=v_environment
          AND enabled;

    END IF;

    RETURN true;

END $$;
