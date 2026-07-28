-- Effective policy and period-window RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.effective_subject_policy(
    p_subject_id uuid,
    p_operation text
)
RETURNS TABLE(
    catalog_revision_id uuid,
    plan_id uuid,
    minimum_balance numeric,
    max_concurrent integer,
    feature_max_calls integer,
    feature_action text
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    WITH assignment AS (
        SELECT
            a.catalog_revision_id,
            a.plan_id,
            credit_policy.policy_type,
            credit_policy.credit_limit,
            COALESCE(
                operation_policy.max_in_flight,
                admission_policy.max_in_flight
            ) AS max_in_flight
        FROM bursar.credit_accounts ca
        JOIN bursar.account_plan_assignments a ON a.account_id = ca.id
        JOIN bursar.catalog_plans p ON p.id = a.plan_id AND p.catalog_revision_id = a.catalog_revision_id
        LEFT JOIN bursar.catalog_credit_policies AS credit_policy
          ON credit_policy.catalog_revision_id = p.catalog_revision_id
         AND credit_policy.policy_key = p.credit_policy_key
        LEFT JOIN bursar.catalog_admission_policies AS admission_policy
          ON admission_policy.catalog_revision_id = p.catalog_revision_id
         AND admission_policy.policy_key = p.admission_policy_key
        LEFT JOIN bursar.catalog_admission_operation_policies
            AS operation_policy
          ON operation_policy.catalog_revision_id =
             admission_policy.catalog_revision_id
         AND operation_policy.admission_policy_key =
             admission_policy.policy_key
         AND operation_policy.operation_key = p_operation
        WHERE ca.subject_id = p_subject_id AND ca.account_kind = 'personal'
          AND a.starts_at <= now() AND (a.ends_at IS NULL OR a.ends_at > now())
    )
    SELECT
        catalog_revision_id,
        plan_id,
        CASE policy_type
            WHEN 'credit_line' THEN -credit_limit
            ELSE 0
        END,
        max_in_flight,
        NULL::integer,
        NULL::text
    FROM assignment
$$;

CREATE FUNCTION bursar.policy_period_window(
    p_anchor_at timestamptz,
    p_unit text,
    p_count integer,
    p_anchor text,
    p_timezone text
)
RETURNS TABLE(
    window_start timestamptz,
    window_end timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_step interval;

    v_local timestamp;

    v_month integer;

    v_start_month integer;

BEGIN
    IF p_unit NOT IN ('second','minute','hour','day','week','month','year')
       OR p_count IS NULL OR p_count < 1
       OR p_anchor NOT IN ('calendar','plan_assignment','rolling')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = p_timezone) THEN
        RAISE EXCEPTION 'invalid policy period' USING ERRCODE = '22023';

    END IF;

    v_step := CASE p_unit WHEN 'second' THEN make_interval(secs => p_count)
                          WHEN 'minute' THEN make_interval(mins => p_count)
                          WHEN 'hour' THEN make_interval(hours => p_count)
                          WHEN 'day' THEN make_interval(days => p_count)
                          WHEN 'week' THEN make_interval(weeks => p_count)
                          WHEN 'month' THEN make_interval(months => p_count)
                          WHEN 'year' THEN make_interval(years => p_count) END;

    IF p_anchor = 'rolling' THEN
        window_end := now();
        window_start := (
            window_end AT TIME ZONE p_timezone - v_step
        ) AT TIME ZONE p_timezone;
        RETURN NEXT;
        RETURN;
    ELSIF p_anchor = 'calendar' THEN
        v_local := now() AT TIME ZONE p_timezone;

        IF p_unit = 'second' THEN
            window_start := date_bin(
                make_interval(secs => p_count),
                v_local,
                timestamp '2000-01-01'
            ) AT TIME ZONE p_timezone;
        ELSIF p_unit = 'minute' THEN
            window_start := date_bin(
                make_interval(mins => p_count),
                v_local,
                timestamp '2000-01-01'
            ) AT TIME ZONE p_timezone;
        ELSIF p_unit = 'hour' THEN
            window_start := date_bin(
                make_interval(hours => p_count),
                v_local,
                timestamp '2000-01-01'
            ) AT TIME ZONE p_timezone;
        ELSIF p_unit = 'day' THEN
            window_start := date_bin(make_interval(days => p_count), v_local, timestamp '2000-01-01') AT TIME ZONE p_timezone;

        ELSIF p_unit = 'week' THEN
            window_start := date_bin(make_interval(weeks => p_count), v_local, timestamp '2000-01-03') AT TIME ZONE p_timezone;

        ELSE
            v_month := extract(year FROM v_local)::integer * 12 + extract(month FROM v_local)::integer - 1;

            v_start_month := v_month - mod(v_month - 24000, p_count * CASE WHEN p_unit = 'year' THEN 12 ELSE 1 END);

            window_start := make_date(v_start_month / 12, mod(v_start_month, 12) + 1, 1)::timestamp AT TIME ZONE p_timezone;

        END IF;

    ELSE
        window_start := COALESCE(p_anchor_at, now());

        window_end := (window_start AT TIME ZONE p_timezone + v_step) AT TIME ZONE p_timezone;

        WHILE window_end <= now() LOOP
            window_start := window_end;

            window_end := (window_start AT TIME ZONE p_timezone + v_step) AT TIME ZONE p_timezone;

        END LOOP;

        RETURN NEXT;

        RETURN;

    END IF;

    window_end := (window_start AT TIME ZONE p_timezone + v_step) AT TIME ZONE p_timezone;

    RETURN NEXT;

END $$;
