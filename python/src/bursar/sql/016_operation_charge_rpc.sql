-- Operation charge orchestration RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.charge_usage_for_operation(
    p_subject_id uuid,
    p_operation text,
    p_requested numeric,
    p_idempotency_key text,
    p_feature text DEFAULT NULL,
    p_model text DEFAULT NULL,
    p_region text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_measures jsonb DEFAULT '{}'::jsonb,
    p_dimensions jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    charge_id uuid,
    ledger_entry_id uuid,
    charged numeric,
    allowance_covered numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_account uuid;
    v_assignment record;
    v_allowance_start timestamptz;
    v_allowance_end timestamptz;
    v_free numeric := 0;
    v_allowance_used numeric := 0;
    v_allowance_reserved numeric := 0;
    v_result record;
    v_existing record;
    v_has_assignment boolean := false;
    v_quota_error text;
    v_event_at timestamptz;
BEGIN
    IF NOT bursar.is_finite_numeric(p_requested)
       OR p_requested < 0
       OR NOT bursar.is_nonempty_text(p_operation)
       OR NOT bursar.is_bounded_text(p_operation, 255)
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR NOT bursar.is_bounded_text(p_idempotency_key, 255)
       OR (
           p_feature IS NOT NULL
           AND NOT bursar.is_bounded_text(p_feature, 255)
       )
       OR (
           p_model IS NOT NULL
           AND NOT bursar.is_bounded_text(p_model, 255)
       )
       OR (
           p_region IS NOT NULL
           AND NOT bursar.is_bounded_text(p_region, 255)
       )
       OR NOT bursar.is_bounded_json_object(
           COALESCE(p_metadata, '{}'::jsonb),
           1048576
       )
       OR NOT bursar.valid_measure_object(
           COALESCE(p_measures, '{}'::jsonb),
           16384
       )
       OR NOT bursar.valid_dimension_object(
           COALESCE(p_dimensions, '{}'::jsonb),
           65536
       )
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            0::numeric,
            0::numeric,
            false,
            'invalid_request';
        RETURN;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    -- One account lock serializes allowance consumption, rolling-window
    -- calculations, lease holds, and the resulting credit debit.
    PERFORM 1
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    SELECT c.allowance_requested, c.allowance_covered
    INTO v_existing
    FROM bursar.credit_usage_charges AS c
    WHERE c.account_id = v_account
      AND c.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        SELECT *
        INTO v_result
        FROM bursar.charge_usage(
            p_subject_id,
            p_operation,
            p_requested,
            p_idempotency_key,
            p_feature,
            p_model,
            p_region,
            v_existing.allowance_covered,
            p_metadata,
            v_existing.allowance_requested,
            p_measures => COALESCE(p_measures, '{}'::jsonb),
            p_dimensions => COALESCE(p_dimensions, '{}'::jsonb)
        );

        RETURN QUERY
        SELECT
            v_result.charge_id,
            v_result.ledger_entry_id,
            v_result.charged,
            v_result.allowance_covered,
            v_result.replayed,
            v_result.error_code;
        RETURN;
    END IF;

    SELECT
        a.plan_id,
        a.catalog_revision_id,
        a.starts_at,
        p.credit_allowance_amount,
        p.credit_allowance_priority,
        p.credit_allowance_reset_unit,
        p.credit_allowance_reset_count,
        p.credit_allowance_reset_anchor,
        p.credit_allowance_reset_timezone,
        p.allowed_operations,
        p.definition->'credit_allowance' AS credit_allowance_policy
    INTO v_assignment
    FROM bursar.account_plan_assignments AS a
    JOIN bursar.catalog_plans AS p
      ON p.id = a.plan_id
     AND p.catalog_revision_id = a.catalog_revision_id
    WHERE a.account_id = v_account
      AND a.starts_at <= now()
      AND (a.ends_at IS NULL OR a.ends_at > now());

    v_has_assignment := FOUND;

    IF v_has_assignment
       AND cardinality(v_assignment.allowed_operations) > 0
       AND NOT p_operation = ANY(v_assignment.allowed_operations)
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            0::numeric,
            0::numeric,
            false,
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
            NULL::uuid,
            0::numeric,
            0::numeric,
            false,
            'feature_not_entitled';
        RETURN;
    END IF;

    IF v_has_assignment THEN
        v_quota_error := bursar.check_operation_quotas(
            v_account,
            v_assignment.plan_id,
            v_assignment.catalog_revision_id,
            p_operation,
            COALESCE(p_measures, '{}'::jsonb),
            p_idempotency_key
        );
        IF v_quota_error IS NOT NULL THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                NULL::uuid,
                0::numeric,
                0::numeric,
                false,
                v_quota_error;
            RETURN;
        END IF;
    END IF;

    IF v_has_assignment
       AND v_assignment.credit_allowance_amount IS NOT NULL
    THEN
        SELECT window_start, window_end
        INTO v_allowance_start, v_allowance_end
        FROM bursar.policy_period_window(
            v_assignment.starts_at,
            v_assignment.credit_allowance_reset_unit,
            v_assignment.credit_allowance_reset_count,
            v_assignment.credit_allowance_reset_anchor,
            v_assignment.credit_allowance_reset_timezone
        );

        IF v_assignment.credit_allowance_reset_anchor = 'rolling' THEN
            SELECT COALESCE(sum(charge.allowance_covered), 0)
            INTO v_allowance_used
            FROM bursar.credit_usage_charges AS charge
            WHERE charge.account_id = v_account
              AND charge.plan_id = v_assignment.plan_id
              AND charge.catalog_revision_id =
                  v_assignment.catalog_revision_id
              AND charge.event_at > v_allowance_start
              AND charge.event_at <= v_allowance_end;

            SELECT COALESCE(sum(lease.reserved_allowance), 0)
            INTO v_allowance_reserved
            FROM bursar.credit_leases AS lease
            WHERE lease.account_id = v_account
              AND lease.plan_id = v_assignment.plan_id
              AND lease.catalog_revision_id =
                  v_assignment.catalog_revision_id
              AND lease.status = 'active'
              AND lease.expires_at > now();

            v_free := least(
                p_requested,
                greatest(
                    v_assignment.credit_allowance_amount
                        - v_allowance_used
                        - v_allowance_reserved,
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
                allowance,
                policy_snapshot
            )
            VALUES(
                v_account,
                v_assignment.plan_id,
                v_assignment.catalog_revision_id,
                '__included_credits__',
                v_allowance_start,
                v_allowance_end,
                v_assignment.credit_allowance_reset_unit,
                v_assignment.credit_allowance_reset_count,
                v_assignment.credit_allowance_reset_anchor,
                v_assignment.credit_allowance_reset_timezone,
                v_assignment.credit_allowance_amount,
                v_assignment.credit_allowance_policy
            )
            ON CONFLICT (
                account_id,
                plan_id,
                catalog_revision_id,
                allowance_key,
                window_start,
                window_end
            ) DO NOTHING;

            SELECT least(
                p_requested,
                greatest(allowance - reserved - consumed, 0)
            )
            INTO v_free
            FROM bursar.allowance_windows
            WHERE account_id = v_account
              AND plan_id = v_assignment.plan_id
              AND catalog_revision_id =
                  v_assignment.catalog_revision_id
              AND allowance_key = '__included_credits__'
              AND window_start = v_allowance_start
              AND window_end = v_allowance_end
            FOR UPDATE;
        END IF;
    END IF;

    IF v_has_assignment AND v_free > 0 THEN
        IF v_assignment.credit_allowance_priority IS NOT NULL THEN
            v_free := least(
                v_free,
                greatest(
                    p_requested - bursar.available_credit_before_priority(
                        v_account,
                        v_assignment.credit_allowance_priority
                    ),
                    0
                )
            );
        END IF;
    END IF;

    IF v_free > 0 THEN
        IF v_assignment.credit_allowance_reset_anchor = 'rolling' THEN
            SELECT *
            INTO v_result
            FROM bursar.charge_usage(
                p_subject_id,
                p_operation,
                p_requested,
                p_idempotency_key,
                p_feature,
                p_model,
                p_region,
                v_free,
                p_metadata,
                v_free,
                p_measures => COALESCE(p_measures, '{}'::jsonb),
                p_dimensions => COALESCE(p_dimensions, '{}'::jsonb)
            );
        ELSE
            SELECT *
            INTO v_result
            FROM bursar.charge_usage_with_window(
                p_subject_id,
                p_operation,
                p_requested,
                p_idempotency_key,
                '__included_credits__',
                v_allowance_start,
                v_allowance_end,
                v_free,
                p_model,
                p_region,
                p_metadata,
                p_feature,
                COALESCE(p_measures, '{}'::jsonb),
                COALESCE(p_dimensions, '{}'::jsonb)
            );
        END IF;
    ELSE
        SELECT *
        INTO v_result
        FROM bursar.charge_usage(
            p_subject_id,
            p_operation,
            p_requested,
            p_idempotency_key,
            p_feature,
            p_model,
            p_region,
            0,
            p_metadata,
            0,
            p_measures => COALESCE(p_measures, '{}'::jsonb),
            p_dimensions => COALESCE(p_dimensions, '{}'::jsonb)
        );
    END IF;

    IF v_result.error_code IS NOT NULL THEN
        RETURN QUERY
        SELECT
            v_result.charge_id,
            v_result.ledger_entry_id,
            v_result.charged,
            v_result.allowance_covered,
            v_result.replayed,
            v_result.error_code;
        RETURN;
    END IF;

    IF v_has_assignment AND NOT v_result.replayed THEN
        SELECT event_at
        INTO v_event_at
        FROM bursar.credit_usage_charges
        WHERE id = v_result.charge_id;

        PERFORM bursar.record_operation_quotas(
            v_account,
            v_assignment.plan_id,
            v_assignment.catalog_revision_id,
            p_operation,
            COALESCE(p_measures, '{}'::jsonb),
            v_result.charge_id,
            p_idempotency_key,
            v_event_at,
            jsonb_build_object(
                'usage_charge_id', v_result.charge_id,
                'source', 'usage_charge'
            )
        );
    END IF;

    RETURN QUERY
    SELECT
        v_result.charge_id,
        v_result.ledger_entry_id,
        v_result.charged,
        v_result.allowance_covered,
        v_result.replayed,
        v_result.error_code;
END
$$;
