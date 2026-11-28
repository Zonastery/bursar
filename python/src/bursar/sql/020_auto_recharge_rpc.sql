-- Migration: 020_auto_recharge_rpc.sql
-- Purpose: Define idempotent auto-recharge claim and terminal-state transitions.
-- Depends on: Auto-recharge profiles, attempts, catalog policies, and credit accounts.
-- Security: SECURITY DEFINER RPCs require tenant context; claims use the current
--   provider environment and serialize threshold/window admission.

-- Shared eligibility predicate for trusted caller region and catalog availability.
CREATE FUNCTION bursar.offer_is_available(p_availability jsonb, p_region text)
RETURNS boolean
LANGUAGE sql STABLE
SET search_path TO ''
AS $$
    SELECT NOT (
        (p_availability->>'starts_at' IS NOT NULL AND (p_availability->>'starts_at')::timestamptz > now())
        OR (p_availability->>'ends_at' IS NOT NULL AND (p_availability->>'ends_at')::timestamptz <= now())
        OR (jsonb_array_length(COALESCE(p_availability->'regions', '[]'::jsonb)) > 0
            AND (p_region IS NULL OR NOT (p_availability->'regions' ? upper(btrim(p_region)))))
    )
$$;

-- Resolve the active policy only when the saved enrollment remains eligible.
-- Callers hold the shared catalog activation lock while using the result.
CREATE FUNCTION bursar.auto_recharge_eligible_policy(
    p_profile bursar.billing_auto_recharge_profiles
)
RETURNS SETOF bursar.catalog_auto_recharge_policies
LANGUAGE sql STABLE SET search_path TO '' AS $$
    SELECT policy.*
     FROM bursar.catalog_topups AS saved
     JOIN bursar.catalog_topups AS active
       ON active.tenant_id = saved.tenant_id AND active.topup_key = saved.topup_key
      AND ROW(active.amount_minor, active.currency, active.tax_behavior,
              active.credits_per_unit, active.bucket_key, active.min_quantity,
              active.max_quantity, active.expiry_policy, active.lot_behavior)
          IS NOT DISTINCT FROM
          ROW(saved.amount_minor, saved.currency, saved.tax_behavior,
              saved.credits_per_unit, saved.bucket_key, saved.min_quantity,
              saved.max_quantity, saved.expiry_policy, saved.lot_behavior)
     JOIN bursar.catalog_revisions AS revision
       ON revision.id = active.catalog_revision_id
      AND revision.tenant_id = active.tenant_id AND revision.status = 'active'
     JOIN bursar.catalog_auto_recharge_policies AS policy
       ON policy.catalog_revision_id = revision.id
      AND policy.tenant_id = revision.tenant_id
      AND active.topup_key = ANY(policy.eligible_topup_keys)
      AND (p_profile).quantity BETWEEN policy.quantity_min AND policy.quantity_max
      AND (p_profile).threshold BETWEEN policy.balance_min AND policy.balance_max
      AND active.amount_minor::numeric * (p_profile).quantity <= policy.max_charge_minor
     JOIN bursar.catalog_provider_refs AS saved_ref
       ON saved_ref.catalog_revision_id = saved.catalog_revision_id
      AND saved_ref.tenant_id = saved.tenant_id
      AND saved_ref.object_type = 'topup' AND saved_ref.object_key = saved.topup_key
      AND saved_ref.provider = (p_profile).provider
      AND saved_ref.provider_environment = (p_profile).provider_environment
     JOIN bursar.catalog_provider_refs AS active_ref
       ON active_ref.catalog_revision_id = active.catalog_revision_id
      AND active_ref.tenant_id = active.tenant_id
      AND active_ref.object_type = 'topup' AND active_ref.object_key = active.topup_key
      AND active_ref.provider = saved_ref.provider
      AND active_ref.provider_environment = saved_ref.provider_environment
      AND active_ref.lookup_type = saved_ref.lookup_type
      AND active_ref.lookup_value = saved_ref.lookup_value
     WHERE saved.tenant_id = (p_profile).tenant_id
       AND saved.id = (p_profile).topup_id
       AND saved.catalog_revision_id = (p_profile).catalog_revision_id
       AND bursar.offer_is_available(active.availability, NULL);
$$;

-- Calculate the operational window and usage once for both admission and status.
CREATE FUNCTION bursar.auto_recharge_policy_window(
    p_subject_id uuid, p_policy bursar.catalog_auto_recharge_policies
)
RETURNS TABLE (window_start timestamptz, window_end timestamptz, attempt_count integer)
LANGUAGE sql STABLE SET search_path TO '' AS $$
    SELECT period.window_start, period.window_end, (
        SELECT count(*)::integer FROM bursar.billing_auto_recharge_attempts AS attempt
        WHERE attempt.subject_id = p_subject_id
          AND attempt.provider_environment = bursar.current_provider_environment()
          AND attempt.created_at >= period.window_start AND attempt.created_at < period.window_end
          AND attempt.state IN ('submitted','processing','succeeded','action_required')
    )
    FROM bursar.policy_period_window(NULL, (p_policy).period_unit, (p_policy).period_count,
        (p_policy).period_anchor, (p_policy).period_timezone) AS period
$$;

CREATE FUNCTION bursar.get_auto_recharge_operational_window(p_subject_id uuid)
RETURNS TABLE (max_recharges integer, window_start timestamptz, window_end timestamptz, attempt_count integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT policy.max_purchases, period.window_start, period.window_end, period.attempt_count
    FROM bursar.catalog_auto_recharge_policies AS policy
    JOIN bursar.catalog_revisions AS revision
      ON revision.id = policy.catalog_revision_id AND revision.tenant_id = policy.tenant_id
     AND revision.status = 'active'
    CROSS JOIN LATERAL bursar.auto_recharge_policy_window(p_subject_id, policy) AS period
$$;

-- Claim or replay one recharge attempt under the active profile, provider
-- environment, balance threshold, cooldown, and window charge-count fences.

CREATE FUNCTION bursar.claim_auto_recharge_attempt(
    p_subject_id uuid,
    p_idempotency_key text,
    p_expected_product_id text,
    p_expected_quantity integer,
    p_expected_topup_key text
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
    v_policy bursar.catalog_auto_recharge_policies;

BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_nonempty_bounded_text(p_idempotency_key, 255)
       OR NOT bursar.is_nonempty_bounded_text(p_expected_product_id, 255)
       OR NOT bursar.is_nonempty_bounded_text(p_expected_topup_key, 255)
       OR p_expected_quantity IS NULL OR p_expected_quantity < 1
    THEN
        RETURN;
    END IF;

    -- Serialize admission with catalog activation, so the policy used below
    -- cannot be superseded until this transaction commits.
    PERFORM pg_advisory_xact_lock_shared(hashtextextended(
        'bursar.tenant:' || bursar.require_tenant_id()::text || ':catalog.active', 0
    ));

    -- Match credit-account rearm trigger lock order: balance before profile.
 SELECT balance INTO v_balance
 FROM bursar.credit_accounts
 WHERE subject_id=p_subject_id AND account_kind='personal'
 FOR UPDATE;

 v_balance:=COALESCE(v_balance,0);


    SELECT * INTO v_profile
    FROM bursar.billing_auto_recharge_profiles
    WHERE subject_id=p_subject_id
      AND provider_environment=v_environment
    FOR UPDATE;

 IF NOT FOUND THEN RETURN;
 END IF;

 -- Never return an attempt to a caller holding a different product or quantity.
 IF v_profile.quantity <> p_expected_quantity OR NOT EXISTS (
     SELECT 1
     FROM bursar.catalog_topups AS saved
     JOIN bursar.catalog_provider_refs AS reference
       ON reference.tenant_id = saved.tenant_id
      AND reference.catalog_revision_id = saved.catalog_revision_id
      AND reference.object_type = 'topup' AND reference.object_key = saved.topup_key
      AND reference.provider = v_profile.provider
      AND reference.provider_environment = v_environment
      AND reference.lookup_value = p_expected_product_id
     WHERE saved.tenant_id = v_profile.tenant_id AND saved.id = v_profile.topup_id
       AND saved.topup_key = p_expected_topup_key
       AND saved.catalog_revision_id = v_profile.catalog_revision_id
 ) THEN
     RETURN;
 END IF;

 SELECT * INTO v_attempt
 FROM bursar.billing_auto_recharge_attempts
 WHERE subject_id=p_subject_id
   AND provider_environment=v_environment
   AND idempotency_key=p_idempotency_key;

 IF FOUND THEN
     IF v_attempt.topup_id = v_profile.topup_id
        AND v_attempt.provider = v_profile.provider
        AND v_attempt.quantity = p_expected_quantity
     THEN
         RETURN NEXT v_attempt;
     END IF;
     RETURN;
 END IF;

 IF NOT v_profile.enabled OR NOT v_profile.armed
 OR v_profile.state<>'active' OR v_profile.topup_id IS NULL
 THEN
  RETURN;

 END IF;

 IF v_balance>=v_profile.threshold THEN RETURN;
 END IF;

 -- An unattended charge has no trusted region. Evaluate the currently active
 -- version of the saved top-up before creating a fresh provider attempt.
 SELECT * INTO v_policy FROM bursar.auto_recharge_eligible_policy(v_profile);
 IF NOT FOUND THEN
     RETURN;
 END IF;

    SELECT * INTO v_attempt
    FROM bursar.billing_auto_recharge_attempts
    WHERE subject_id=p_subject_id
      AND provider_environment=v_environment
      AND state IN ('claimed','submitted','processing','unknown','action_required')
    ORDER BY created_at DESC
    LIMIT 1;

    IF FOUND THEN
     IF v_attempt.topup_id = v_profile.topup_id
        AND v_attempt.provider = v_profile.provider
        AND v_attempt.quantity = p_expected_quantity
     THEN
         RETURN NEXT v_attempt;
     END IF;
     RETURN;
 END IF;

 IF v_profile.last_attempt_at IS NOT NULL
    AND v_profile.last_attempt_at
        + make_interval(secs=>v_policy.cooldown_seconds)>now()
 THEN
  RETURN;
 END IF;

    SELECT window_start, window_end, attempt_count
    INTO v_window_start, v_window_end, v_count
    FROM bursar.auto_recharge_policy_window(p_subject_id, v_policy);

    IF v_policy.max_purchases IS NOT NULL
       AND v_count>=v_policy.max_purchases
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
