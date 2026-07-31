-- Current assignment changes, catalog rollouts, and subscription transitions.

CREATE FUNCTION bursar.assign_plan(
    p_subject_id uuid,
    p_plan_id uuid,
    p_starts_at timestamptz DEFAULT now(),
    p_ends_at timestamptz DEFAULT NULL
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
BEGIN
    IF p_starts_at IS NULL
       OR (
           p_ends_at IS NOT NULL
           AND p_ends_at <= p_starts_at
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

    IF FOUND AND p_starts_at > now() THEN
        INSERT INTO bursar.plan_assignment_changes(
            account_id,
            from_plan_id,
            to_plan_id,
            strategy,
            effective_at,
            reason
        )
        VALUES (
            v_account,
            v_current.plan_id,
            p_plan_id,
            'next_renewal',
            p_starts_at,
            'manual_schedule'
        )
        ON CONFLICT (account_id) WHERE state = 'scheduled' DO UPDATE
        SET from_plan_id = EXCLUDED.from_plan_id,
            to_plan_id = EXCLUDED.to_plan_id,
            strategy = EXCLUDED.strategy,
            effective_at = EXCLUDED.effective_at,
            reason = EXCLUDED.reason,
            error_message = NULL;

        RETURN true;
    END IF;

    PERFORM set_config('bursar.assignment_reason', 'manual_assignment', true);

    INSERT INTO bursar.account_plan_assignments(
        account_id,
        plan_id,
        catalog_revision_id,
        plan_key,
        revision_policy,
        source_type,
        starts_at,
        ends_at
    )
    VALUES (
        v_account,
        p_plan_id,
        v_plan.catalog_revision_id,
        v_plan.plan_key,
        v_plan.revision_policy,
        'manual',
        p_starts_at,
        p_ends_at
    )
    ON CONFLICT (account_id) DO UPDATE
    SET plan_id = EXCLUDED.plan_id,
        catalog_revision_id = EXCLUDED.catalog_revision_id,
        plan_key = EXCLUDED.plan_key,
        revision_policy = EXCLUDED.revision_policy,
        source_type = EXCLUDED.source_type,
        source_id = NULL,
        starts_at = EXCLUDED.starts_at,
        ends_at = EXCLUDED.ends_at;

    RETURN true;
END
$$;

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
    IF p_subject_id IS NULL OR NOT bursar.is_nonempty_text(p_reason) THEN
        RETURN false;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

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

CREATE FUNCTION bursar.schedule_catalog_plan_rollout(
    p_catalog_revision_id uuid
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    assignment_row record;
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

    FOR assignment_row IN
        SELECT
            assignment.account_id,
            assignment.plan_id AS from_plan_id,
            assignment.ends_at AS assignment_ends_at,
            assignment.revision_policy AS current_revision_policy,
            assignment.source_type,
            assignment.source_id,
            replacement.id AS to_plan_id,
            replacement.catalog_revision_id,
            replacement.plan_key,
            replacement.revision_policy
        FROM bursar.account_plan_assignments AS assignment
        JOIN bursar.catalog_plans AS replacement
          ON replacement.catalog_revision_id = p_catalog_revision_id
         AND replacement.plan_key = assignment.plan_key
        WHERE assignment.catalog_revision_id <> p_catalog_revision_id
          AND assignment.revision_policy <> 'pinned'
        ORDER BY assignment.account_id
        FOR UPDATE OF assignment
    LOOP
        IF assignment_row.current_revision_policy = 'immediate' THEN
            PERFORM set_config(
                'bursar.assignment_reason',
                'catalog_revision_immediate',
                true
            );

            UPDATE bursar.account_plan_assignments
            SET plan_id = assignment_row.to_plan_id,
                catalog_revision_id = assignment_row.catalog_revision_id,
                plan_key = assignment_row.plan_key,
                revision_policy = assignment_row.revision_policy,
                source_type = 'migration',
                starts_at = now(),
                ends_at = NULL
            WHERE account_id = assignment_row.account_id;

            v_count := v_count + 1;
        ELSE
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
                  'trialing', 'active', 'past_due', 'paused'
              )
            ORDER BY subscription.current_period_end DESC NULLS LAST
            LIMIT 1;

            v_effective_at := COALESCE(
                v_effective_at,
                assignment_row.assignment_ends_at,
                now()
            );

            INSERT INTO bursar.plan_assignment_changes(
                account_id,
                from_plan_id,
                to_plan_id,
                strategy,
                effective_at,
                reason
            )
            VALUES (
                assignment_row.account_id,
                assignment_row.from_plan_id,
                assignment_row.to_plan_id,
                'next_renewal',
                v_effective_at,
                'catalog_revision_next_renewal'
            )
            ON CONFLICT (account_id) WHERE state = 'scheduled' DO UPDATE
            SET from_plan_id = EXCLUDED.from_plan_id,
                to_plan_id = EXCLUDED.to_plan_id,
                strategy = EXCLUDED.strategy,
                effective_at = EXCLUDED.effective_at,
                reason = EXCLUDED.reason,
                error_message = NULL;

            v_count := v_count + 1;
        END IF;
    END LOOP;

    RETURN v_count;
END
$$;

CREATE FUNCTION bursar.apply_due_plan_assignment_changes(
    p_limit integer DEFAULT 100
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    change_row record;
    target_plan bursar.catalog_plans;
    v_count integer := 0;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'invalid batch size' USING ERRCODE = '22023';
    END IF;

    FOR change_row IN
        SELECT *
        FROM bursar.plan_assignment_changes
        WHERE state = 'scheduled'
          AND effective_at <= now()
        ORDER BY effective_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    LOOP
        SELECT *
        INTO target_plan
        FROM bursar.catalog_plans
        WHERE id = change_row.to_plan_id;

        PERFORM set_config(
            'bursar.assignment_reason',
            change_row.reason,
            true
        );

        UPDATE bursar.account_plan_assignments
        SET plan_id = target_plan.id,
            catalog_revision_id = target_plan.catalog_revision_id,
            plan_key = target_plan.plan_key,
            revision_policy = target_plan.revision_policy,
            source_type = 'migration',
            starts_at = change_row.effective_at,
            ends_at = NULL
        WHERE account_id = change_row.account_id
          AND plan_id = change_row.from_plan_id;

        IF FOUND THEN
            UPDATE bursar.plan_assignment_changes
            SET state = 'applied',
                applied_at = now()
            WHERE id = change_row.id;
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
        RETURN NULL;
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
        RETURN QUERY SELECT 0, false, NULL::uuid;
        RETURN;
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
            revision_policy = v_target.revision_policy,
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
    IF NOT bursar.is_nonempty_text(p_idempotency_key)
       OR p_effective_at IS NULL
       OR p_effective_behavior NOT IN ('immediate', 'renewal')
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
    target_offer bursar.catalog_offers;
    target_plan bursar.catalog_plans;
    v_account uuid;
BEGIN
    IF p_state NOT IN (
        'awaiting_payment', 'scheduled', 'applied', 'canceled', 'failed'
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

    IF change_row.state = p_state THEN
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

        UPDATE bursar.billing_subscriptions
        SET offer_id = change_row.to_offer_id,
            catalog_revision_id = change_row.to_catalog_revision_id
        WHERE id = change_row.subscription_id
          AND offer_id = change_row.from_offer_id
          AND catalog_revision_id = change_row.from_catalog_revision_id;

        IF NOT FOUND THEN
            RETURN false;
        END IF;

        SELECT *
        INTO target_offer
        FROM bursar.catalog_offers
        WHERE id = change_row.to_offer_id
          AND catalog_revision_id = change_row.to_catalog_revision_id;

        SELECT *
        INTO target_plan
        FROM bursar.catalog_plans
        WHERE catalog_revision_id = target_offer.catalog_revision_id
          AND plan_key = target_offer.plan_key;

        SELECT id
        INTO v_account
        FROM bursar.credit_accounts
        WHERE subject_id = subscription_row.subject_id
          AND account_kind = 'personal'
        FOR UPDATE;

        IF v_account IS NOT NULL THEN
            PERFORM set_config(
                'bursar.assignment_reason',
                'subscription_offer_change',
                true
            );

            INSERT INTO bursar.account_plan_assignments(
                account_id,
                plan_id,
                catalog_revision_id,
                plan_key,
                revision_policy,
                source_type,
                source_id,
                starts_at
            )
            VALUES (
                v_account,
                target_plan.id,
                target_plan.catalog_revision_id,
                target_plan.plan_key,
                target_plan.revision_policy,
                'subscription',
                subscription_row.id,
                COALESCE(change_row.effective_at, now())
            )
            ON CONFLICT (account_id) DO UPDATE
            SET plan_id = EXCLUDED.plan_id,
                catalog_revision_id = EXCLUDED.catalog_revision_id,
                plan_key = EXCLUDED.plan_key,
                revision_policy = EXCLUDED.revision_policy,
                source_type = EXCLUDED.source_type,
                source_id = EXCLUDED.source_id,
                starts_at = EXCLUDED.starts_at,
                ends_at = NULL;
        END IF;
    END IF;

    UPDATE bursar.billing_subscription_changes
    SET state = p_state,
        provider_operation_id = COALESCE(
            p_provider_operation_id,
            provider_operation_id
        ),
        error_message = CASE
            WHEN p_state = 'failed'
                THEN COALESCE(p_error_message, error_message)
            ELSE error_message
        END
    WHERE id = p_change_id;

    RETURN true;
END
$$;
