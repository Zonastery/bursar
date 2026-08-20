-- Migration: 025_plan_migration_rpc.sql
-- Purpose: Define current plan assignment, catalog rollout, and subscription transitions.
-- Depends on: Catalog plans, account assignments, allowance/quota state, and subscriptions.
-- Security: SECURITY DEFINER state machines require tenant context and serialize
--   account transitions; each RPC defines its own pin, compatibility, and replay rules.
--
-- Contents
--   1. Direct assignment and revision pinning
--   2. Catalog rollout scheduling and application
--   3. Explicit account-plan migrations
--   4. Provider subscription transitions

-- ---------------------------------------------------------------------------
-- 1. Direct assignment and revision pinning
-- ---------------------------------------------------------------------------

-- Assign one exact plan to a tenant-owned subject, closing prior history and
-- carrying compatible allowance/quota state into the new effective interval.

CREATE FUNCTION bursar.apply_plan_assignment(
    p_subject_id uuid,
    p_plan_id uuid,
    p_starts_at timestamptz,
    p_ends_at timestamptz,
    p_source_type text,
    p_source_id uuid,
    p_reason text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
    v_plan bursar.catalog_plans;
    v_current bursar.account_plan_assignments;
    v_starts_at timestamptz := COALESCE(p_starts_at, now());
BEGIN
    IF p_subject_id IS NULL
       OR p_plan_id IS NULL
       OR p_source_type NOT IN ('manual', 'subscription')
       OR (p_source_type = 'manual' AND p_source_id IS NOT NULL)
       OR (p_source_type = 'subscription' AND p_source_id IS NULL)
       OR NOT bursar.is_nonempty_bounded_text(p_reason, 255)
       OR NOT bursar.is_finite_timestamptz(v_starts_at)
       OR (
           p_ends_at IS NOT NULL
           AND NOT bursar.is_finite_timestamptz(p_ends_at)
       )
       OR (p_ends_at IS NOT NULL
       AND p_ends_at <= v_starts_at
       )
    THEN
        RETURN false;
    END IF;

    SELECT *
    INTO v_plan
    FROM bursar.catalog_plans
    WHERE id = p_plan_id;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    SELECT *
    INTO v_current
    FROM bursar.account_plan_assignments
    WHERE account_id = v_account
    FOR UPDATE;

    -- Billing providers may re-provision the same commercial plan on every
    -- webhook. Keep catalog rollout timing authoritative: a same-key
    -- reassignment may refresh the assignment period, but it must not jump to
    -- the active catalog revision or clear a revision pin.
    IF v_current.account_id IS NOT NULL
       AND v_current.plan_key = v_plan.plan_key
    THEN
        PERFORM set_config(
            'bursar.assignment_reason',
            p_reason,
            true
        );

        UPDATE bursar.account_plan_assignments
        SET source_type = p_source_type,
            source_id = p_source_id,
            starts_at = COALESCE(p_starts_at, starts_at),
            ends_at = CASE
                WHEN p_starts_at IS NULL AND p_ends_at IS NULL
                    THEN ends_at
                ELSE p_ends_at
            END
        WHERE account_id = v_account
          AND ROW(source_type, source_id, starts_at, ends_at) IS DISTINCT FROM ROW(
              p_source_type,
              p_source_id,
              COALESCE(p_starts_at, starts_at),
              CASE
                  WHEN p_starts_at IS NULL AND p_ends_at IS NULL
                      THEN ends_at
                  ELSE p_ends_at
              END
          );

        RETURN true;
    END IF;

    -- Evaluate same-plan idempotency before scheduling. Otherwise a harmless
    -- refresh whose timestamp is slightly ahead of the database clock tries
    -- to insert a from-plan == to-plan transition, which is intentionally
    -- forbidden by plan_assignment_changes.
    IF FOUND
       AND p_starts_at IS NOT NULL
       AND p_starts_at > now()
    THEN
        INSERT INTO bursar.plan_assignment_changes(
            account_id,
            from_plan_id,
            to_plan_id,
            change_kind,
            strategy,
            effective_at,
            reason
        )
        VALUES (
            v_account,
            v_current.plan_id,
            p_plan_id,
            'manual',
            'next_renewal',
            p_starts_at,
            p_reason
        )
        ON CONFLICT (account_id, change_kind) WHERE state = 'scheduled' DO UPDATE
        SET from_plan_id = EXCLUDED.from_plan_id,
            to_plan_id = EXCLUDED.to_plan_id,
            strategy = EXCLUDED.strategy,
            effective_at = EXCLUDED.effective_at,
            reason = EXCLUDED.reason,
            error_message = NULL;

        RETURN true;
    END IF;

    PERFORM set_config('bursar.assignment_reason', p_reason, true);

    INSERT INTO bursar.account_plan_assignments(
        account_id,
        plan_id,
        catalog_revision_id,
        plan_key,
        catalog_revision_pinned,
        source_type,
        source_id,
        starts_at,
        ends_at
    )
    VALUES (
        v_account,
        p_plan_id,
        v_plan.catalog_revision_id,
        v_plan.plan_key,
        false,
        p_source_type,
        p_source_id,
        v_starts_at,
        p_ends_at
    )
    ON CONFLICT (account_id) DO UPDATE
    SET plan_id = EXCLUDED.plan_id,
        catalog_revision_id = EXCLUDED.catalog_revision_id,
        plan_key = EXCLUDED.plan_key,
        catalog_revision_pinned = false,
        source_type = EXCLUDED.source_type,
        source_id = EXCLUDED.source_id,
        starts_at = EXCLUDED.starts_at,
        ends_at = EXCLUDED.ends_at;

    RETURN true;
END
$$;

-- Keep manual callers on the stable four-argument API while the internal
-- assignment primitive records the durable business source in the same write.
CREATE FUNCTION bursar.assign_plan(
    p_subject_id uuid,
    p_plan_id uuid,
    p_starts_at timestamptz DEFAULT NULL,
    p_ends_at timestamptz DEFAULT NULL
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT bursar.apply_plan_assignment(
        p_subject_id,
        p_plan_id,
        p_starts_at,
        p_ends_at,
        'manual',
        NULL,
        'manual_assignment'
    )
$$;

-- Public, key-based assignment boundary used by the SDK. Resolution and
-- mutation happen in one database statement, avoiding a catalog activation
-- race between a client-side lookup and assign_plan.
-- Return the resolved immutable plan identity and resulting assignment state.
CREATE FUNCTION bursar.set_subject_plan(
    p_subject_id uuid,
    p_plan_key text,
    p_starts_at timestamptz DEFAULT NULL
)
RETURNS TABLE (
    user_id uuid,
    plan_id uuid,
    plan_key text,
    plan_assigned_at timestamptz,
    assignment_state text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan bursar.catalog_plans;
    v_assignment bursar.account_plan_assignments;
BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_nonempty_bounded_text(p_plan_key, 255)
       OR (
           p_starts_at IS NOT NULL
           AND NOT bursar.is_finite_timestamptz(p_starts_at)
       )
    THEN
        RAISE EXCEPTION 'subject id and plan key are required'
            USING ERRCODE = '22023';
    END IF;

    SELECT plan.*
    INTO v_plan
    FROM bursar.catalog_plans AS plan
    JOIN bursar.catalog_revisions AS revision
      ON revision.id = plan.catalog_revision_id
     AND revision.status = 'active'
    WHERE plan.plan_key = p_plan_key
    ORDER BY revision.revision_no DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown active plan key: %', p_plan_key
            USING ERRCODE = '22023';
    END IF;

    IF NOT bursar.assign_plan(p_subject_id, v_plan.id, p_starts_at) THEN
        RAISE EXCEPTION 'plan assignment was rejected'
            USING ERRCODE = 'P0001';
    END IF;

    UPDATE bursar.account_plan_assignments AS assignment
    SET source_type = 'manual',
        source_id = NULL
    FROM bursar.credit_accounts AS account
    WHERE assignment.account_id = account.id
      AND account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
      AND assignment.plan_key = v_plan.plan_key
      AND ROW(assignment.source_type, assignment.source_id)
          IS DISTINCT FROM ROW('manual'::text, NULL::uuid);

    IF p_starts_at IS NOT NULL AND p_starts_at > now() THEN
        RETURN QUERY SELECT
            p_subject_id,
            v_plan.id,
            v_plan.plan_key,
            p_starts_at,
            'scheduled'::text;
        RETURN;
    END IF;

    SELECT assignment.*
    INTO v_assignment
    FROM bursar.account_plan_assignments AS assignment
    JOIN bursar.credit_accounts AS account
      ON account.id = assignment.account_id
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'plan assignment committed without a current assignment'
            USING ERRCODE = 'P0001';
    END IF;

    RETURN QUERY SELECT
        p_subject_id,
        v_assignment.plan_id,
        v_assignment.plan_key,
        v_assignment.starts_at,
        'applied'::text;
END
$$;

-- End the subject's active plan assignment with an audited reason and effective time.
CREATE FUNCTION bursar.unassign_plan(
    p_subject_id uuid,
    p_reason text DEFAULT 'manual_unassignment'
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_nonempty_bounded_text(p_reason, 255)
    THEN
        RETURN false;
    END IF;

    -- Unassignment must never provision an account: account creation can run
    -- account_created grant programs and is not a revocation side effect.
    SELECT account.id
    INTO v_account
    FROM bursar.credit_accounts AS account
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN true;
    END IF;

    PERFORM 1
    FROM bursar.account_plan_assignments
    WHERE account_id = v_account
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN true;
    END IF;

    PERFORM set_config('bursar.assignment_reason', p_reason, true);

    DELETE FROM bursar.account_plan_assignments
    WHERE account_id = v_account;

    RETURN true;
END
$$;

-- End a subject plan assignment only while its durable source identity still
-- matches. This compare-and-delete boundary lets asynchronous billing
-- maintenance avoid revoking a later manual or replacement assignment.
CREATE FUNCTION bursar.unassign_plan_if_source(
    p_subject_id uuid,
    p_source_type text,
    p_source_id uuid,
    p_reason text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
BEGIN
    IF p_subject_id IS NULL
       OR p_source_id IS NULL
       OR p_source_type NOT IN ('manual', 'subscription', 'migration', 'system')
       OR NOT bursar.is_nonempty_bounded_text(p_reason, 255)
    THEN
        RETURN false;
    END IF;

    -- Revocation is never an account-provisioning boundary: creating a missing
    -- account here would also execute account_created grant programs.
    SELECT account.id
    INTO v_account
    FROM bursar.credit_accounts AS account
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    PERFORM set_config('bursar.assignment_reason', p_reason, true);

    DELETE FROM bursar.account_plan_assignments
    WHERE account_id = v_account
      AND source_type = p_source_type
      AND source_id = p_source_id;

    RETURN FOUND;
END
$$;

-- Replace a subscription-owned assignment only while its durable source still
-- matches. Applying the optional terminal plan in this same statement ensures
-- a failed replacement rolls the source deletion back.
CREATE FUNCTION bursar.replace_subscription_entitlement_if_source(
    p_subject_id uuid,
    p_subscription_id uuid,
    p_terminal_plan_key text,
    p_reason text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_replaced boolean;
BEGIN
    -- Keep the shared subject -> account lock order used by subscription
    -- upserts before unassign_plan_if_source locks the personal account.
    PERFORM 1
    FROM bursar.subjects AS subject
    WHERE subject.id = p_subject_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    v_replaced := bursar.unassign_plan_if_source(
        p_subject_id,
        'subscription',
        p_subscription_id,
        p_reason
    );

    IF NOT v_replaced THEN
        RETURN false;
    END IF;

    IF NULLIF(btrim(p_terminal_plan_key), '') IS NOT NULL THEN
        PERFORM 1
        FROM bursar.set_subject_plan(
            p_subject_id,
            btrim(p_terminal_plan_key),
            NULL
        );

        IF NOT FOUND THEN
            RAISE EXCEPTION 'terminal plan assignment returned no result'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;

    RETURN true;
END
$$;

-- Reconcile the entitlement effect of one exact persisted provider update.
-- The expected status and provider timestamp fence a caller that lost a race
-- to a newer webhook; provider environment and tenant authority come only
-- from transaction-local database context. The lock order matches provider
-- subscription upserts: subject -> personal account -> subscription.
CREATE FUNCTION bursar.reconcile_subscription_entitlement(
    p_subject_id uuid,
    p_subscription_id uuid,
    p_billing_event_id uuid,
    p_expected_status bursar.billing_subscription_status,
    p_expected_provider_updated_at timestamptz,
    p_plan_assigned_at timestamptz,
    p_apply_entitlement boolean,
    p_terminal_plan_key text,
    p_reason text
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_tenant_id uuid := bursar.require_tenant_id();
    v_environment text := bursar.current_provider_environment();
    v_subscription bursar.billing_subscriptions;
    v_account_id uuid;
    v_plan_id uuid;
    v_plan_key text;
    v_replaced boolean;
BEGIN
    IF p_subject_id IS NULL
       OR p_subscription_id IS NULL
       OR p_billing_event_id IS NULL
       OR p_expected_status IS NULL
       OR p_apply_entitlement IS NULL
       OR NOT bursar.is_finite_timestamptz(
           p_expected_provider_updated_at
       )
       OR (
           p_plan_assigned_at IS NOT NULL
           AND NOT bursar.is_finite_timestamptz(p_plan_assigned_at)
       )
       OR (
           p_terminal_plan_key IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(
               p_terminal_plan_key,
               255
           )
       )
       OR NOT bursar.is_nonempty_bounded_text(p_reason, 255)
    THEN
        RAISE EXCEPTION 'invalid subscription entitlement reconciliation'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1
    FROM bursar.billing_events AS event
    WHERE event.tenant_id = v_tenant_id
      AND event.id = p_billing_event_id
      AND event.provider_environment = v_environment
      AND (
          event.subject_id IS NULL
          OR event.subject_id = p_subject_id
      );

    IF NOT FOUND THEN
        RAISE EXCEPTION 'billing event does not belong to reconciliation context'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1
    FROM bursar.subjects AS subject
    WHERE subject.tenant_id = v_tenant_id
      AND subject.id = p_subject_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN 'stale';
    END IF;

    -- Read the exact version while holding the subject lock. Any competing
    -- upsert for this subject must wait, so this decides whether provisioning
    -- an absent personal account is an eligible side effect.
    SELECT subscription.*
    INTO v_subscription
    FROM bursar.billing_subscriptions AS subscription
    WHERE subscription.tenant_id = v_tenant_id
      AND subscription.id = p_subscription_id
      AND subscription.subject_id = p_subject_id
      AND subscription.provider_environment = v_environment
      AND subscription.status = p_expected_status
      AND subscription.provider_updated_at IS NOT DISTINCT FROM
          p_expected_provider_updated_at;

    IF NOT FOUND THEN
        RETURN 'stale';
    END IF;

    IF p_apply_entitlement
       AND p_expected_status IN ('active', 'trialing')
    THEN
        v_account_id := bursar.account_for_subject(p_subject_id);
    ELSE
        -- Revocation and preservation never provision an account because
        -- account creation can execute account-created grant programs.
        SELECT account.id
        INTO v_account_id
        FROM bursar.credit_accounts AS account
        WHERE account.tenant_id = v_tenant_id
          AND account.subject_id = p_subject_id
          AND account.account_kind = 'personal'
        FOR UPDATE;
    END IF;

    SELECT subscription.*
    INTO v_subscription
    FROM bursar.billing_subscriptions AS subscription
    WHERE subscription.tenant_id = v_tenant_id
      AND subscription.id = p_subscription_id
      AND subscription.subject_id = p_subject_id
      AND subscription.provider_environment = v_environment
      AND subscription.status = p_expected_status
      AND subscription.provider_updated_at IS NOT DISTINCT FROM
          p_expected_provider_updated_at
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN 'stale';
    END IF;

    PERFORM 1
    FROM bursar.billing_events AS event
    WHERE event.tenant_id = v_tenant_id
      AND event.id = p_billing_event_id
      AND event.provider = v_subscription.provider
      AND event.provider_environment = v_environment
      AND (
          event.subject_id IS NULL
          OR event.subject_id = p_subject_id
      );

    IF NOT FOUND THEN
        RAISE EXCEPTION 'billing event provider does not match subscription'
            USING ERRCODE = '22023';
    END IF;

    IF v_subscription.entitlement_provider_updated_at IS NOT NULL
       AND v_subscription.entitlement_provider_updated_at =
           p_expected_provider_updated_at
    THEN
        IF v_subscription.entitlement_billing_event_id = p_billing_event_id THEN
            RETURN v_subscription.entitlement_reconciliation_outcome;
        END IF;
        RETURN 'stale';
    END IF;

    IF v_subscription.entitlement_provider_updated_at IS NOT NULL
       AND v_subscription.entitlement_provider_updated_at >
           p_expected_provider_updated_at
    THEN
        RETURN 'stale';
    END IF;

    IF NOT p_apply_entitlement THEN
        UPDATE bursar.billing_subscriptions
        SET entitlement_provider_updated_at = p_expected_provider_updated_at,
            entitlement_billing_event_id = p_billing_event_id,
            entitlement_reconciliation_outcome = 'preserved'
        WHERE id = p_subscription_id;

        RETURN 'preserved';
    END IF;

    IF p_expected_status IN ('active', 'trialing') THEN
        SELECT plan.id, plan.plan_key
        INTO v_plan_id, v_plan_key
        FROM bursar.catalog_offers AS offer
        JOIN bursar.catalog_plans AS plan
          ON plan.catalog_revision_id = offer.catalog_revision_id
         AND plan.plan_key = offer.plan_key
        WHERE offer.id = v_subscription.offer_id
          AND offer.catalog_revision_id =
              v_subscription.catalog_revision_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'subscription offer has no matching plan'
                USING ERRCODE = 'P0001';
        END IF;

        IF NOT bursar.apply_plan_assignment(
            p_subject_id,
            v_plan_id,
            CASE
                WHEN p_plan_assigned_at IS NULL THEN NULL
                ELSE LEAST(p_plan_assigned_at, now())
            END,
            NULL,
            'subscription',
            p_subscription_id,
            p_reason
        )
        THEN
            RAISE EXCEPTION 'subscription plan assignment was rejected'
                USING ERRCODE = 'P0001';
        END IF;

        PERFORM 1
        FROM bursar.account_plan_assignments AS assignment
        WHERE assignment.account_id = v_account_id
          AND assignment.plan_key = v_plan_key
          AND assignment.source_type = 'subscription'
          AND assignment.source_id = p_subscription_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'subscription plan source was not committed'
                USING ERRCODE = 'P0001';
        END IF;

        UPDATE bursar.billing_entitlement_sources AS source
        SET selected = false,
            deselected_at = now()
        WHERE source.tenant_id = v_tenant_id
          AND source.subject_id = p_subject_id
          AND source.provider_environment = v_environment
          AND source.selected;

        INSERT INTO bursar.billing_entitlement_sources(
            subject_id,
            provider_environment,
            subscription_id,
            selected,
            selected_at
        )
        VALUES (
            p_subject_id,
            v_environment,
            p_subscription_id,
            true,
            now()
        )
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

        UPDATE bursar.billing_subscriptions
        SET entitlement_provider_updated_at = p_expected_provider_updated_at,
            entitlement_billing_event_id = p_billing_event_id,
            entitlement_reconciliation_outcome = 'applied'
        WHERE id = p_subscription_id;

        RETURN 'applied';
    END IF;

    IF p_expected_status IN (
        'incomplete_expired',
        'paused',
        'canceled',
        'unpaid',
        'expired'
    )
    THEN
        v_replaced := bursar.replace_subscription_entitlement_if_source(
            p_subject_id,
            p_subscription_id,
            p_terminal_plan_key,
            p_reason
        );

        UPDATE bursar.billing_entitlement_sources AS source
        SET selected = false,
            deselected_at = now()
        WHERE source.tenant_id = v_tenant_id
          AND source.subject_id = p_subject_id
          AND source.provider_environment = v_environment
          AND source.subscription_id = p_subscription_id
          AND source.selected;

        UPDATE bursar.billing_subscriptions
        SET entitlement_provider_updated_at = p_expected_provider_updated_at,
            entitlement_billing_event_id = p_billing_event_id,
            entitlement_reconciliation_outcome = CASE
                WHEN v_replaced THEN 'revoked'
                ELSE 'preserved'
            END
        WHERE id = p_subscription_id;

        RETURN CASE WHEN v_replaced THEN 'revoked' ELSE 'preserved' END;
    END IF;

    -- past_due retains access through grace, while incomplete has not earned
    -- positive entitlement. Neither state may replace a manual assignment.
    UPDATE bursar.billing_subscriptions
    SET entitlement_provider_updated_at = p_expected_provider_updated_at,
        entitlement_billing_event_id = p_billing_event_id,
        entitlement_reconciliation_outcome = 'preserved'
    WHERE id = p_subscription_id;

    RETURN 'preserved';
END
$$;

-- Atomically mark one still-current grace deadline and replace only the plan
-- assignment owned by that exact subscription. The subject check prevents a
-- caller from combining another subject with a tenant-visible subscription.
-- A true result means the grace marker committed; source mismatch or a missing
-- account intentionally does not turn a successfully marked expiry into false.
CREATE FUNCTION bursar.expire_subscription_grace_period(
    p_subject_id uuid,
    p_subscription_id uuid,
    p_expected_grace_ends_at timestamptz,
    p_expired_at timestamptz,
    p_terminal_plan_key text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_marked boolean;
BEGIN
    IF p_subject_id IS NULL OR p_subscription_id IS NULL THEN
        RETURN false;
    END IF;

    -- Match subscription upserts' subject -> account -> subscription order so
    -- webhook persistence and grace maintenance cannot deadlock each other.
    PERFORM 1
    FROM bursar.subjects AS subject
    WHERE subject.id = p_subject_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    PERFORM 1
    FROM bursar.credit_accounts AS account
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
    FOR UPDATE;

    PERFORM 1
    FROM bursar.billing_subscriptions AS subscription
    WHERE subscription.id = p_subscription_id
      AND subscription.subject_id = p_subject_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    v_marked := COALESCE(
        bursar.mark_subscription_grace_expired(
            p_subscription_id,
            p_expected_grace_ends_at,
            p_expired_at
        ),
        false
    );

    IF NOT v_marked THEN
        RETURN false;
    END IF;

    PERFORM bursar.replace_subscription_entitlement_if_source(
        p_subject_id,
        p_subscription_id,
        p_terminal_plan_key,
        'subscription_grace_expired'
    );

    RETURN true;
END
$$;

-- Pin or unpin a subject so automatic catalog rollouts respect explicit revision choice.
CREATE FUNCTION bursar.set_plan_revision_pin(
    p_subject_id uuid,
    p_pinned boolean
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account_id uuid;
BEGIN
    IF p_subject_id IS NULL OR p_pinned IS NULL THEN
        RETURN false;
    END IF;

    SELECT assignment.account_id
    INTO v_account_id
    FROM bursar.credit_accounts AS account
    JOIN bursar.account_plan_assignments AS assignment
      ON assignment.account_id = account.id
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
    FOR UPDATE OF assignment;

    IF v_account_id IS NULL THEN
        RETURN false;
    END IF;

    UPDATE bursar.account_plan_assignments
    SET catalog_revision_pinned = p_pinned
    WHERE account_id = v_account_id;

    IF p_pinned THEN
        UPDATE bursar.plan_assignment_changes
        SET state = 'canceled',
            error_message = NULL
        WHERE account_id = v_account_id
          AND change_kind = 'catalog_revision'
          AND state = 'scheduled';
    END IF;

    RETURN true;
END
$$;

-- Transfer compatible allowance and quota state between revisions of the same
-- logical plan, optionally rejecting incompatible policy changes.
CREATE FUNCTION bursar.carry_catalog_plan_revision_state(
    p_account_id uuid,
    p_from_plan_id uuid,
    p_to_plan_id uuid,
    p_require_compatible boolean DEFAULT TRUE
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_from bursar.catalog_plans;
    v_to bursar.catalog_plans;
    v_allowance_compatible boolean;
BEGIN
    IF p_require_compatible IS NULL THEN
        RAISE EXCEPTION 'compatibility requirement must be explicit'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_from
    FROM bursar.catalog_plans
    WHERE id = p_from_plan_id;

    SELECT * INTO v_to
    FROM bursar.catalog_plans
    WHERE id = p_to_plan_id;

    IF v_from.id IS NULL
       OR v_to.id IS NULL
       OR v_from.plan_key <> v_to.plan_key
    THEN
        RAISE EXCEPTION 'catalog revision state requires one stable plan key'
            USING ERRCODE = '22023';
    END IF;

    v_allowance_compatible := ROW(
        v_from.credit_allowance_reset_unit,
        v_from.credit_allowance_reset_count,
        v_from.credit_allowance_reset_anchor,
        v_from.credit_allowance_reset_timezone
    ) IS NOT DISTINCT FROM ROW(
        v_to.credit_allowance_reset_unit,
        v_to.credit_allowance_reset_count,
        v_to.credit_allowance_reset_anchor,
        v_to.credit_allowance_reset_timezone
    );

    IF p_require_compatible
       AND v_from.credit_allowance_amount IS NOT NULL
       AND v_to.credit_allowance_amount IS NOT NULL
       AND NOT v_allowance_compatible
    THEN
        RAISE EXCEPTION
            'immediate rollout cannot change the active allowance window for plan %; use next_renewal or new_assignments_only',
            v_to.plan_key
            USING ERRCODE = '22023';
    END IF;

    IF p_require_compatible
       AND EXISTS (
           SELECT 1
           FROM bursar.catalog_plan_quotas AS source_quota
           JOIN bursar.catalog_plan_quotas AS target_quota
             ON target_quota.catalog_revision_id =
                    v_to.catalog_revision_id
            AND target_quota.plan_key = v_to.plan_key
            AND target_quota.quota_key = source_quota.quota_key
           WHERE source_quota.catalog_revision_id =
                    v_from.catalog_revision_id
             AND source_quota.plan_key = v_from.plan_key
             AND ROW(
                    source_quota.operation_key,
                    source_quota.measure_key,
                    source_quota.window_policy
                 ) IS DISTINCT FROM ROW(
                    target_quota.operation_key,
                    target_quota.measure_key,
                    target_quota.window_policy
                 )
       )
    THEN
        RAISE EXCEPTION
            'immediate rollout cannot change an active quota identity or window for plan %; use next_renewal or new_assignments_only',
            v_to.plan_key
            USING ERRCODE = '22023';
    END IF;

    IF v_from.credit_allowance_amount IS NOT NULL
       AND v_to.credit_allowance_amount IS NOT NULL
       AND v_allowance_compatible
    THEN
        UPDATE bursar.allowance_windows AS allowance
        SET plan_id = v_to.id,
            catalog_revision_id = v_to.catalog_revision_id,
            allowance = v_to.credit_allowance_amount,
            policy_snapshot = v_to.definition->'credit_allowance'
        WHERE allowance.account_id = p_account_id
          AND allowance.plan_id = v_from.id
          AND allowance.catalog_revision_id = v_from.catalog_revision_id
          AND allowance.window_end > now();
    END IF;

    UPDATE bursar.quota_windows AS quota_window
    SET plan_id = v_to.id,
        catalog_revision_id = v_to.catalog_revision_id,
        operation_key = target_quota.operation_key,
        measure_key = target_quota.measure_key,
        quota_limit = target_quota.quota_limit,
        enforcement = target_quota.enforcement,
        policy_snapshot = target_quota.definition
    FROM bursar.catalog_plan_quotas AS source_quota
    JOIN bursar.catalog_plan_quotas AS target_quota
      ON target_quota.catalog_revision_id = v_to.catalog_revision_id
     AND target_quota.plan_key = v_to.plan_key
     AND target_quota.quota_key = source_quota.quota_key
     AND target_quota.operation_key = source_quota.operation_key
     AND target_quota.measure_key = source_quota.measure_key
     AND target_quota.window_policy = source_quota.window_policy
    WHERE source_quota.catalog_revision_id = v_from.catalog_revision_id
      AND source_quota.plan_key = v_from.plan_key
      AND quota_window.account_id = p_account_id
      AND quota_window.plan_id = v_from.id
      AND quota_window.catalog_revision_id = v_from.catalog_revision_id
      AND quota_window.quota_key = source_quota.quota_key
      AND quota_window.window_end > now();
END
$$;

-- ---------------------------------------------------------------------------
-- 2. Catalog rollout scheduling and application
-- ---------------------------------------------------------------------------

-- Return the closed JSON Schema accepted by catalog plan rollout manifests.
CREATE FUNCTION bursar.catalog_plan_rollout_schema()
RETURNS json
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $function$
    SELECT $schema$
    {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "plans": {
          "type": "object",
          "additionalProperties": {
            "type": "object",
            "additionalProperties": false,
            "properties": {
              "effective": {
                "enum": [
                  "immediate",
                  "next_renewal",
                  "new_assignments_only"
                ]
              },
              "include_pinned": {"type": "boolean"}
            },
            "required": ["effective"]
          }
        }
      }
    }
    $schema$::json
$function$;

-- Validate a newly activated revision's rollout manifest, apply explicit pin
-- overrides, and schedule or apply each eligible plan change.
CREATE FUNCTION bursar.schedule_catalog_plan_rollout(
    p_catalog_revision_id uuid,
    p_rollout jsonb DEFAULT '{"plans": {}}'::jsonb
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    assignment_row record;
    override_row record;
    v_effective_at timestamptz;
    v_count integer := 0;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM bursar.catalog_revisions
        WHERE id = p_catalog_revision_id
          AND status = 'active'
    )
    THEN
        RAISE EXCEPTION 'rollout target must be active'
            USING ERRCODE = '22023';
    END IF;

    IF p_rollout IS NULL
       OR NOT COALESCE(
           extensions.jsonb_matches_schema(
               bursar.catalog_plan_rollout_schema(),
               p_rollout
           ),
           false
       )
    THEN
        RAISE EXCEPTION 'invalid catalog rollout manifest'
            USING ERRCODE = '22023';
    END IF;

    FOR override_row IN
        SELECT entry.key AS plan_key, entry.value AS policy
        FROM jsonb_each(COALESCE(p_rollout->'plans', '{}'::jsonb))
            AS entry(key, value)
    LOOP
        IF NOT EXISTS (
               SELECT 1
               FROM bursar.catalog_plans AS plan
               WHERE plan.catalog_revision_id = p_catalog_revision_id
                 AND plan.plan_key = override_row.plan_key
           )
        THEN
            RAISE EXCEPTION 'invalid rollout policy for plan %',
                override_row.plan_key
                USING ERRCODE = '22023';
        END IF;

        IF override_row.policy->>'effective' = 'next_renewal'
           AND NOT EXISTS (
               SELECT 1
               FROM bursar.catalog_offers AS offer
               WHERE offer.catalog_revision_id = p_catalog_revision_id
                 AND offer.plan_key = override_row.plan_key
           )
        THEN
            RAISE EXCEPTION
                'next_renewal rollout requires a subscription offer for plan %',
                override_row.plan_key
                USING ERRCODE = '22023';
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM bursar.catalog_plans AS plan
        WHERE plan.catalog_revision_id = p_catalog_revision_id
          AND plan.default_rollout = 'next_renewal'
          AND NOT EXISTS (
              SELECT 1
              FROM bursar.catalog_offers AS offer
              WHERE offer.catalog_revision_id = p_catalog_revision_id
                AND offer.plan_key = plan.plan_key
          )
    )
    THEN
        RAISE EXCEPTION
            'next_renewal default rollout requires a subscription offer'
            USING ERRCODE = '22023';
    END IF;

    FOR assignment_row IN
        SELECT
            assignment.account_id,
            assignment.plan_id AS from_plan_id,
            assignment.ends_at AS assignment_ends_at,
            assignment.catalog_revision_pinned,
            replacement.id AS to_plan_id,
            replacement.catalog_revision_id,
            replacement.plan_key,
            COALESCE(
                p_rollout #>> ARRAY[
                    'plans', assignment.plan_key, 'effective'
                ],
                replacement.default_rollout
            ) AS rollout_strategy,
            COALESCE(
                (
                    p_rollout #>> ARRAY[
                        'plans', assignment.plan_key, 'include_pinned'
                    ]
                )::boolean,
                false
            ) AS include_pinned
        FROM bursar.account_plan_assignments AS assignment
        JOIN bursar.catalog_plans AS replacement
          ON replacement.catalog_revision_id = p_catalog_revision_id
         AND replacement.plan_key = assignment.plan_key
        WHERE assignment.catalog_revision_id <> p_catalog_revision_id
        ORDER BY assignment.account_id
        FOR UPDATE OF assignment
    LOOP
        IF assignment_row.catalog_revision_pinned
           AND NOT assignment_row.include_pinned
        THEN
            CONTINUE;
        END IF;

        IF assignment_row.rollout_strategy = 'new_assignments_only' THEN
            UPDATE bursar.plan_assignment_changes
            SET state = 'canceled',
                error_message = NULL
            WHERE account_id = assignment_row.account_id
              AND change_kind = 'catalog_revision'
              AND state = 'scheduled';
            CONTINUE;
        END IF;

        IF assignment_row.rollout_strategy = 'immediate' THEN
            PERFORM bursar.carry_catalog_plan_revision_state(
                assignment_row.account_id,
                assignment_row.from_plan_id,
                assignment_row.to_plan_id,
                true
            );

            PERFORM set_config(
                'bursar.assignment_reason',
                'catalog_revision_immediate',
                true
            );

            UPDATE bursar.account_plan_assignments
            SET plan_id = assignment_row.to_plan_id,
                catalog_revision_id = assignment_row.catalog_revision_id,
                plan_key = assignment_row.plan_key
            WHERE account_id = assignment_row.account_id;

            UPDATE bursar.plan_assignment_changes
            SET state = 'canceled',
                error_message = NULL
            WHERE account_id = assignment_row.account_id
              AND change_kind = 'catalog_revision'
              AND state = 'scheduled';

            UPDATE bursar.plan_assignment_changes
            SET from_plan_id = assignment_row.to_plan_id
            WHERE account_id = assignment_row.account_id
              AND change_kind = 'manual'
              AND state = 'scheduled'
              AND from_plan_id = assignment_row.from_plan_id
              AND to_plan_id <> assignment_row.to_plan_id;

            v_count := v_count + 1;
        ELSIF assignment_row.rollout_strategy = 'next_renewal' THEN
            v_effective_at := NULL;
            SELECT subscription.current_period_end
            INTO v_effective_at
            FROM bursar.billing_entitlement_sources AS source
            JOIN bursar.billing_subscriptions AS subscription
              ON subscription.id = source.subscription_id
            JOIN bursar.credit_accounts AS account
              ON account.subject_id = source.subject_id
             AND account.account_kind = 'personal'
            WHERE account.id = assignment_row.account_id
              AND source.selected
              AND source.provider_environment =
                  bursar.current_provider_environment()
              AND subscription.status IN (
                  'trialing', 'active', 'past_due'
              )
            ORDER BY subscription.current_period_end DESC NULLS LAST
            LIMIT 1;

            v_effective_at := COALESCE(
                v_effective_at,
                assignment_row.assignment_ends_at
            );

            IF v_effective_at IS NULL THEN
                RAISE EXCEPTION
                    'next renewal is unavailable for account % plan %',
                    assignment_row.account_id,
                    assignment_row.plan_key
                    USING ERRCODE = '22023';
            END IF;

            INSERT INTO bursar.plan_assignment_changes(
                account_id,
                from_plan_id,
                to_plan_id,
                change_kind,
                pin_overridden,
                strategy,
                effective_at,
                reason
            )
            VALUES (
                assignment_row.account_id,
                assignment_row.from_plan_id,
                assignment_row.to_plan_id,
                'catalog_revision',
                assignment_row.include_pinned,
                'next_renewal',
                v_effective_at,
                'catalog_revision_next_renewal'
            )
            ON CONFLICT (account_id, change_kind)
                WHERE state = 'scheduled' DO UPDATE
            SET from_plan_id = EXCLUDED.from_plan_id,
                to_plan_id = EXCLUDED.to_plan_id,
                strategy = EXCLUDED.strategy,
                pin_overridden = EXCLUDED.pin_overridden,
                effective_at = EXCLUDED.effective_at,
                reason = EXCLUDED.reason,
                error_message = NULL;

            v_count := v_count + 1;
        ELSE
            RAISE EXCEPTION 'invalid catalog rollout strategy'
                USING ERRCODE = '22023';
        END IF;
    END LOOP;

    RETURN v_count;
END
$$;

-- Claim and apply a bounded SKIP LOCKED batch of due plan changes, preserving
-- assignment history and compatible policy-window state.
CREATE FUNCTION bursar.apply_due_plan_assignment_changes(
    p_limit integer DEFAULT 100
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_change_id bigint;
    change_row record;
    current_assignment bursar.account_plan_assignments;
    target_plan bursar.catalog_plans;
    v_count integer := 0;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'invalid batch size' USING ERRCODE = '22023';
    END IF;

    FOR v_change_id IN
        SELECT pending_change.id
        FROM bursar.plan_assignment_changes AS pending_change
        JOIN bursar.account_plan_assignments AS assignment
          ON assignment.account_id = pending_change.account_id
        WHERE pending_change.state = 'scheduled'
          AND pending_change.effective_at <= now()
        ORDER BY
            pending_change.effective_at,
            CASE pending_change.change_kind
                WHEN 'catalog_revision' THEN 0
                ELSE 1
            END,
            pending_change.id
        FOR UPDATE OF assignment SKIP LOCKED
        LIMIT p_limit
    LOOP
        SELECT *
        INTO change_row
        FROM bursar.plan_assignment_changes
        WHERE id = v_change_id
          AND state = 'scheduled'
          AND effective_at <= now()
        FOR UPDATE SKIP LOCKED;

        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        SELECT *
        INTO target_plan
        FROM bursar.catalog_plans
        WHERE id = change_row.to_plan_id;

        SELECT *
        INTO current_assignment
        FROM bursar.account_plan_assignments
        WHERE account_id = change_row.account_id
        FOR UPDATE;

        IF target_plan.id IS NULL
           OR current_assignment.account_id IS NULL
           OR current_assignment.plan_id <> change_row.from_plan_id
           OR (
               change_row.change_kind = 'catalog_revision'
               AND current_assignment.plan_key <> target_plan.plan_key
           )
        THEN
            UPDATE bursar.plan_assignment_changes
            SET state = 'failed',
                error_message = 'current assignment changed before apply'
            WHERE id = change_row.id;
            CONTINUE;
        END IF;

        IF change_row.change_kind = 'catalog_revision' THEN
            PERFORM bursar.carry_catalog_plan_revision_state(
                change_row.account_id,
                change_row.from_plan_id,
                change_row.to_plan_id,
                false
            );
        END IF;

        PERFORM set_config(
            'bursar.assignment_reason',
            change_row.reason,
            true
        );

        IF change_row.change_kind = 'catalog_revision' THEN
            UPDATE bursar.account_plan_assignments
            SET plan_id = target_plan.id,
                catalog_revision_id = target_plan.catalog_revision_id,
                plan_key = target_plan.plan_key
            WHERE account_id = change_row.account_id
              AND plan_id = change_row.from_plan_id;
        ELSE
            UPDATE bursar.account_plan_assignments
            SET plan_id = target_plan.id,
                catalog_revision_id = target_plan.catalog_revision_id,
                plan_key = target_plan.plan_key,
                catalog_revision_pinned = false,
                source_type = 'migration',
                source_id = NULL,
                starts_at = change_row.effective_at,
                ends_at = NULL
            WHERE account_id = change_row.account_id
              AND plan_id = change_row.from_plan_id;
        END IF;

        IF FOUND THEN
            UPDATE bursar.plan_assignment_changes
            SET state = 'applied',
                applied_at = now()
            WHERE id = change_row.id;

            IF change_row.change_kind = 'catalog_revision' THEN
                UPDATE bursar.plan_assignment_changes
                SET from_plan_id = target_plan.id
                WHERE account_id = change_row.account_id
                  AND change_kind = 'manual'
                  AND state = 'scheduled'
                  AND from_plan_id = change_row.from_plan_id
                  AND to_plan_id <> target_plan.id;
            ELSE
                UPDATE bursar.plan_assignment_changes
                SET state = 'canceled',
                    error_message = NULL
                WHERE account_id = change_row.account_id
                  AND change_kind = 'catalog_revision'
                  AND state = 'scheduled';
            END IF;

            v_count := v_count + 1;
        ELSE
            UPDATE bursar.plan_assignment_changes
            SET state = 'failed',
                error_message = 'current assignment changed before apply'
            WHERE id = change_row.id;
        END IF;
    END LOOP;

    RETURN v_count;
END
$$;

-- ---------------------------------------------------------------------------
-- 3. Explicit account-plan migrations
-- ---------------------------------------------------------------------------

-- Create a fresh tenant-scoped migration from an optional source to a distinct target.
CREATE FUNCTION bursar.start_plan_migration(
    p_from_plan_id uuid,
    p_to_plan_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_id uuid;
BEGIN
    IF p_to_plan_id IS NULL
       OR p_from_plan_id IS NOT DISTINCT FROM p_to_plan_id
       OR NOT EXISTS (
           SELECT 1 FROM bursar.catalog_plans WHERE id = p_to_plan_id
       )
       OR (
           p_from_plan_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
               FROM bursar.catalog_plans
               WHERE id = p_from_plan_id
           )
       )
    THEN
        RAISE EXCEPTION 'invalid source or target plan for migration'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO bursar.credit_plan_migrations(
        from_plan_id,
        to_plan_id,
        strategy,
        effective_at
    )
    VALUES (p_from_plan_id, p_to_plan_id, 'immediate', now())
    RETURNING id INTO v_id;

    RETURN v_id;
END
$$;

-- Advance one migration through a bounded account batch using its stable cursor.
CREATE FUNCTION bursar.migrate_plan_batch(
    p_migration_id uuid,
    p_batch_size integer DEFAULT 100
)
RETURNS TABLE (
    migrated integer,
    done boolean,
    next_cursor uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_migration bursar.credit_plan_migrations;
    v_target bursar.catalog_plans;
    v_count integer := 0;
    assignment_row record;
BEGIN
    IF p_batch_size IS NULL OR p_batch_size < 1 OR p_batch_size > 1000 THEN
        RAISE EXCEPTION 'invalid batch size' USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO v_migration
    FROM bursar.credit_plan_migrations
    WHERE id = p_migration_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown plan migration: %', p_migration_id
            USING ERRCODE = '22023';
    END IF;

    IF v_migration.status <> 'running' THEN
        RETURN QUERY
        SELECT
            0,
            v_migration.status = 'completed',
            v_migration.cursor_account_id;
        RETURN;
    END IF;

    SELECT *
    INTO v_target
    FROM bursar.catalog_plans
    WHERE id = v_migration.to_plan_id;

    FOR assignment_row IN
        SELECT assignment.account_id
        FROM bursar.account_plan_assignments AS assignment
        WHERE (
            v_migration.from_plan_id IS NULL
            OR assignment.plan_id = v_migration.from_plan_id
        )
          AND (
              v_migration.cursor_account_id IS NULL
              OR assignment.account_id > v_migration.cursor_account_id
          )
        ORDER BY assignment.account_id
        LIMIT p_batch_size
        FOR UPDATE
    LOOP
        PERFORM set_config(
            'bursar.assignment_reason',
            'manual_plan_migration',
            true
        );

        UPDATE bursar.account_plan_assignments
        SET plan_id = v_target.id,
            catalog_revision_id = v_target.catalog_revision_id,
            plan_key = v_target.plan_key,
            catalog_revision_pinned = false,
            source_type = 'migration',
            starts_at = now(),
            ends_at = NULL
        WHERE account_id = assignment_row.account_id;

        v_count := v_count + 1;
        v_migration.cursor_account_id := assignment_row.account_id;
    END LOOP;

    UPDATE bursar.credit_plan_migrations
    SET cursor_account_id = v_migration.cursor_account_id,
        migrated_count = migrated_count + v_count,
        status = CASE
            WHEN v_count < p_batch_size THEN 'completed'
            ELSE status
        END
    WHERE id = p_migration_id;

    RETURN QUERY
    SELECT
        v_count,
        v_count < p_batch_size,
        v_migration.cursor_account_id;
END
$$;

-- ---------------------------------------------------------------------------
-- 4. Provider subscription transitions
-- ---------------------------------------------------------------------------

-- Create or replay one transition for a subscription selected by tenant-scoped UUID,
-- fencing effective behavior and target offer with an immutable idempotency key.
CREATE FUNCTION bursar.open_subscription_change(
    p_subscription_id uuid,
    p_to_offer_id uuid,
    p_effective_at timestamptz,
    p_effective_behavior text,
    p_idempotency_key text,
    p_proration_behavior text DEFAULT 'provider_default'
)
RETURNS TABLE (
    change_id bigint,
    state text,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    subscription_row bursar.billing_subscriptions;
    target_offer bursar.catalog_offers;
    change_row bursar.billing_subscription_changes;
BEGIN
    IF p_subscription_id IS NULL
       OR p_to_offer_id IS NULL
       OR NOT bursar.is_nonempty_bounded_text(p_idempotency_key, 255)
       OR NOT bursar.is_finite_timestamptz(p_effective_at)
       OR p_effective_behavior IS NULL
       OR p_effective_behavior NOT IN ('immediate', 'renewal')
       OR p_proration_behavior IS NULL
       OR p_proration_behavior NOT IN (
           'provider_default',
           'invoice_immediately',
           'none'
       )
    THEN
        RETURN QUERY SELECT NULL::bigint, NULL::text, 'invalid_request';
        RETURN;
    END IF;

    SELECT *
    INTO subscription_row
    FROM bursar.billing_subscriptions
    WHERE id = p_subscription_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::bigint, NULL::text, 'missing_subscription';
        RETURN;
    END IF;

    SELECT offer.*
    INTO target_offer
    FROM bursar.catalog_offers AS offer
    JOIN bursar.catalog_revisions AS revision
      ON revision.id = offer.catalog_revision_id
     AND revision.status = 'active'
    WHERE offer.id = p_to_offer_id;

    IF NOT FOUND OR subscription_row.offer_id = p_to_offer_id THEN
        RETURN QUERY SELECT NULL::bigint, NULL::text, 'invalid_target_offer';
        RETURN;
    END IF;

    SELECT *
    INTO change_row
    FROM bursar.billing_subscription_changes
    WHERE subscription_id = p_subscription_id
      AND idempotency_key = p_idempotency_key
    FOR UPDATE;

    IF FOUND THEN
        IF change_row.from_offer_id <> subscription_row.offer_id
           OR change_row.to_offer_id <> p_to_offer_id
           OR change_row.effective_at IS DISTINCT FROM p_effective_at
           OR change_row.effective_behavior <> p_effective_behavior
           OR change_row.proration_behavior <>
              p_proration_behavior
        THEN
            RETURN QUERY
            SELECT NULL::bigint, NULL::text, 'idempotency_conflict';
        ELSE
            RETURN QUERY
            SELECT change_row.id, change_row.state, NULL::text;
        END IF;
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM bursar.billing_subscription_changes
        WHERE subscription_id = p_subscription_id
          AND billing_subscription_changes.state IN ('awaiting_payment', 'scheduled')
    )
    THEN
        RETURN QUERY SELECT NULL::bigint, NULL::text, 'open_change_exists';
        RETURN;
    END IF;

    INSERT INTO bursar.billing_subscription_changes(
        subscription_id,
        state,
        from_offer_id,
        from_catalog_revision_id,
        to_offer_id,
        to_catalog_revision_id,
        effective_at,
        effective_behavior,
        proration_behavior,
        idempotency_key
    )
    VALUES (
        p_subscription_id,
        CASE
            WHEN p_effective_behavior = 'renewal' THEN 'scheduled'
            ELSE 'awaiting_payment'
        END,
        subscription_row.offer_id,
        subscription_row.catalog_revision_id,
        target_offer.id,
        target_offer.catalog_revision_id,
        p_effective_at,
        p_effective_behavior,
        p_proration_behavior,
        p_idempotency_key
    )
    RETURNING * INTO change_row;

    RETURN QUERY SELECT change_row.id, change_row.state, NULL::text;
END
$$;

-- Advance an open subscription transition through its legal provider outcome,
-- storing the latest non-null provider operation ID and bounded failure detail.
CREATE FUNCTION bursar.advance_subscription_change(
    p_change_id bigint,
    p_state text,
    p_provider_operation_id text DEFAULT NULL,
    p_error_message text DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    change_row bursar.billing_subscription_changes;
    subscription_row bursar.billing_subscriptions;
    v_subject uuid;
    v_error_message text;
BEGIN
    IF p_change_id IS NULL
       OR p_state IS NULL
       OR p_state NOT IN (
        'awaiting_payment', 'scheduled', 'applied', 'canceled', 'failed'
    )
       OR (
           p_provider_operation_id IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(
               p_provider_operation_id,
               255
           )
       )
       OR (
           p_error_message IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_error_message, 8192)
       )
    THEN
        RETURN false;
    END IF;

    SELECT *
    INTO change_row
    FROM bursar.billing_subscription_changes
    WHERE id = p_change_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    SELECT subscription.subject_id
    INTO v_subject
    FROM bursar.billing_subscriptions AS subscription
    WHERE subscription.id = change_row.subscription_id;

    v_error_message := CASE
        WHEN bursar.is_subject_pseudonymized(v_subject) THEN NULL
        ELSE p_error_message
    END;

    IF p_provider_operation_id IS NOT NULL
       AND change_row.provider_operation_id IS NOT NULL
       AND change_row.provider_operation_id <> p_provider_operation_id
    THEN
        RETURN false;
    END IF;

    IF change_row.state = p_state THEN
        UPDATE bursar.billing_subscription_changes
        SET provider_operation_id = COALESCE(
                provider_operation_id,
                p_provider_operation_id
            ),
            error_message = CASE
                WHEN p_state = 'failed'
                    THEN COALESCE(v_error_message, error_message)
                ELSE error_message
            END
        WHERE id = p_change_id;

        RETURN true;
    END IF;

    IF (change_row.state, p_state) NOT IN (
        ('awaiting_payment', 'scheduled'),
        -- Providers such as Dodo confirm an immediate plan change with a
        -- single subscription.plan_changed event. That event is both the
        -- payment confirmation and the effective subscription transition,
        -- so it must be able to advance the persisted change directly.
        ('awaiting_payment', 'applied'),
        ('awaiting_payment', 'canceled'),
        ('awaiting_payment', 'failed'),
        ('scheduled', 'applied'),
        ('scheduled', 'canceled'),
        ('scheduled', 'failed')
    )
    THEN
        RETURN false;
    END IF;

    IF p_state = 'applied' THEN
        SELECT *
        INTO subscription_row
        FROM bursar.billing_subscriptions
        WHERE id = change_row.subscription_id
        FOR UPDATE;

        IF ROW(
            subscription_row.offer_id,
            subscription_row.catalog_revision_id
        ) IS DISTINCT FROM ROW(
            change_row.from_offer_id,
            change_row.from_catalog_revision_id
        )
           AND ROW(
               subscription_row.offer_id,
               subscription_row.catalog_revision_id
           ) IS DISTINCT FROM ROW(
               change_row.to_offer_id,
               change_row.to_catalog_revision_id
           )
        THEN
            RETURN false;
        END IF;

        UPDATE bursar.billing_subscriptions
        SET offer_id = change_row.to_offer_id,
            catalog_revision_id = change_row.to_catalog_revision_id
        WHERE id = change_row.subscription_id
          AND offer_id = change_row.from_offer_id
          AND catalog_revision_id = change_row.from_catalog_revision_id;
    END IF;

    UPDATE bursar.billing_subscription_changes
    SET state = p_state,
        provider_operation_id = COALESCE(
            p_provider_operation_id,
            provider_operation_id
        ),
        error_message = CASE
            WHEN p_state = 'failed'
                THEN COALESCE(v_error_message, error_message)
            ELSE error_message
        END
    WHERE id = p_change_id;

    RETURN true;
END
$$;
