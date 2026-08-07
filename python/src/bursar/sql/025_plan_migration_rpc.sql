-- Current assignment changes, catalog rollouts, and subscription transitions.

CREATE FUNCTION bursar.assign_plan(
    p_subject_id uuid,
    p_plan_id uuid,
    p_starts_at timestamptz DEFAULT NULL,
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
    v_starts_at timestamptz := COALESCE(p_starts_at, now());
BEGIN
    IF p_ends_at IS NOT NULL
       AND p_ends_at <= v_starts_at
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
            'manual_schedule'
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

    -- Billing providers may re-provision the same commercial plan on every
    -- webhook. Keep catalog rollout timing authoritative: a same-key
    -- reassignment may refresh the assignment period, but it must not jump to
    -- the active catalog revision or clear a revision pin.
    IF v_current.account_id IS NOT NULL
       AND v_current.plan_key = v_plan.plan_key
    THEN
        PERFORM set_config(
            'bursar.assignment_reason',
            'same_plan_reassignment',
            true
        );

        UPDATE bursar.account_plan_assignments
        SET starts_at = COALESCE(p_starts_at, starts_at),
            ends_at = CASE
                WHEN p_starts_at IS NULL AND p_ends_at IS NULL
                    THEN ends_at
                ELSE p_ends_at
            END
        WHERE account_id = v_account
          AND ROW(starts_at, ends_at) IS DISTINCT FROM ROW(
              COALESCE(p_starts_at, starts_at),
              CASE
                  WHEN p_starts_at IS NULL AND p_ends_at IS NULL
                      THEN ends_at
                  ELSE p_ends_at
              END
          );

        RETURN true;
    END IF;

    PERFORM set_config('bursar.assignment_reason', 'manual_assignment', true);

    INSERT INTO bursar.account_plan_assignments(
        account_id,
        plan_id,
        catalog_revision_id,
        plan_key,
        catalog_revision_pinned,
        source_type,
        starts_at,
        ends_at
    )
    VALUES (
        v_account,
        p_plan_id,
        v_plan.catalog_revision_id,
        v_plan.plan_key,
        false,
        'manual',
        v_starts_at,
        p_ends_at
    )
    ON CONFLICT (account_id) DO UPDATE
    SET plan_id = EXCLUDED.plan_id,
        catalog_revision_id = EXCLUDED.catalog_revision_id,
        plan_key = EXCLUDED.plan_key,
        catalog_revision_pinned = false,
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
                  'trialing', 'active', 'past_due', 'paused'
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
       OR (
           p_provider_operation_id IS NOT NULL
           AND NOT bursar.is_nonempty_text(p_provider_operation_id)
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
                catalog_revision_pinned,
                source_type,
                source_id,
                starts_at
            )
            VALUES (
                v_account,
                target_plan.id,
                target_plan.catalog_revision_id,
                target_plan.plan_key,
                false,
                'subscription',
                subscription_row.id,
                COALESCE(change_row.effective_at, now())
            )
            ON CONFLICT (account_id) DO UPDATE
            SET plan_id = EXCLUDED.plan_id,
                catalog_revision_id = EXCLUDED.catalog_revision_id,
                plan_key = EXCLUDED.plan_key,
                catalog_revision_pinned = false,
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
