-- Credit reservation and concurrent-admission leases.

CREATE FUNCTION bursar.jsonb_nonnegative_numeric(
    p_value jsonb
)
RETURNS numeric
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_value numeric;
BEGIN
    IF p_value IS NULL
       OR jsonb_typeof(p_value) NOT IN ('number', 'string')
    THEN
        RETURN NULL;
    END IF;

    BEGIN
        v_value := (p_value #>> '{}')::numeric;
    EXCEPTION
        WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN NULL;
    END;

    IF NOT bursar.is_finite_numeric(v_value) OR v_value < 0 THEN
        RETURN NULL;
    END IF;
    RETURN v_value;
END
$$;

CREATE FUNCTION bursar.subject_has_entitlement(
    p_subject_id uuid,
    p_feature text,
    p_at timestamptz DEFAULT now()
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH assignment AS (
        SELECT
            plan.catalog_revision_id,
            plan.plan_key
        FROM bursar.credit_accounts AS account
        JOIN bursar.account_plan_assignments AS assigned
          ON assigned.account_id = account.id
        JOIN bursar.catalog_plans AS plan
          ON plan.id = assigned.plan_id
         AND plan.catalog_revision_id = assigned.catalog_revision_id
        WHERE account.subject_id = p_subject_id
          AND account.account_kind = 'personal'
          AND assigned.starts_at <= p_at
          AND (assigned.ends_at IS NULL OR assigned.ends_at > p_at)
        LIMIT 1
    ),
    context AS (
        SELECT
            COALESCE(assignment.catalog_revision_id, active.id)
                AS catalog_revision_id,
            assignment.plan_key
        FROM (
            SELECT id
            FROM bursar.catalog_revisions
            WHERE status = 'active'
            LIMIT 1
        ) AS active
        FULL JOIN assignment ON true
        LIMIT 1
    )
    SELECT COALESCE(
        bool_or(
            COALESCE(plan_feature.feature_value, feature.default_value)
                NOT IN ('null'::jsonb, 'false'::jsonb)
        ),
        false
    )
    FROM context
    JOIN bursar.catalog_entitlement_features AS feature
      ON feature.catalog_revision_id = context.catalog_revision_id
     AND feature.feature_key = p_feature
    LEFT JOIN bursar.catalog_plan_features AS plan_feature
      ON plan_feature.catalog_revision_id = context.catalog_revision_id
     AND plan_feature.plan_key = context.plan_key
     AND plan_feature.feature_key = feature.feature_key
    WHERE bursar.is_nonempty_text(p_feature)
$$;

CREATE FUNCTION bursar.quota_policy_window(
    p_anchor_at timestamptz,
    p_policy jsonb
)
RETURNS TABLE(
    window_start timestamptz,
    window_end timestamptz,
    is_rolling boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_type text := p_policy->>'type';
    v_unit text;
    v_count integer;
    v_anchor text;
    v_timezone text;
BEGIN
    IF v_type = 'rolling' THEN
        v_unit := p_policy->'duration'->>'unit';
        v_count := (p_policy->'duration'->>'count')::integer;
        v_anchor := 'rolling';
        v_timezone := 'UTC';
        is_rolling := true;
    ELSIF v_type = 'plan_assignment' THEN
        v_unit := p_policy->'interval'->>'unit';
        v_count := (p_policy->'interval'->>'count')::integer;
        v_anchor := 'plan_assignment';
        v_timezone := COALESCE(p_policy->>'timezone', 'UTC');
        is_rolling := false;
    ELSIF v_type = 'calendar' THEN
        v_unit := p_policy->>'unit';
        v_count := COALESCE((p_policy->>'count')::integer, 1);
        v_anchor := 'calendar';
        v_timezone := COALESCE(p_policy->>'timezone', 'UTC');
        is_rolling := false;
    ELSE
        RAISE EXCEPTION 'invalid quota window policy'
            USING ERRCODE = '22023';
    END IF;

    SELECT period.window_start, period.window_end
    INTO window_start, window_end
    FROM bursar.policy_period_window(
        p_anchor_at,
        v_unit,
        v_count,
        v_anchor,
        v_timezone
    ) AS period;
    RETURN NEXT;
END
$$;

CREATE FUNCTION bursar.check_operation_quotas(
    p_account_id uuid,
    p_plan_id uuid,
    p_catalog_revision_id uuid,
    p_operation text,
    p_measures jsonb,
    p_idempotency_key text,
    p_enforce boolean DEFAULT true,
    p_exclude_lease_id uuid DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan record;
    v_quota record;
    v_window record;
    v_amount numeric;
    v_consumed numeric;
    v_reserved numeric;
    v_window_id uuid;
    v_reference_at timestamptz := now();
BEGIN
    IF p_plan_id IS NULL THEN
        RETURN NULL;
    END IF;

    IF p_exclude_lease_id IS NOT NULL THEN
        SELECT lease.created_at
        INTO v_reference_at
        FROM bursar.credit_leases AS lease
        WHERE lease.id = p_exclude_lease_id
          AND lease.account_id = p_account_id
          AND lease.plan_id = p_plan_id
          AND lease.catalog_revision_id = p_catalog_revision_id;

        IF NOT FOUND THEN
            RETURN 'missing_lease';
        END IF;
    END IF;

    SELECT plan.plan_key, assignment.starts_at
    INTO v_plan
    FROM bursar.catalog_plans AS plan
    JOIN LATERAL (
        SELECT context.starts_at, context.ends_at
        FROM (
            SELECT
                current_assignment.starts_at,
                current_assignment.ends_at
            FROM bursar.account_plan_assignments
                AS current_assignment
            WHERE current_assignment.account_id = p_account_id
              AND current_assignment.plan_id = p_plan_id
              AND current_assignment.catalog_revision_id =
                  p_catalog_revision_id
            UNION ALL
            SELECT
                historical_assignment.starts_at,
                historical_assignment.ends_at
            FROM bursar.account_plan_assignment_history
                AS historical_assignment
            WHERE historical_assignment.account_id = p_account_id
              AND historical_assignment.plan_id = p_plan_id
              AND historical_assignment.catalog_revision_id =
                  p_catalog_revision_id
        ) AS context
        WHERE context.starts_at <= v_reference_at
          AND (
              context.ends_at IS NULL
              OR context.ends_at > v_reference_at
          )
        ORDER BY context.starts_at DESC
        LIMIT 1
    ) AS assignment ON true
    WHERE plan.id = p_plan_id
      AND plan.catalog_revision_id = p_catalog_revision_id
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN 'missing_plan_assignment';
    END IF;

    FOR v_quota IN
        SELECT *
        FROM bursar.catalog_plan_quotas
        WHERE catalog_revision_id = p_catalog_revision_id
          AND plan_key = v_plan.plan_key
          AND operation_key = p_operation
        ORDER BY quota_key
    LOOP
        IF NOT COALESCE(p_measures, '{}'::jsonb) ? v_quota.measure_key THEN
            RETURN 'missing_quota_measure';
        END IF;

        v_amount := bursar.jsonb_nonnegative_numeric(
            p_measures->v_quota.measure_key
        );
        IF v_amount IS NULL THEN
            RETURN 'invalid_measure';
        END IF;

        SELECT *
        INTO v_window
        FROM bursar.quota_policy_window(
            v_plan.starts_at,
            v_quota.window_policy
        );

        IF v_window.is_rolling THEN
            SELECT COALESCE(sum(event.amount), 0)
            INTO v_consumed
            FROM bursar.quota_usage_events AS event
            WHERE event.account_id = p_account_id
              AND event.catalog_quota_id = v_quota.id
              AND event.event_at > v_window.window_start
              AND event.event_at <= v_window.window_end;

            SELECT COALESCE(sum(reservation.amount), 0)
            INTO v_reserved
            FROM bursar.credit_lease_quota_reservations AS reservation
            JOIN bursar.credit_leases AS lease
              ON lease.id = reservation.lease_id
            WHERE reservation.catalog_quota_id = v_quota.id
              AND reservation.released_at IS NULL
              AND reservation.created_at > v_window.window_start
              AND lease.account_id = p_account_id
              AND lease.status = 'active'
              AND lease.expires_at > now()
              AND (
                  p_exclude_lease_id IS NULL
                  OR lease.id <> p_exclude_lease_id
              );
        ELSE
            SELECT
                COALESCE(quota_window.consumed, 0),
                greatest(
                    COALESCE(quota_window.reserved, 0)
                    - COALESCE(own_reservation.amount, 0),
                    0
                )
            INTO v_consumed, v_reserved
            FROM (SELECT true) AS singleton
            LEFT JOIN bursar.quota_windows AS quota_window
              ON quota_window.account_id = p_account_id
             AND quota_window.plan_id = p_plan_id
             AND quota_window.catalog_revision_id = p_catalog_revision_id
             AND quota_window.quota_key = v_quota.quota_key
             AND quota_window.window_start = v_window.window_start
             AND quota_window.window_end = v_window.window_end
            LEFT JOIN bursar.credit_lease_quota_reservations
                AS own_reservation
              ON own_reservation.quota_window_id = quota_window.id
             AND own_reservation.lease_id = p_exclude_lease_id
             AND own_reservation.released_at IS NULL;
        END IF;

        IF p_enforce
           AND v_quota.enforcement = 'block'
           AND v_consumed + v_reserved + v_amount > v_quota.quota_limit
        THEN
            INSERT INTO bursar.quota_windows(
                account_id,
                plan_id,
                catalog_revision_id,
                quota_key,
                operation_key,
                measure_key,
                window_start,
                window_end,
                quota_limit,
                reserved,
                consumed,
                enforcement,
                policy_snapshot
            )
            VALUES(
                p_account_id,
                p_plan_id,
                p_catalog_revision_id,
                v_quota.quota_key,
                v_quota.operation_key,
                v_quota.measure_key,
                v_window.window_start,
                v_window.window_end,
                v_quota.quota_limit,
                v_reserved,
                v_consumed,
                v_quota.enforcement,
                v_quota.definition
            )
            ON CONFLICT (
                account_id,
                plan_id,
                catalog_revision_id,
                quota_key,
                window_start,
                window_end
            ) DO UPDATE
            SET reserved = EXCLUDED.reserved,
                consumed = EXCLUDED.consumed
            RETURNING id INTO v_window_id;

            INSERT INTO bursar.quota_events(
                quota_window_id,
                event_type,
                idempotency_key
            )
            VALUES(v_window_id, 'blocked', p_idempotency_key)
            ON CONFLICT DO NOTHING;
            RETURN 'quota_exceeded';
        END IF;
    END LOOP;
    RETURN NULL;
END
$$;

CREATE FUNCTION bursar.reserve_operation_quotas(
    p_lease_id uuid,
    p_measures jsonb
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_lease bursar.credit_leases;
    v_plan_key text;
    v_quota record;
    v_window record;
    v_amount numeric;
    v_consumed numeric;
    v_reserved numeric;
    v_window_id uuid;
BEGIN
    SELECT *
    INTO v_lease
    FROM bursar.credit_leases
    WHERE id = p_lease_id;

    IF NOT FOUND OR v_lease.plan_id IS NULL THEN
        RETURN;
    END IF;

    SELECT plan_key
    INTO v_plan_key
    FROM bursar.catalog_plans
    WHERE id = v_lease.plan_id
      AND catalog_revision_id = v_lease.catalog_revision_id;

    FOR v_quota IN
        SELECT *
        FROM bursar.catalog_plan_quotas
        WHERE catalog_revision_id = v_lease.catalog_revision_id
          AND plan_key = v_plan_key
          AND operation_key = v_lease.operation
        ORDER BY quota_key
    LOOP
        v_amount := bursar.jsonb_nonnegative_numeric(
            p_measures->v_quota.measure_key
        );
        IF v_amount = 0 THEN
            CONTINUE;
        END IF;

        SELECT *
        INTO v_window
        FROM bursar.quota_policy_window(
            v_lease.created_at,
            v_quota.window_policy
        );
        v_window_id := NULL;

        IF NOT v_window.is_rolling THEN
            INSERT INTO bursar.quota_windows(
                account_id,
                plan_id,
                catalog_revision_id,
                quota_key,
                operation_key,
                measure_key,
                window_start,
                window_end,
                quota_limit,
                enforcement,
                policy_snapshot
            )
            VALUES(
                v_lease.account_id,
                v_lease.plan_id,
                v_lease.catalog_revision_id,
                v_quota.quota_key,
                v_quota.operation_key,
                v_quota.measure_key,
                v_window.window_start,
                v_window.window_end,
                v_quota.quota_limit,
                v_quota.enforcement,
                v_quota.definition
            )
            ON CONFLICT (
                account_id,
                plan_id,
                catalog_revision_id,
                quota_key,
                window_start,
                window_end
            ) DO UPDATE SET quota_limit = EXCLUDED.quota_limit
            RETURNING id INTO v_window_id;

            UPDATE bursar.quota_windows
            SET reserved = reserved + v_amount
            WHERE id = v_window_id;
        END IF;

        INSERT INTO bursar.credit_lease_quota_reservations(
            lease_id,
            catalog_quota_id,
            quota_window_id,
            amount,
            window_start,
            window_end
        )
        VALUES(
            v_lease.id,
            v_quota.id,
            v_window_id,
            v_amount,
            v_window.window_start,
            v_window.window_end
        );
    END LOOP;
END
$$;

CREATE FUNCTION bursar.release_lease_quota_reservations(
    p_lease_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_reservation record;
BEGIN
    FOR v_reservation IN
        SELECT *
        FROM bursar.credit_lease_quota_reservations
        WHERE lease_id = p_lease_id
          AND released_at IS NULL
        FOR UPDATE
    LOOP
        IF v_reservation.quota_window_id IS NOT NULL THEN
            UPDATE bursar.quota_windows
            SET reserved = reserved - v_reservation.amount
            WHERE id = v_reservation.quota_window_id
              AND reserved >= v_reservation.amount;

            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'lease quota release is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
        END IF;

        UPDATE bursar.credit_lease_quota_reservations
        SET released_at = now()
        WHERE lease_id = v_reservation.lease_id
          AND catalog_quota_id = v_reservation.catalog_quota_id;
    END LOOP;
END
$$;

CREATE FUNCTION bursar.record_operation_quotas(
    p_account_id uuid,
    p_plan_id uuid,
    p_catalog_revision_id uuid,
    p_operation text,
    p_measures jsonb,
    p_usage_charge_id uuid,
    p_idempotency_key text,
    p_event_at timestamptz,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan record;
    v_quota record;
    v_window record;
    v_amount numeric;
    v_before numeric;
    v_after numeric;
    v_reserved numeric;
    v_window_id uuid;
    v_threshold integer;
    v_digest bytea;
BEGIN
    IF p_plan_id IS NULL THEN
        RETURN;
    END IF;

    SELECT plan.plan_key, assignment.starts_at
    INTO v_plan
    FROM bursar.catalog_plans AS plan
    JOIN LATERAL (
        SELECT context.starts_at
        FROM (
            SELECT current_assignment.starts_at
            FROM bursar.account_plan_assignments
                AS current_assignment
            WHERE current_assignment.account_id = p_account_id
              AND current_assignment.plan_id = p_plan_id
              AND current_assignment.catalog_revision_id =
                  p_catalog_revision_id
            UNION ALL
            SELECT historical_assignment.starts_at
            FROM bursar.account_plan_assignment_history
                AS historical_assignment
            WHERE historical_assignment.account_id = p_account_id
              AND historical_assignment.plan_id = p_plan_id
              AND historical_assignment.catalog_revision_id =
                  p_catalog_revision_id
        ) AS context
        ORDER BY context.starts_at DESC
        LIMIT 1
    ) AS assignment ON true
    WHERE plan.id = p_plan_id
      AND plan.catalog_revision_id = p_catalog_revision_id
    LIMIT 1;

    FOR v_quota IN
        SELECT *
        FROM bursar.catalog_plan_quotas
        WHERE catalog_revision_id = p_catalog_revision_id
          AND plan_key = v_plan.plan_key
          AND operation_key = p_operation
        ORDER BY quota_key
    LOOP
        v_amount := bursar.jsonb_nonnegative_numeric(
            p_measures->v_quota.measure_key
        );
        IF v_amount = 0 THEN
            CONTINUE;
        END IF;

        SELECT *
        INTO v_window
        FROM bursar.quota_policy_window(
            v_plan.starts_at,
            v_quota.window_policy
        );

        IF v_window.is_rolling THEN
            SELECT COALESCE(sum(event.amount), 0)
            INTO v_before
            FROM bursar.quota_usage_events AS event
            WHERE event.account_id = p_account_id
              AND event.catalog_quota_id = v_quota.id
              AND event.event_at > v_window.window_start
              AND event.event_at <= v_window.window_end;

            SELECT COALESCE(sum(reservation.amount), 0)
            INTO v_reserved
            FROM bursar.credit_lease_quota_reservations AS reservation
            JOIN bursar.credit_leases AS lease
              ON lease.id = reservation.lease_id
            WHERE reservation.catalog_quota_id = v_quota.id
              AND reservation.released_at IS NULL
              AND reservation.created_at > v_window.window_start
              AND lease.account_id = p_account_id
              AND lease.status = 'active'
              AND lease.expires_at > now();
            v_window_id := NULL;
        ELSE
            INSERT INTO bursar.quota_windows(
                account_id,
                plan_id,
                catalog_revision_id,
                quota_key,
                operation_key,
                measure_key,
                window_start,
                window_end,
                quota_limit,
                enforcement,
                policy_snapshot
            )
            VALUES(
                p_account_id,
                p_plan_id,
                p_catalog_revision_id,
                v_quota.quota_key,
                v_quota.operation_key,
                v_quota.measure_key,
                v_window.window_start,
                v_window.window_end,
                v_quota.quota_limit,
                v_quota.enforcement,
                v_quota.definition
            )
            ON CONFLICT (
                account_id,
                plan_id,
                catalog_revision_id,
                quota_key,
                window_start,
                window_end
            ) DO UPDATE SET quota_limit = EXCLUDED.quota_limit
            RETURNING id INTO v_window_id;

            SELECT consumed, reserved
            INTO v_before, v_reserved
            FROM bursar.quota_windows
            WHERE id = v_window_id
            FOR UPDATE;
        END IF;

        v_digest := extensions.digest(
            convert_to(
                jsonb_build_object(
                    'catalog_quota_id', v_quota.id,
                    'amount', bursar.digest_numeric_text(v_amount),
                    'usage_charge_id', p_usage_charge_id,
                    'event_at', p_event_at,
                    'metadata', COALESCE(p_metadata, '{}'::jsonb)
                )::text,
                'UTF8'
            ),
            'sha256'
        );

        INSERT INTO bursar.quota_usage_events(
            account_id,
            plan_id,
            catalog_revision_id,
            catalog_quota_id,
            quota_key,
            operation_key,
            measure_key,
            amount,
            event_at,
            usage_charge_id,
            idempotency_key,
            request_digest,
            metadata
        )
        VALUES(
            p_account_id,
            p_plan_id,
            p_catalog_revision_id,
            v_quota.id,
            v_quota.quota_key,
            v_quota.operation_key,
            v_quota.measure_key,
            v_amount,
            p_event_at,
            p_usage_charge_id,
            p_idempotency_key,
            v_digest,
            COALESCE(p_metadata, '{}'::jsonb)
        );

        v_after := v_before + v_amount;
        IF v_window_id IS NOT NULL THEN
            UPDATE bursar.quota_windows
            SET consumed = v_after
            WHERE id = v_window_id;
        END IF;

        FOREACH v_threshold IN ARRAY v_quota.emit_at_percent LOOP
            IF v_before * 100 < v_quota.quota_limit * v_threshold
               AND v_after * 100 >= v_quota.quota_limit * v_threshold
            THEN
                IF v_window_id IS NULL THEN
                    INSERT INTO bursar.quota_windows(
                        account_id,
                        plan_id,
                        catalog_revision_id,
                        quota_key,
                        operation_key,
                        measure_key,
                        window_start,
                        window_end,
                        quota_limit,
                        reserved,
                        consumed,
                        enforcement,
                        policy_snapshot
                    )
                    VALUES(
                        p_account_id,
                        p_plan_id,
                        p_catalog_revision_id,
                        v_quota.quota_key,
                        v_quota.operation_key,
                        v_quota.measure_key,
                        v_window.window_start,
                        v_window.window_end,
                        v_quota.quota_limit,
                        v_reserved,
                        v_after,
                        v_quota.enforcement,
                        v_quota.definition
                    )
                    ON CONFLICT (
                        account_id,
                        plan_id,
                        catalog_revision_id,
                        quota_key,
                        window_start,
                        window_end
                    ) DO UPDATE
                    SET reserved = EXCLUDED.reserved,
                        consumed = EXCLUDED.consumed
                    RETURNING id INTO v_window_id;
                END IF;

                INSERT INTO bursar.quota_events(
                    quota_window_id,
                    usage_charge_id,
                    event_type,
                    threshold_percent,
                    idempotency_key
                )
                VALUES(
                    v_window_id,
                    p_usage_charge_id,
                    'threshold',
                    v_threshold,
                    p_idempotency_key
                )
                ON CONFLICT DO NOTHING;
            END IF;
        END LOOP;
    END LOOP;
END
$$;

CREATE FUNCTION bursar.create_lease(
    p_subject_id uuid,
    p_operation text,
    p_estimate numeric,
    p_idempotency_key text,
    p_minimum_balance numeric DEFAULT NULL,
    p_ttl interval DEFAULT interval '10 minutes',
    p_policy_snapshot jsonb DEFAULT '{}'::jsonb,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_feature text DEFAULT NULL,
    p_measures jsonb DEFAULT '{}'::jsonb,
    p_dimensions jsonb DEFAULT '{}'::jsonb,
    p_max_concurrent integer DEFAULT NULL
)
RETURNS TABLE(
    lease_id uuid,
    status bursar.lease_status,
    reserved_amount numeric,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
    v_existing bursar.credit_leases;
    v_policy record;
    v_plan record;
    v_digest bytea;
    v_balance numeric;
    v_expired numeric;
    v_reserved numeric;
    v_credit_estimate numeric;
    v_allowance_reserved numeric := 0;
    v_allowance_used numeric := 0;
    v_allowance_holds numeric := 0;
    v_allowance_start timestamptz;
    v_allowance_end timestamptz;
    v_allowance_window_id uuid;
    v_revision uuid;
    v_plan_id uuid;
    v_lease_id uuid;
    v_max_concurrent integer;
    v_requested_minimum numeric := p_minimum_balance;
    v_requested_max_concurrent integer := p_max_concurrent;
    v_quota_error text;
BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_nonempty_text(p_operation)
       OR NOT bursar.is_finite_numeric(p_estimate)
       OR p_estimate < 0
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR p_ttl <= interval '0'
       OR (p_max_concurrent IS NOT NULL AND p_max_concurrent < 1)
       OR jsonb_typeof(COALESCE(p_policy_snapshot, '{}'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_measures, '{}'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_dimensions, '{}'::jsonb)) <> 'object'
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            'active'::bursar.lease_status,
            0::numeric,
            'invalid_request';
        RETURN;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    SELECT balance
    INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    -- Idempotency describes only caller input. Derived plan policy is persisted
    -- on the lease, but must not turn a retry into a conflict after activation
    -- or assignment changes.
    v_digest := extensions.digest(
        convert_to(
            jsonb_build_object(
                'operation', p_operation,
                'estimate', bursar.digest_numeric_text(p_estimate),
                'minimum_balance',
                    bursar.digest_numeric_text(v_requested_minimum),
                'max_concurrent', v_requested_max_concurrent,
                'ttl', p_ttl::text,
                'policy', COALESCE(p_policy_snapshot, '{}'::jsonb),
                'feature', p_feature,
                'measures', COALESCE(p_measures, '{}'::jsonb),
                'dimensions', COALESCE(p_dimensions, '{}'::jsonb),
                'metadata', COALESCE(p_metadata, '{}'::jsonb)
            )::text,
            'UTF8'
        ),
        'sha256'
    );

    SELECT *
    INTO v_existing
    FROM bursar.credit_leases
    WHERE account_id = v_account
      AND idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_existing.request_digest <> v_digest THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                'active'::bursar.lease_status,
                0::numeric,
                'idempotency_conflict';
        ELSE
            IF v_existing.status = 'active'
               AND v_existing.expires_at <= now()
            THEN
                IF v_existing.allowance_window_id IS NOT NULL THEN
                    UPDATE bursar.allowance_windows
                    SET reserved =
                        reserved - v_existing.reserved_allowance
                    WHERE id = v_existing.allowance_window_id
                      AND reserved >= v_existing.reserved_allowance;

                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'lease allowance expiry is inconsistent'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;

                PERFORM bursar.release_lease_quota_reservations(
                    v_existing.id
                );
                UPDATE bursar.credit_leases
                SET status = 'expired'
                WHERE id = v_existing.id
                RETURNING * INTO v_existing;
            END IF;

            RETURN QUERY
            SELECT
                v_existing.id,
                v_existing.status,
                v_existing.reserved_amount,
                NULL::text;
        END IF;
        RETURN;
    END IF;

    SELECT *
    INTO v_policy
    FROM bursar.effective_subject_policy(
        p_subject_id,
        p_operation
    );

    IF FOUND THEN
        IF p_minimum_balance IS NOT NULL
           AND p_minimum_balance <> v_policy.minimum_balance
        THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                'active'::bursar.lease_status,
                0::numeric,
                'policy_mismatch';
            RETURN;
        END IF;

        p_minimum_balance := v_policy.minimum_balance;
        IF v_requested_max_concurrent IS NOT NULL
           AND v_requested_max_concurrent
               IS DISTINCT FROM v_policy.max_concurrent
        THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                'active'::bursar.lease_status,
                0::numeric,
                'policy_mismatch';
            RETURN;
        END IF;
        v_max_concurrent := v_policy.max_concurrent;
        v_revision := v_policy.catalog_revision_id;
        v_plan_id := v_policy.plan_id;
    ELSE
        p_minimum_balance := COALESCE(p_minimum_balance, 0);
        v_max_concurrent := v_requested_max_concurrent;
    END IF;

    IF NOT bursar.is_finite_numeric(p_minimum_balance) THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            'active'::bursar.lease_status,
            0::numeric,
            'invalid_request';
        RETURN;
    END IF;

    IF v_revision IS NULL THEN
        SELECT id
        INTO v_revision
        FROM bursar.catalog_revisions AS revision
        WHERE revision.status = 'active';
    END IF;

    IF v_revision IS NULL THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            'active'::bursar.lease_status,
            0::numeric,
            'missing_active_catalog';
        RETURN;
    END IF;

    IF v_plan_id IS NOT NULL THEN
        SELECT
            plan.allowed_operations,
            plan.credit_allowance_amount,
            plan.credit_allowance_reset_unit,
            plan.credit_allowance_reset_count,
            plan.credit_allowance_reset_anchor,
            plan.credit_allowance_reset_timezone,
            assignment.starts_at
        INTO v_plan
        FROM bursar.catalog_plans AS plan
        JOIN bursar.account_plan_assignments AS assignment
          ON assignment.plan_id = plan.id
         AND assignment.catalog_revision_id = plan.catalog_revision_id
        WHERE plan.id = v_plan_id
          AND plan.catalog_revision_id = v_revision
          AND assignment.account_id = v_account
          AND assignment.starts_at <= now()
          AND (
              assignment.ends_at IS NULL
              OR assignment.ends_at > now()
          );

        IF NOT FOUND THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                'active'::bursar.lease_status,
                0::numeric,
                'missing_plan_assignment';
            RETURN;
        END IF;

        IF cardinality(v_plan.allowed_operations) > 0
           AND NOT p_operation = ANY(v_plan.allowed_operations)
        THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                'active'::bursar.lease_status,
                0::numeric,
                'operation_not_allowed';
            RETURN;
        END IF;

        IF p_feature IS NOT NULL
           AND NOT bursar.subject_has_entitlement(
               p_subject_id,
               p_feature,
               now()
           )
        THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                'active'::bursar.lease_status,
                0::numeric,
                'feature_not_entitled';
            RETURN;
        END IF;

        v_quota_error := bursar.check_operation_quotas(
            v_account,
            v_plan_id,
            v_revision,
            p_operation,
            COALESCE(p_measures, '{}'::jsonb),
            p_idempotency_key
        );
        IF v_quota_error IS NOT NULL THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                'active'::bursar.lease_status,
                0::numeric,
                v_quota_error;
            RETURN;
        END IF;

        IF v_plan.credit_allowance_amount IS NOT NULL
           AND p_estimate > 0
        THEN
            SELECT window_start, window_end
            INTO v_allowance_start, v_allowance_end
            FROM bursar.policy_period_window(
                v_plan.starts_at,
                v_plan.credit_allowance_reset_unit,
                v_plan.credit_allowance_reset_count,
                v_plan.credit_allowance_reset_anchor,
                v_plan.credit_allowance_reset_timezone
            );

            IF v_plan.credit_allowance_reset_anchor = 'rolling' THEN
                SELECT COALESCE(sum(charge.allowance_covered), 0)
                INTO v_allowance_used
                FROM bursar.credit_usage_charges AS charge
                WHERE charge.account_id = v_account
                  AND charge.plan_id = v_plan_id
                  AND charge.catalog_revision_id = v_revision
                  AND charge.event_at > v_allowance_start
                  AND charge.event_at <= v_allowance_end;

                SELECT COALESCE(sum(lease.reserved_allowance), 0)
                INTO v_allowance_holds
                FROM bursar.credit_leases AS lease
                WHERE lease.account_id = v_account
                  AND lease.plan_id = v_plan_id
                  AND lease.catalog_revision_id = v_revision
                  AND lease.status = 'active'
                  AND lease.expires_at > now();

                v_allowance_reserved := least(
                    p_estimate,
                    greatest(
                        v_plan.credit_allowance_amount
                            - v_allowance_used
                            - v_allowance_holds,
                        0
                    )
                );
            ELSE
                INSERT INTO bursar.allowance_windows(
                    account_id,
                    plan_id,
                    catalog_revision_id,
                    allowance_key,
                    window_start,
                    window_end,
                    period_unit,
                    period_count,
                    period_anchor,
                    period_timezone,
                    allowance
                )
                VALUES(
                    v_account,
                    v_plan_id,
                    v_revision,
                    '__included_credits__',
                    v_allowance_start,
                    v_allowance_end,
                    v_plan.credit_allowance_reset_unit,
                    v_plan.credit_allowance_reset_count,
                    v_plan.credit_allowance_reset_anchor,
                    v_plan.credit_allowance_reset_timezone,
                    v_plan.credit_allowance_amount
                )
                ON CONFLICT (
                    account_id,
                    plan_id,
                    catalog_revision_id,
                    allowance_key,
                    window_start,
                    window_end
                ) DO NOTHING;

                SELECT
                    id,
                    least(
                        p_estimate,
                        greatest(allowance - reserved - consumed, 0)
                    )
                INTO v_allowance_window_id, v_allowance_reserved
                FROM bursar.allowance_windows
                WHERE account_id = v_account
                  AND plan_id = v_plan_id
                  AND catalog_revision_id = v_revision
                  AND allowance_key = '__included_credits__'
                  AND window_start = v_allowance_start
                  AND window_end = v_allowance_end
                FOR UPDATE;
            END IF;
        END IF;
    END IF;

    IF v_plan_id IS NULL
       AND p_feature IS NOT NULL
       AND NOT bursar.subject_has_entitlement(
           p_subject_id,
           p_feature,
           now()
       )
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            'active'::bursar.lease_status,
            0::numeric,
            'feature_not_entitled';
        RETURN;
    END IF;

    SELECT COALESCE(sum(granted - consumed), 0)
    INTO v_expired
    FROM bursar.credit_lots
    WHERE account_id = v_account
      AND consumed < granted
      AND expires_at <= now();

    SELECT COALESCE(
        sum(lease.reserved_amount - lease.reserved_allowance),
        0
    )
    INTO v_reserved
    FROM bursar.credit_leases AS lease
    WHERE lease.account_id = v_account
      AND lease.status = 'active'
      AND lease.expires_at > now();

    v_credit_estimate := p_estimate - v_allowance_reserved;

    IF v_balance - v_expired - v_reserved - v_credit_estimate
       < p_minimum_balance
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            'active'::bursar.lease_status,
            0::numeric,
            'insufficient_headroom';
        RETURN;
    END IF;

    IF v_max_concurrent IS NOT NULL
       AND (
           SELECT count(*)
           FROM bursar.credit_leases AS lease
           WHERE lease.account_id = v_account
             AND lease.operation = p_operation
             AND lease.status = 'active'
             AND lease.expires_at > now()
       ) >= v_max_concurrent
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            'active'::bursar.lease_status,
            0::numeric,
            'max_concurrent_reached';
        RETURN;
    END IF;

    IF v_allowance_window_id IS NOT NULL
       AND v_allowance_reserved > 0
    THEN
        UPDATE bursar.allowance_windows
        SET reserved = reserved + v_allowance_reserved
        WHERE id = v_allowance_window_id
          AND reserved + consumed + v_allowance_reserved <= allowance;

        IF NOT FOUND THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                'active'::bursar.lease_status,
                0::numeric,
                'allowance_race';
            RETURN;
        END IF;
    END IF;

    INSERT INTO bursar.credit_leases(
        account_id,
        operation,
        feature,
        measures,
        dimensions,
        policy_snapshot,
        metadata,
        catalog_revision_id,
        plan_id,
        reserved_amount,
        reserved_allowance,
        allowance_window_id,
        minimum_balance,
        max_concurrent,
        expires_at,
        idempotency_key,
        request_digest
    )
    VALUES (
        v_account,
        p_operation,
        p_feature,
        COALESCE(p_measures, '{}'::jsonb),
        COALESCE(p_dimensions, '{}'::jsonb),
        COALESCE(p_policy_snapshot, '{}'::jsonb),
        COALESCE(p_metadata, '{}'::jsonb),
        v_revision,
        v_plan_id,
        p_estimate,
        v_allowance_reserved,
        v_allowance_window_id,
        p_minimum_balance,
        v_max_concurrent,
        now() + p_ttl,
        p_idempotency_key,
        v_digest
    )
    RETURNING id INTO v_lease_id;

    PERFORM bursar.reserve_operation_quotas(
        v_lease_id,
        COALESCE(p_measures, '{}'::jsonb)
    );

    RETURN QUERY
    SELECT
        v_lease_id,
        'active'::bursar.lease_status,
        p_estimate,
        NULL::text;
END
$$;

CREATE FUNCTION bursar.settle_lease(
    p_subject_id uuid,
    p_lease_id uuid,
    p_actual numeric,
    p_idempotency_key text,
    p_feature text DEFAULT NULL,
    p_model text DEFAULT NULL,
    p_region text DEFAULT NULL,
    p_measures jsonb DEFAULT '{}'::jsonb,
    p_dimensions jsonb DEFAULT '{}'::jsonb,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
    ledger_entry_id uuid,
    settled_amount numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
    v_lease bursar.credit_leases;
    v_plan bursar.catalog_plans;
    v_allowance_window bursar.allowance_windows;
    v_post record;
    v_ledger_entry uuid;
    v_allowance numeric := 0;
    v_allowance_used numeric := 0;
    v_allowance_holds numeric := 0;
    v_allowance_start timestamptz;
    v_allowance_end timestamptz;
    v_feature text;
    v_metadata jsonb;
    v_settlement_digest bytea;
    v_quota_error text;
    v_event_at timestamptz;
BEGIN
    IF NOT bursar.is_finite_numeric(p_actual)
       OR p_actual < 0
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR jsonb_typeof(COALESCE(p_measures, '{}'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_dimensions, '{}'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
    THEN
        RETURN QUERY
        SELECT NULL::uuid, 0::numeric, false, 'invalid_request';
        RETURN;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    PERFORM 1
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    SELECT *
    INTO v_lease
    FROM bursar.credit_leases
    WHERE id = p_lease_id
      AND account_id = v_account
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT NULL::uuid, 0::numeric, false, 'missing_lease';
        RETURN;
    END IF;

    v_feature := COALESCE(p_feature, v_lease.feature);
    v_metadata := COALESCE(v_lease.metadata, '{}'::jsonb)
        || COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_build_object('lease_id', p_lease_id);
    v_settlement_digest := extensions.digest(
        convert_to(
            jsonb_build_object(
                'lease_id', p_lease_id,
                'actual', bursar.digest_numeric_text(p_actual),
                'idempotency_key', p_idempotency_key,
                'feature', v_feature,
                'model', p_model,
                'region', p_region,
                'measures', COALESCE(p_measures, '{}'::jsonb),
                'dimensions', COALESCE(p_dimensions, '{}'::jsonb),
                'metadata', v_metadata
            )::text,
            'UTF8'
        ),
        'sha256'
    );

    IF v_lease.status = 'settled' THEN
        IF v_lease.settlement_request_digest
           IS DISTINCT FROM v_settlement_digest
        THEN
            RETURN QUERY
            SELECT NULL::uuid, 0::numeric, false, 'settlement_conflict';
            RETURN;
        END IF;

        RETURN QUERY
        SELECT
            v_lease.ledger_entry_id,
            COALESCE(v_lease.settled_amount, 0),
            true,
            NULL::text;
        RETURN;
    END IF;

    IF v_lease.status = 'released' THEN
        RETURN QUERY
        SELECT NULL::uuid, 0::numeric, false, 'released_lease';
        RETURN;
    END IF;

    IF v_lease.status = 'settling' THEN
        RETURN QUERY
        SELECT NULL::uuid, 0::numeric, false, 'lease_in_progress';
        RETURN;
    END IF;

    IF v_lease.status = 'expired' OR v_lease.expires_at <= now() THEN
        IF v_lease.status = 'active'
           AND v_lease.allowance_window_id IS NOT NULL
        THEN
            UPDATE bursar.allowance_windows
            SET reserved = reserved - v_lease.reserved_allowance
            WHERE id = v_lease.allowance_window_id
              AND reserved >= v_lease.reserved_allowance;

            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'lease allowance expiry is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
        END IF;

        IF v_lease.status = 'active' THEN
            PERFORM bursar.release_lease_quota_reservations(v_lease.id);
        END IF;
        UPDATE bursar.credit_leases
        SET status = 'expired'
        WHERE id = v_lease.id
          AND status = 'active';

        RETURN QUERY
        SELECT NULL::uuid, 0::numeric, false, 'expired_lease';
        RETURN;
    END IF;

    IF v_lease.plan_id IS NOT NULL THEN
        SELECT *
        INTO v_plan
        FROM bursar.catalog_plans
        WHERE id = v_lease.plan_id
          AND catalog_revision_id = v_lease.catalog_revision_id;

        IF v_plan.credit_allowance_amount IS NOT NULL THEN
            IF v_plan.credit_allowance_reset_anchor = 'rolling' THEN
                SELECT window_start, window_end
                INTO v_allowance_start, v_allowance_end
                FROM bursar.policy_period_window(
                    v_lease.created_at,
                    v_plan.credit_allowance_reset_unit,
                    v_plan.credit_allowance_reset_count,
                    v_plan.credit_allowance_reset_anchor,
                    v_plan.credit_allowance_reset_timezone
                );

                SELECT COALESCE(sum(charge.allowance_covered), 0)
                INTO v_allowance_used
                FROM bursar.credit_usage_charges AS charge
                WHERE charge.account_id = v_account
                  AND charge.plan_id = v_lease.plan_id
                  AND charge.catalog_revision_id =
                      v_lease.catalog_revision_id
                  AND charge.event_at > v_allowance_start
                  AND charge.event_at <= v_allowance_end;

                SELECT COALESCE(sum(lease.reserved_allowance), 0)
                INTO v_allowance_holds
                FROM bursar.credit_leases AS lease
                WHERE lease.account_id = v_account
                  AND lease.plan_id = v_lease.plan_id
                  AND lease.catalog_revision_id =
                      v_lease.catalog_revision_id
                  AND lease.id <> v_lease.id
                  AND lease.status = 'active'
                  AND lease.expires_at > now();

                v_allowance := least(
                    p_actual,
                    greatest(
                        v_plan.credit_allowance_amount
                            - v_allowance_used
                            - v_allowance_holds,
                        0
                    )
                );
            ELSIF v_lease.allowance_window_id IS NOT NULL THEN
                SELECT *
                INTO v_allowance_window
                FROM bursar.allowance_windows
                WHERE id = v_lease.allowance_window_id
                  AND account_id = v_account
                FOR UPDATE;

                IF NOT FOUND
                   OR v_allowance_window.reserved
                        < v_lease.reserved_allowance
                THEN
                    RAISE EXCEPTION
                        'lease allowance reservation is inconsistent'
                        USING ERRCODE = '23514';
                END IF;

                v_allowance := least(
                    p_actual,
                    greatest(
                        v_allowance_window.allowance
                            - v_allowance_window.consumed
                            - (
                                v_allowance_window.reserved
                                - v_lease.reserved_allowance
                            ),
                        0
                    )
                );
            END IF;
        END IF;
    END IF;

    v_quota_error := bursar.check_operation_quotas(
        v_account,
        v_lease.plan_id,
        v_lease.catalog_revision_id,
        v_lease.operation,
        COALESCE(p_measures, '{}'::jsonb),
        p_idempotency_key,
        false,
        v_lease.id
    );
    IF v_quota_error IS NOT NULL THEN
        RETURN QUERY
        SELECT NULL::uuid, 0::numeric, false, v_quota_error;
        RETURN;
    END IF;

    -- Temporarily release this lease's credit hold. post_credit still accounts
    -- for every other active lease while the account row remains locked.
    UPDATE bursar.credit_leases
    SET status = 'settling'
    WHERE id = v_lease.id;

    v_event_at := now();
    SELECT *
    INTO v_post
    FROM bursar.charge_usage(
        p_subject_id,
        v_lease.operation,
        p_actual,
        p_idempotency_key,
        v_feature,
        p_model,
        p_region,
        v_allowance,
        v_metadata,
        v_allowance,
        v_lease.catalog_revision_id,
        v_lease.plan_id,
        NULL,
        v_lease.minimum_balance,
        v_event_at,
        COALESCE(p_measures, '{}'::jsonb),
        COALESCE(p_dimensions, '{}'::jsonb)
    );

    IF v_post.error_code IS NOT NULL THEN
        UPDATE bursar.credit_leases
        SET status = 'active'
        WHERE id = v_lease.id;

        RETURN QUERY
        SELECT NULL::uuid, 0::numeric, false, v_post.error_code;
        RETURN;
    END IF;

    v_ledger_entry := v_post.ledger_entry_id;

    PERFORM bursar.release_lease_quota_reservations(v_lease.id);
    PERFORM bursar.record_operation_quotas(
        v_account,
        v_lease.plan_id,
        v_lease.catalog_revision_id,
        v_lease.operation,
        COALESCE(p_measures, '{}'::jsonb),
        v_post.charge_id,
        p_idempotency_key,
        v_event_at,
        v_metadata
    );

    IF v_lease.allowance_window_id IS NOT NULL THEN
        UPDATE bursar.allowance_windows
        SET reserved = reserved - v_lease.reserved_allowance,
            consumed = consumed + v_allowance
        WHERE id = v_lease.allowance_window_id
          AND reserved >= v_lease.reserved_allowance
          AND consumed + v_allowance
                <= allowance - (
                    reserved - v_lease.reserved_allowance
                );

        IF NOT FOUND THEN
            RAISE EXCEPTION
                'lease allowance settlement is inconsistent'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    UPDATE bursar.credit_leases
    SET status = 'settled',
        settled_amount = p_actual,
        settlement_idempotency_key = p_idempotency_key,
        settlement_request_digest = v_settlement_digest,
        ledger_entry_id = v_ledger_entry
    WHERE id = v_lease.id;

    RETURN QUERY
    SELECT v_ledger_entry, p_actual, false, NULL::text;
END
$$;

CREATE FUNCTION bursar.renew_lease(
    p_subject_id uuid,
    p_lease_id uuid,
    p_ttl interval
)
RETURNS TABLE(
    lease_id uuid,
    status bursar.lease_status,
    reserved_amount numeric,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
    v_lease bursar.credit_leases;
BEGIN
    IF p_subject_id IS NULL
       OR p_lease_id IS NULL
       OR p_ttl IS NULL
       OR p_ttl <= interval '0'
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            'active'::bursar.lease_status,
            0::numeric,
            'invalid_request';
        RETURN;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    PERFORM 1
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    SELECT *
    INTO v_lease
    FROM bursar.credit_leases
    WHERE id = p_lease_id
      AND account_id = v_account
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            'active'::bursar.lease_status,
            0::numeric,
            'missing_lease';
        RETURN;
    END IF;

    IF v_lease.status = 'active' AND v_lease.expires_at <= now() THEN
        IF v_lease.allowance_window_id IS NOT NULL THEN
            UPDATE bursar.allowance_windows
            SET reserved = reserved - v_lease.reserved_allowance
            WHERE id = v_lease.allowance_window_id
              AND reserved >= v_lease.reserved_allowance;

            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'lease allowance expiry is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
        END IF;

        PERFORM bursar.release_lease_quota_reservations(v_lease.id);

        UPDATE bursar.credit_leases
        SET status = 'expired'
        WHERE id = v_lease.id
        RETURNING * INTO v_lease;
    END IF;

    IF v_lease.status <> 'active' THEN
        RETURN QUERY
        SELECT
            v_lease.id,
            v_lease.status,
            v_lease.reserved_amount,
            CASE v_lease.status
                WHEN 'expired' THEN 'expired_lease'
                WHEN 'released' THEN 'released_lease'
                WHEN 'settled' THEN 'settled_lease'
                ELSE 'lease_in_progress'
            END;
        RETURN;
    END IF;

    UPDATE bursar.credit_leases
    SET expires_at = greatest(expires_at, now() + p_ttl)
    WHERE id = v_lease.id
    RETURNING * INTO v_lease;

    RETURN QUERY
    SELECT
        v_lease.id,
        v_lease.status,
        v_lease.reserved_amount,
        NULL::text;
END
$$;

CREATE FUNCTION bursar.expire_leases(
    p_limit integer DEFAULT 100
)
RETURNS TABLE(expired integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_count integer;
    v_expected_windows integer;
    v_released_windows integer;
    v_expired_ids uuid[];
    v_expired_id uuid;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RETURN QUERY SELECT 0;
        RETURN;
    END IF;

    WITH claimed AS (
        SELECT id
        FROM bursar.credit_leases
        WHERE status = 'active'
          AND expires_at <= now()
        ORDER BY expires_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    ),
    changed AS (
        UPDATE bursar.credit_leases AS lease
        SET status = 'expired'
        FROM claimed
        WHERE lease.id = claimed.id
          AND lease.status = 'active'
        RETURNING
            lease.id,
            lease.allowance_window_id,
            lease.reserved_allowance
    ),
    released_by_window AS (
        SELECT
            allowance_window_id,
            sum(reserved_allowance) AS amount
        FROM changed
        WHERE allowance_window_id IS NOT NULL
        GROUP BY allowance_window_id
    ),
    released AS (
        UPDATE bursar.allowance_windows AS allowance
        SET reserved = allowance.reserved - release.amount
        FROM released_by_window AS release
        WHERE allowance.id = release.allowance_window_id
          AND allowance.reserved >= release.amount
        RETURNING allowance.id
    )
    SELECT
        (SELECT count(*)::integer FROM changed),
        (SELECT count(*)::integer FROM released_by_window),
        (SELECT count(*)::integer FROM released),
        (SELECT array_agg(id) FROM changed)
    INTO
        v_count,
        v_expected_windows,
        v_released_windows,
        v_expired_ids;

    IF v_expected_windows <> v_released_windows THEN
        RAISE EXCEPTION
            'lease allowance expiry is inconsistent'
            USING ERRCODE = '23514';
    END IF;

    FOREACH v_expired_id IN ARRAY COALESCE(v_expired_ids, ARRAY[]::uuid[])
    LOOP
        PERFORM bursar.release_lease_quota_reservations(v_expired_id);
    END LOOP;

    RETURN QUERY SELECT v_count;
END
$$;

CREATE FUNCTION bursar.release_lease(
    p_subject_id uuid,
    p_lease_id uuid
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
    v_lease bursar.credit_leases;
BEGIN
    v_account := bursar.account_for_subject(p_subject_id);

    PERFORM 1
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    SELECT *
    INTO v_lease
    FROM bursar.credit_leases
    WHERE id = p_lease_id
      AND account_id = v_account
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN 'missing_lease';
    END IF;

    IF v_lease.status = 'active' THEN
        IF v_lease.allowance_window_id IS NOT NULL THEN
            UPDATE bursar.allowance_windows
            SET reserved = reserved - v_lease.reserved_allowance
            WHERE id = v_lease.allowance_window_id
              AND reserved >= v_lease.reserved_allowance;

            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'lease allowance release is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
        END IF;

        PERFORM bursar.release_lease_quota_reservations(v_lease.id);
        UPDATE bursar.credit_leases
        SET status = 'released'
        WHERE id = p_lease_id;
        RETURN 'released';
    END IF;

    RETURN v_lease.status::text;
END
$$;
