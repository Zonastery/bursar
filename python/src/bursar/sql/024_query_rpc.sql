-- Read-only reporting and catalog resolution RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.get_credit_bucket_balances(
    p_subject_id uuid
)
RETURNS TABLE (
    bucket_key text,
    label text,
    priority integer,
    expires boolean,
    balance numeric
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH active_buckets AS (
        SELECT
            bucket.bucket_key,
            bucket.label,
            bucket.priority,
            bucket.expires
        FROM bursar.catalog_buckets AS bucket
        JOIN bursar.catalog_revisions AS revision
          ON revision.id = bucket.catalog_revision_id
         AND revision.status = 'active'
    ),
    balances AS (
        SELECT
            lot.bucket_key,
            sum(lot.granted - lot.consumed) AS balance
        FROM bursar.credit_lots AS lot
        JOIN bursar.credit_accounts AS account
          ON account.id = lot.account_id
        WHERE account.subject_id = p_subject_id
          AND account.account_kind = 'personal'
          AND lot.consumed < lot.granted
          AND (lot.expires_at IS NULL OR lot.expires_at > now())
        GROUP BY lot.bucket_key
    ),
    all_keys AS (
        SELECT active_buckets.bucket_key FROM active_buckets
        UNION
        SELECT balances.bucket_key FROM balances
    )
    SELECT
        all_keys.bucket_key,
        COALESCE(
            active_buckets.label,
            historical_bucket.label,
            initcap(replace(all_keys.bucket_key, '_', ' '))
        ),
        COALESCE(
            active_buckets.priority,
            historical_bucket.priority,
            2147483647
        ),
        COALESCE(
            active_buckets.expires,
            historical_bucket.expires,
            false
        ),
        COALESCE(balances.balance, 0)
    FROM all_keys
    LEFT JOIN active_buckets USING (bucket_key)
    LEFT JOIN balances USING (bucket_key)
    LEFT JOIN LATERAL (
        SELECT
            bucket.label,
            bucket.priority,
            bucket.expires
        FROM bursar.catalog_buckets AS bucket
        JOIN bursar.catalog_revisions AS revision
          ON revision.id = bucket.catalog_revision_id
        WHERE bucket.bucket_key = all_keys.bucket_key
        ORDER BY revision.revision_no DESC
        LIMIT 1
    ) AS historical_bucket ON active_buckets.bucket_key IS NULL
    ORDER BY
        COALESCE(active_buckets.priority, historical_bucket.priority, 2147483647),
        all_keys.bucket_key
$$;

CREATE FUNCTION bursar.get_subject_entitlements(
    p_subject_id uuid,
    p_at timestamptz DEFAULT now()
)
RETURNS TABLE (
    feature_key text,
    feature_type text,
    feature_value jsonb,
    catalog_revision_id uuid,
    plan_key text,
    value_source text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH assigned AS (
        SELECT
            assignment.catalog_revision_id,
            assignment.plan_key
        FROM bursar.credit_accounts AS account
        JOIN bursar.account_plan_assignments AS assignment
          ON assignment.account_id = account.id
        WHERE account.subject_id = p_subject_id
          AND account.account_kind = 'personal'
          AND assignment.starts_at <= p_at
          AND (
              assignment.ends_at IS NULL
              OR assignment.ends_at > p_at
          )
        LIMIT 1
    ),
    catalog_context AS (
        SELECT
            COALESCE(
                assigned.catalog_revision_id,
                active_revision.id
            ) AS catalog_revision_id,
            assigned.plan_key
        FROM (
            SELECT id
            FROM bursar.catalog_revisions
            WHERE status = 'active'
        ) AS active_revision
        FULL JOIN assigned ON true
        LIMIT 1
    )
    SELECT
        feature.feature_key,
        feature.value_type,
        COALESCE(plan_feature.feature_value, feature.default_value),
        context.catalog_revision_id,
        context.plan_key,
        CASE
            WHEN plan_feature.feature_key IS NULL THEN 'default'
            ELSE 'plan'
        END
    FROM catalog_context AS context
    JOIN bursar.catalog_entitlement_features AS feature
      ON feature.catalog_revision_id = context.catalog_revision_id
    LEFT JOIN bursar.catalog_plan_features AS plan_feature
      ON plan_feature.catalog_revision_id = context.catalog_revision_id
     AND plan_feature.plan_key = context.plan_key
     AND plan_feature.feature_key = feature.feature_key
    WHERE p_subject_id IS NOT NULL
      AND p_at IS NOT NULL
    ORDER BY feature.feature_key
$$;

CREATE FUNCTION bursar.usage_analytics_slice(
    p_start timestamptz,
    p_end timestamptz
)
RETURNS TABLE (
    usage_day date,
    account_id uuid,
    operation text,
    model text,
    region text,
    charged numeric,
    allowance_covered numeric,
    charge_count bigint
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH bounds AS (
        SELECT
            CASE
                WHEN p_start = (
                    date_trunc('day', p_start AT TIME ZONE 'UTC')
                    AT TIME ZONE 'UTC'
                )
                THEN p_start
                ELSE (
                    date_trunc('day', p_start AT TIME ZONE 'UTC')
                    + interval '1 day'
                ) AT TIME ZONE 'UTC'
            END AS full_start,
            (
                date_trunc('day', p_end AT TIME ZONE 'UTC')
                AT TIME ZONE 'UTC'
            ) AS full_end
        WHERE p_start IS NOT NULL
          AND p_end IS NOT NULL
          AND p_end > p_start
    ),
    complete_days AS (
        SELECT
            rollup.usage_day,
            rollup.account_id,
            rollup.operation,
            NULLIF(rollup.model_key, '') AS model,
            NULLIF(rollup.region_key, '') AS region,
            sum(rollup.charged) AS charged,
            sum(rollup.allowance_covered) AS allowance_covered,
            sum(rollup.charge_count)::bigint AS charge_count
        FROM bursar.usage_daily_rollups AS rollup
        CROSS JOIN bounds
        WHERE bounds.full_start < bounds.full_end
          AND rollup.usage_day
                >= (bounds.full_start AT TIME ZONE 'UTC')::date
          AND rollup.usage_day
                < (bounds.full_end AT TIME ZONE 'UTC')::date
        GROUP BY
            rollup.usage_day,
            rollup.account_id,
            rollup.operation,
            rollup.model_key,
            rollup.region_key
    ),
    edge_rows AS (
        SELECT
            (charge.event_at AT TIME ZONE 'UTC')::date AS usage_day,
            charge.account_id,
            charge.operation,
            payload.model,
            payload.region,
            sum(charge.charged) AS charged,
            sum(charge.allowance_covered) AS allowance_covered,
            count(*) AS charge_count
        FROM bursar.credit_usage_charges AS charge
        LEFT JOIN bursar.usage_charge_payloads AS payload
          ON payload.charge_id = charge.id
         AND payload.event_at = charge.event_at
        CROSS JOIN bounds
        WHERE charge.event_at >= p_start
          AND charge.event_at < p_end
          AND charge.billing_disposition = 'billable'
          AND (
              bounds.full_start >= bounds.full_end
              OR charge.event_at < bounds.full_start
              OR charge.event_at >= bounds.full_end
          )
        GROUP BY
            (charge.event_at AT TIME ZONE 'UTC')::date,
            charge.account_id,
            charge.operation,
            payload.model,
            payload.region
    )
    SELECT * FROM complete_days
    UNION ALL
    SELECT * FROM edge_rows
$$;

CREATE FUNCTION bursar.list_ledger(
    p_subject_id uuid,
    p_after_created_at timestamptz DEFAULT NULL,
    p_after_id uuid DEFAULT NULL,
    p_page_size integer DEFAULT 50,
    p_entry_types text [] DEFAULT NULL,
    p_from_at timestamptz DEFAULT NULL,
    p_to_at timestamptz DEFAULT NULL,
    p_usage_only boolean DEFAULT FALSE
)
RETURNS TABLE (
    entry_id uuid,
    account_id uuid,
    actor_user_id uuid,
    amount numeric,
    entry_type text,
    operation text,
    reference_entry_id uuid,
    idempotency_key text,
    metadata jsonb,
    created_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        entry.id,
        entry.account_id,
        account.subject_id,
        entry.amount,
        entry.kind::text,
        entry.operation,
        entry.reference_entry_id,
        entry.idempotency_key,
        entry.metadata,
        entry.created_at
    FROM bursar.credit_ledger_entries AS entry
    JOIN bursar.credit_accounts AS account
      ON account.id = entry.account_id
    WHERE account.subject_id=p_subject_id
      -- SDKs fetch one look-ahead row to determine whether a cursor follows.
      AND p_page_size BETWEEN 1 AND 201
      AND (
          p_entry_types IS NULL
          OR entry.kind::text = ANY(p_entry_types)
      )
      AND (
          NOT p_usage_only
          OR entry.kind = 'usage'
      )
      AND (p_from_at IS NULL OR entry.created_at >= p_from_at)
      AND (p_to_at IS NULL OR entry.created_at < p_to_at)
      AND (
          (p_after_created_at IS NULL AND p_after_id IS NULL)
          OR (
              p_after_created_at IS NOT NULL AND p_after_id IS NOT NULL
              AND (entry.created_at,entry.id)
                    < (p_after_created_at,p_after_id)
          )
      )
    ORDER BY entry.created_at DESC,entry.id DESC
    LIMIT p_page_size
$$;

CREATE FUNCTION bursar.get_ledger_entry(
    p_subject_id uuid,
    p_entry_id uuid
)
RETURNS TABLE (
    entry_id uuid,
    account_id uuid,
    actor_user_id uuid,
    amount numeric,
    entry_type text,
    operation text,
    reference_entry_id uuid,
    idempotency_key text,
    metadata jsonb,
    created_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        entry.id,
        entry.account_id,
        account.subject_id,
        entry.amount,
        entry.kind::text,
        entry.operation,
        entry.reference_entry_id,
        entry.idempotency_key,
        entry.metadata,
        entry.created_at
    FROM bursar.credit_ledger_entries AS entry
    JOIN bursar.credit_accounts AS account
      ON account.id = entry.account_id
    WHERE account.subject_id = p_subject_id
      AND entry.id = p_entry_id
$$;

-- Usage history is sourced from the usage-charge journal rather than the
-- monetary ledger. This keeps allowance-covered events visible without
-- fabricating a zero-value ledger entry.
CREATE FUNCTION bursar.list_usage_charges(
    p_subject_id uuid,
    p_after_event_at timestamptz DEFAULT NULL,
    p_after_id uuid DEFAULT NULL,
    p_page_size integer DEFAULT 100,
    p_from_at timestamptz DEFAULT NULL,
    p_to_at timestamptz DEFAULT NULL,
    p_include_record_only boolean DEFAULT TRUE
)
RETURNS TABLE (
    usage_id uuid,
    account_id uuid,
    operation text,
    requested numeric,
    charged numeric,
    allowance_requested numeric,
    allowance_covered numeric,
    billing_disposition text,
    feature text,
    model text,
    region text,
    event_at timestamptz,
    idempotency_key text,
    metadata jsonb,
    created_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        charge.id,
        charge.account_id,
        charge.operation,
        charge.requested,
        charge.charged,
        charge.allowance_requested,
        charge.allowance_covered,
        charge.billing_disposition,
        payload.feature,
        payload.model,
        payload.region,
        charge.event_at,
        charge.idempotency_key,
        COALESCE(payload.metadata, '{}'::jsonb),
        charge.created_at
    FROM bursar.credit_usage_charges AS charge
    JOIN bursar.credit_accounts AS account
      ON account.id = charge.account_id
    LEFT JOIN bursar.usage_charge_payloads AS payload
      ON payload.charge_id = charge.id
     AND payload.event_at = charge.event_at
    WHERE account.subject_id = p_subject_id
      -- SDKs fetch one look-ahead row to determine whether a cursor follows.
      AND p_page_size BETWEEN 1 AND 201
      AND (p_from_at IS NULL OR charge.event_at >= p_from_at)
      AND (p_to_at IS NULL OR charge.event_at < p_to_at)
      AND (p_include_record_only OR charge.billing_disposition = 'billable')
      AND (
          (p_after_event_at IS NULL AND p_after_id IS NULL)
          OR (
              p_after_event_at IS NOT NULL AND p_after_id IS NOT NULL
              AND (charge.event_at, charge.id)
                    < (p_after_event_at, p_after_id)
          )
      )
    ORDER BY charge.event_at DESC, charge.id DESC
    LIMIT p_page_size
$$;

CREATE FUNCTION bursar.spend_by_user(
    p_start timestamptz,
    p_end timestamptz
)
RETURNS TABLE (
    subject_id uuid,
    total_spend numeric,
    charge_count bigint
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT
        account.subject_id,
        sum(usage.charged),
        sum(usage.charge_count)::bigint
    FROM bursar.usage_analytics_slice(p_start, p_end) AS usage
    JOIN bursar.credit_accounts AS account ON account.id = usage.account_id
    GROUP BY account.subject_id
    ORDER BY sum(usage.charged) DESC
$$;

CREATE FUNCTION bursar.spend_by_model(
    p_start timestamptz,
    p_end timestamptz
)
RETURNS TABLE (
    model text,
    total_spend numeric,
    charge_count bigint
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT
        COALESCE(model, 'unknown'),
        sum(charged),
        sum(charge_count)::bigint
    FROM bursar.usage_analytics_slice(p_start, p_end)
    GROUP BY COALESCE(model, 'unknown')
    ORDER BY sum(charged) DESC
$$;

CREATE FUNCTION bursar.daily_spend(
    p_start timestamptz,
    p_end timestamptz
)
RETURNS TABLE (
    day date,
    total_spend numeric,
    charge_count bigint
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT usage_day, sum(charged), sum(charge_count)::bigint
    FROM bursar.usage_analytics_slice(p_start, p_end)
    GROUP BY usage_day
    ORDER BY usage_day
$$;

CREATE FUNCTION bursar.aggregate_usage_stats(
    p_start timestamptz,
    p_end timestamptz
)
RETURNS TABLE (
    total_credits_consumed numeric,
    active_users bigint,
    avg_daily_spend numeric,
    top_model text,
    top_user uuid
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH usage AS (
        SELECT
            slice.charged,
            slice.charge_count,
            slice.model,
            account.subject_id
        FROM bursar.usage_analytics_slice(p_start, p_end) AS slice
        JOIN bursar.credit_accounts AS account
          ON account.id = slice.account_id
    ),
    totals AS (
        SELECT
            COALESCE(sum(charged), 0) AS consumed,
            count(DISTINCT subject_id) AS users
        FROM usage
    ),
    model_rank AS (
        SELECT
            COALESCE(model, 'unknown') AS model,
            sum(charged) AS spend
        FROM usage
        GROUP BY COALESCE(model, 'unknown')
        ORDER BY spend DESC, model
        LIMIT 1
    ),
    user_rank AS (
        SELECT subject_id, sum(charged) AS spend
        FROM usage
        GROUP BY subject_id
        ORDER BY spend DESC, subject_id
        LIMIT 1
    )
    SELECT
        totals.consumed,
        totals.users,
        totals.consumed / greatest(
            ceil(
                extract(epoch FROM (p_end - p_start)) / 86400
            ),
            1
        ),
        COALESCE((SELECT model FROM model_rank), ''),
        (SELECT subject_id FROM user_rank)
    FROM totals
$$;

CREATE FUNCTION bursar.get_billing_customer(
    p_subject_id uuid,
    p_provider text DEFAULT NULL
)
RETURNS SETOF bursar.billing_customers
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_customers
 WHERE subject_id=p_subject_id
 AND (p_provider IS NULL OR provider=p_provider)
 AND provider_environment=bursar.current_provider_environment()
 ORDER BY provider
$$;

CREATE FUNCTION bursar.get_billing_customer_by_provider(
    p_provider text,
    p_provider_customer_id text
)
RETURNS SETOF bursar.billing_customers
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_customers
 WHERE provider=p_provider
 AND provider_environment=bursar.current_provider_environment()
 AND provider_customer_id=p_provider_customer_id
$$;

CREATE FUNCTION bursar.get_billing_subscription_by_provider(
    p_provider text,
    p_provider_subscription_id text
)
RETURNS SETOF bursar.billing_subscriptions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_subscriptions
 WHERE provider=p_provider
 AND provider_environment=bursar.current_provider_environment()
 AND provider_subscription_id=p_provider_subscription_id
$$;

CREATE FUNCTION bursar.list_billing_subscriptions(
    p_subject_id uuid
)
RETURNS SETOF bursar.billing_subscriptions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_subscriptions
 WHERE subject_id=p_subject_id
 AND provider_environment=bursar.current_provider_environment()
 ORDER BY created_at,id
$$;

CREATE FUNCTION bursar.get_billing_payment_by_provider(
    p_provider text,
    p_provider_payment_id text
)
RETURNS SETOF bursar.billing_payments
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_payments
 WHERE provider=p_provider
 AND provider_environment=bursar.current_provider_environment()
 AND provider_payment_id=p_provider_payment_id
$$;

CREATE FUNCTION bursar.get_billing_preferences(
    p_subject_id uuid
)
RETURNS SETOF bursar.billing_preferences
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_preferences
 WHERE subject_id=p_subject_id
$$;

CREATE FUNCTION bursar.get_auto_recharge_profile(
    p_subject_id uuid
)
RETURNS SETOF bursar.billing_auto_recharge_profiles
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_auto_recharge_profiles
 WHERE subject_id=p_subject_id
 AND provider_environment=bursar.current_provider_environment()
$$;

CREATE FUNCTION bursar.get_auto_recharge_attempt(
    p_attempt_id uuid
)
RETURNS SETOF bursar.billing_auto_recharge_attempts
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_auto_recharge_attempts
 WHERE id=p_attempt_id
$$;

CREATE FUNCTION bursar.resolve_catalog_offer(
    p_provider text,
    p_lookup_type text,
    p_lookup_value text
)
RETURNS SETOF bursar.catalog_offers
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT o.*
 FROM bursar.catalog_provider_refs r
 JOIN bursar.catalog_revisions cr
 ON cr.id=r.catalog_revision_id
 JOIN bursar.catalog_offers o
 ON o.catalog_revision_id=r.catalog_revision_id
 AND o.offer_key=r.object_key
 WHERE r.provider=p_provider
 AND cr.status='active'
 AND r.provider_environment=bursar.current_provider_environment()
 AND r.lookup_type=p_lookup_type
 AND r.lookup_value=p_lookup_value
 AND r.object_type='offer'
 ORDER BY cr.revision_no DESC
 LIMIT 1
$$;

CREATE FUNCTION bursar.resolve_catalog_topup(
    p_provider text,
    p_lookup_type text,
    p_lookup_value text
)
RETURNS SETOF bursar.catalog_topups
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT t.*
 FROM bursar.catalog_provider_refs r
 JOIN bursar.catalog_revisions cr
 ON cr.id=r.catalog_revision_id
 JOIN bursar.catalog_topups t
 ON t.catalog_revision_id=r.catalog_revision_id
 AND t.topup_key=r.object_key
 WHERE r.provider=p_provider
 AND cr.status='active'
 AND r.provider_environment=bursar.current_provider_environment()
 AND r.lookup_type=p_lookup_type
 AND r.lookup_value=p_lookup_value
 AND r.object_type='topup'
 ORDER BY cr.revision_no DESC
 LIMIT 1
$$;

CREATE FUNCTION bursar.get_credit_state(
    p_subject_id uuid
)
RETURNS TABLE (
    balance numeric,
    reserved numeric,
    available numeric,
    lifetime_purchased numeric
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH account AS (
        SELECT id, balance
        FROM bursar.credit_accounts
        WHERE subject_id = p_subject_id
          AND account_kind = 'personal'
    ),
    expired AS (
        SELECT COALESCE(sum(lot.granted - lot.consumed), 0) AS amount
        FROM account
        JOIN bursar.credit_lots AS lot ON lot.account_id = account.id
        WHERE lot.consumed < lot.granted
          AND lot.expires_at <= now()
    ),
    holds AS (
        SELECT COALESCE(
            sum(lease.reserved_amount - lease.reserved_allowance),
            0
        ) AS amount
        FROM account
        JOIN bursar.credit_leases AS lease ON lease.account_id = account.id
        WHERE lease.status = 'active'
          AND lease.expires_at > now()
    ),
    lifetime AS (
        SELECT COALESCE(sum(entry.amount), 0) AS purchased
        FROM account
        JOIN bursar.credit_ledger_entries AS entry
          ON entry.account_id = account.id
        WHERE entry.kind = 'purchase'
    )
    SELECT
        account.balance - expired.amount,
        holds.amount,
        account.balance - expired.amount - holds.amount,
        lifetime.purchased
    FROM account
    CROSS JOIN expired
    CROSS JOIN holds
    CROSS JOIN lifetime
$$;

CREATE FUNCTION bursar.get_credit_operation_details(
    p_subject_id uuid,
    p_ledger_entry_id uuid DEFAULT NULL,
    p_idempotency_key text DEFAULT NULL
)
RETURNS TABLE (
    balance_after numeric,
    allowance_covered numeric,
    bucket_breakdown jsonb
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH account AS (
        SELECT id, balance
        FROM bursar.credit_accounts
        WHERE subject_id = p_subject_id
          AND account_kind = 'personal'
    ),
    charge AS (
        SELECT
            usage_charge.ledger_entry_id,
            usage_charge.allowance_covered
        FROM bursar.credit_usage_charges AS usage_charge
        JOIN account ON account.id = usage_charge.account_id
        WHERE (
            p_ledger_entry_id IS NOT NULL
            AND usage_charge.ledger_entry_id = p_ledger_entry_id
        ) OR (
            p_idempotency_key IS NOT NULL
            AND usage_charge.idempotency_key = p_idempotency_key
        )
        ORDER BY usage_charge.created_at DESC, usage_charge.id DESC
        LIMIT 1
    ),
    allocation AS (
        SELECT
            bucket.account_id,
            jsonb_object_agg(
                bucket.bucket_key,
                bucket.amount
                ORDER BY bucket.bucket_key
            ) AS bucket_breakdown
        FROM (
            SELECT
                lot.account_id,
                lot.bucket_key,
                sum(lot_allocation.amount) AS amount
            FROM bursar.credit_lot_allocations AS lot_allocation
            JOIN charge
              ON charge.ledger_entry_id = lot_allocation.debit_entry_id
            JOIN bursar.credit_lots AS lot
              ON lot.id = lot_allocation.lot_id
            GROUP BY lot.account_id, lot.bucket_key
        ) AS bucket
        GROUP BY bucket.account_id
    )
    SELECT
        COALESCE(entry.balance_after, account.balance),
        COALESCE(charge.allowance_covered, 0),
        COALESCE(allocation.bucket_breakdown, '{}'::jsonb)
    FROM account
    LEFT JOIN charge ON true
    LEFT JOIN bursar.credit_ledger_entries AS entry
      ON entry.id = charge.ledger_entry_id
    LEFT JOIN allocation ON allocation.account_id = account.id
$$;

CREATE FUNCTION bursar.get_credit_grant_details(
    p_subject_id uuid,
    p_ledger_entry_id uuid
)
RETURNS TABLE (
    bucket_key text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT lot.bucket_key
    FROM bursar.credit_lots AS lot
    JOIN bursar.credit_accounts AS account
      ON account.id = lot.account_id
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
      AND lot.source_entry_id = p_ledger_entry_id
$$;

CREATE FUNCTION bursar.get_credit_lease(
    p_subject_id uuid,
    p_lease_id uuid
)
RETURNS SETOF bursar.credit_leases
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT lease.*
    FROM bursar.credit_leases AS lease
    JOIN bursar.credit_accounts AS account ON account.id = lease.account_id
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
      AND lease.id = p_lease_id
$$;

CREATE FUNCTION bursar.get_credit_lease_pricing_context(
    p_subject_id uuid,
    p_lease_id uuid
)
RETURNS TABLE (
    catalog_revision_no bigint,
    plan_id uuid,
    plan_key text,
    rate_card text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        revision.revision_no,
        lease.plan_id,
        plan.plan_key,
        plan.rate_card
    FROM bursar.credit_leases AS lease
    JOIN bursar.credit_accounts AS account
      ON account.id = lease.account_id
    JOIN bursar.catalog_revisions AS revision
      ON revision.id = lease.catalog_revision_id
    LEFT JOIN bursar.catalog_plans AS plan
      ON plan.id = lease.plan_id
     AND plan.catalog_revision_id = lease.catalog_revision_id
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
      AND lease.id = p_lease_id
$$;

CREATE FUNCTION bursar.resolve_active_plan(
    p_plan_reference text
)
RETURNS SETOF bursar.catalog_plans
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT plan.*
    FROM bursar.catalog_plans AS plan
    JOIN bursar.catalog_revisions AS revision
      ON revision.id = plan.catalog_revision_id
     AND revision.status = 'active'
    WHERE plan.id::text = p_plan_reference
       OR plan.plan_key = p_plan_reference
    ORDER BY revision.revision_no DESC
    LIMIT 1
$$;

CREATE FUNCTION bursar.get_subject_plan(
    p_subject_id uuid
)
RETURNS TABLE (
    user_id uuid,
    plan_assigned_at timestamptz,
    plan_assignment_ends_at timestamptz,
    assignment_source_type text,
    assignment_source_id uuid,
    catalog_revision_pinned boolean,
    plan_id uuid,
    plan_key text,
    plan_label text,
    rate_card text,
    allowed_operations text [],
    credit_allowance_amount numeric,
    credit_allowance_priority integer,
    credit_allowance_reset_unit text,
    credit_allowance_reset_count integer,
    credit_allowance_reset_anchor text,
    credit_allowance_reset_timezone text,
    entitlements jsonb,
    credit_policy_type text,
    credit_limit numeric,
    admission_max_in_flight integer,
    operation_admission jsonb,
    catalog_revision_no bigint
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        account.subject_id,
        assignment.starts_at,
        assignment.ends_at,
        assignment.source_type,
        assignment.source_id,
        assignment.catalog_revision_pinned,
        plan.id,
        plan.plan_key,
        plan.display_name,
        plan.rate_card,
        plan.allowed_operations,
        plan.credit_allowance_amount,
        plan.credit_allowance_priority,
        plan.credit_allowance_reset_unit,
        plan.credit_allowance_reset_count,
        plan.credit_allowance_reset_anchor,
        plan.credit_allowance_reset_timezone,
        COALESCE(features.entitlements, '{}'::jsonb),
        credit_policy.policy_type,
        credit_policy.credit_limit,
        admission_policy.max_in_flight,
        COALESCE(operation_policy.per_operation, '{}'::jsonb),
        revision.revision_no
    FROM bursar.account_plan_assignments AS assignment
    JOIN bursar.credit_accounts AS account
      ON account.id = assignment.account_id
    JOIN bursar.catalog_plans AS plan
      ON plan.id = assignment.plan_id
     AND plan.catalog_revision_id = assignment.catalog_revision_id
    JOIN bursar.catalog_revisions AS revision
      ON revision.id = plan.catalog_revision_id
    LEFT JOIN bursar.catalog_credit_policies AS credit_policy
      ON credit_policy.catalog_revision_id = plan.catalog_revision_id
     AND credit_policy.policy_key = plan.credit_policy_key
    LEFT JOIN bursar.catalog_admission_policies AS admission_policy
      ON admission_policy.catalog_revision_id = plan.catalog_revision_id
     AND admission_policy.policy_key = plan.admission_policy_key
    LEFT JOIN LATERAL (
        SELECT jsonb_object_agg(
            entitlement.feature_key,
            jsonb_build_object('value', entitlement.feature_value)
            ORDER BY entitlement.feature_key
        ) AS entitlements
        FROM bursar.get_subject_entitlements(
            account.subject_id,
            now()
        ) AS entitlement
    ) AS features ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_object_agg(
            policy.operation_key,
            jsonb_build_object(
                'max_in_flight',
                policy.max_in_flight
            )
            ORDER BY policy.operation_key
        ) AS per_operation
        FROM bursar.catalog_admission_operation_policies AS policy
        WHERE policy.catalog_revision_id = plan.catalog_revision_id
          AND policy.admission_policy_key = plan.admission_policy_key
    ) AS operation_policy ON true
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
      AND assignment.starts_at <= now()
      AND (
          assignment.ends_at IS NULL
          OR assignment.ends_at > now()
      )
    LIMIT 1
$$;

CREATE FUNCTION bursar.get_subject_allowance(
    p_subject_id uuid,
    p_window_start timestamptz DEFAULT NULL
)
RETURNS TABLE (
    plan_id uuid,
    allowance_remaining numeric,
    period_start timestamptz,
    period_end timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH assigned AS (
        SELECT
            account.id AS account_id,
            assignment.plan_id,
            assignment.catalog_revision_id,
            assignment.starts_at,
            plan.credit_allowance_amount AS allowance,
            plan.credit_allowance_reset_unit AS period_unit,
            plan.credit_allowance_reset_count AS period_count,
            plan.credit_allowance_reset_anchor AS period_anchor,
            plan.credit_allowance_reset_timezone AS period_timezone
        FROM bursar.credit_accounts AS account
        JOIN bursar.account_plan_assignments AS assignment
          ON assignment.account_id = account.id
        JOIN bursar.catalog_plans AS plan
          ON plan.id = assignment.plan_id
         AND plan.catalog_revision_id = assignment.catalog_revision_id
        WHERE account.subject_id = p_subject_id
          AND account.account_kind = 'personal'
          AND assignment.starts_at <= now()
          AND (
              assignment.ends_at IS NULL
              OR assignment.ends_at > now()
          )
          AND plan.credit_allowance_amount IS NOT NULL
        LIMIT 1
    ),
    current_window AS (
        SELECT assigned.*, period.window_start, period.window_end
        FROM assigned
        CROSS JOIN LATERAL bursar.policy_period_window(
            assigned.starts_at,
            assigned.period_unit,
            assigned.period_count,
            assigned.period_anchor,
            assigned.period_timezone
        ) AS period
    ),
    rolling_usage AS (
        SELECT COALESCE(sum(charge.allowance_covered), 0) AS consumed
        FROM current_window
        JOIN bursar.credit_usage_charges AS charge
          ON charge.account_id = current_window.account_id
         AND charge.plan_id = current_window.plan_id
         AND charge.catalog_revision_id =
             current_window.catalog_revision_id
         AND charge.event_at > current_window.window_start
         AND charge.event_at <= current_window.window_end
        WHERE current_window.period_anchor = 'rolling'
    ),
    rolling_holds AS (
        SELECT COALESCE(sum(lease.reserved_allowance), 0) AS reserved
        FROM current_window
        JOIN bursar.credit_leases AS lease
          ON lease.account_id = current_window.account_id
         AND lease.plan_id = current_window.plan_id
         AND lease.catalog_revision_id =
             current_window.catalog_revision_id
         AND lease.status = 'active'
         AND lease.expires_at > now()
        WHERE current_window.period_anchor = 'rolling'
    )
    SELECT
        current_window.plan_id,
        greatest(
            current_window.allowance
            - CASE
                WHEN current_window.period_anchor = 'rolling'
                    THEN COALESCE(
                        (SELECT consumed FROM rolling_usage),
                        0
                    )
                ELSE COALESCE(allowance_window.consumed, 0)
              END
            - CASE
                WHEN current_window.period_anchor = 'rolling'
                    THEN COALESCE(
                        (SELECT reserved FROM rolling_holds),
                        0
                    )
                ELSE COALESCE(allowance_window.reserved, 0)
              END,
            0
        ),
        current_window.window_start,
        current_window.window_end
    FROM current_window
    LEFT JOIN bursar.allowance_windows AS allowance_window
      ON allowance_window.account_id = current_window.account_id
     AND allowance_window.plan_id = current_window.plan_id
     AND allowance_window.catalog_revision_id =
         current_window.catalog_revision_id
     AND allowance_window.allowance_key = '__included_credits__'
     AND allowance_window.window_start = current_window.window_start
     AND allowance_window.window_end = current_window.window_end
    WHERE p_window_start IS NULL
       OR current_window.window_start = p_window_start
    LIMIT 1
$$;

CREATE FUNCTION bursar.get_subject_quota_state(
    p_subject_id uuid,
    p_quota_key text DEFAULT NULL
)
RETURNS TABLE (
    user_id uuid,
    quota_key text,
    operation_key text,
    measure_key text,
    quota_limit numeric,
    consumed numeric,
    reserved numeric,
    remaining numeric,
    overage numeric,
    enforcement text,
    window_start timestamptz,
    window_end timestamptz,
    emit_at_percent integer []
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH assigned AS (
        SELECT
            account.subject_id,
            account.id AS account_id,
            assignment.plan_id,
            assignment.catalog_revision_id,
            assignment.plan_key,
            assignment.starts_at
        FROM bursar.credit_accounts AS account
        JOIN bursar.account_plan_assignments AS assignment
          ON assignment.account_id = account.id
        WHERE account.subject_id = p_subject_id
          AND account.account_kind = 'personal'
          AND assignment.starts_at <= now()
          AND (
              assignment.ends_at IS NULL
              OR assignment.ends_at > now()
          )
        LIMIT 1
    ),
    policy AS (
        SELECT
            assigned.*,
            quota.id AS catalog_quota_id,
            quota.quota_key,
            quota.operation_key,
            quota.measure_key,
            quota.quota_limit,
            quota.enforcement,
            quota.emit_at_percent,
            period.window_start,
            period.window_end,
            period.is_rolling
        FROM assigned
        JOIN bursar.catalog_plan_quotas AS quota
          ON quota.catalog_revision_id = assigned.catalog_revision_id
         AND quota.plan_key = assigned.plan_key
        CROSS JOIN LATERAL bursar.quota_policy_window(
            assigned.starts_at,
            quota.window_policy
        ) AS period
        WHERE p_quota_key IS NULL
           OR quota.quota_key = p_quota_key
    ),
    state AS (
        SELECT
            policy.*,
            CASE
                WHEN policy.is_rolling THEN
                    bursar.quota_lineage_consumed(
                        policy.account_id,
                        policy.plan_key,
                        policy.quota_key,
                        policy.operation_key,
                        policy.measure_key,
                        policy.starts_at,
                        policy.window_start,
                        policy.window_end
                    )
                ELSE COALESCE(quota_window.consumed, 0)
            END AS consumed,
            CASE
                WHEN policy.is_rolling THEN
                    bursar.quota_lineage_reserved(
                        policy.account_id,
                        policy.plan_key,
                        policy.quota_key,
                        policy.operation_key,
                        policy.measure_key,
                        policy.starts_at,
                        policy.window_start,
                        policy.window_end
                    )
                ELSE COALESCE(quota_window.reserved, 0)
            END AS reserved
        FROM policy
        LEFT JOIN bursar.quota_windows AS quota_window
          ON NOT policy.is_rolling
         AND quota_window.account_id = policy.account_id
         AND quota_window.plan_id = policy.plan_id
         AND quota_window.catalog_revision_id =
             policy.catalog_revision_id
         AND quota_window.quota_key = policy.quota_key
         AND quota_window.window_start = policy.window_start
         AND quota_window.window_end = policy.window_end
    )
    SELECT
        state.subject_id,
        state.quota_key,
        state.operation_key,
        state.measure_key,
        state.quota_limit,
        state.consumed,
        state.reserved,
        greatest(
            state.quota_limit - state.consumed - state.reserved,
            0
        ),
        greatest(
            state.consumed + state.reserved - state.quota_limit,
            0
        ),
        state.enforcement,
        state.window_start,
        state.window_end,
        state.emit_at_percent
    FROM state
    ORDER BY state.quota_key
$$;

CREATE FUNCTION bursar.list_subject_quota_events(
    p_subject_id uuid,
    p_after timestamptz DEFAULT NULL,
    p_limit integer DEFAULT 100,
    p_idempotency_key text DEFAULT NULL,
    p_after_id uuid DEFAULT NULL
)
RETURNS TABLE (
    event_id uuid,
    quota_key text,
    operation_key text,
    measure_key text,
    event_type text,
    threshold_percent integer,
    idempotency_key text,
    usage_charge_id uuid,
    created_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        event.id,
        quota_window.quota_key,
        quota_window.operation_key,
        quota_window.measure_key,
        event.event_type,
        event.threshold_percent,
        event.idempotency_key,
        event.usage_charge_id,
        event.created_at
    FROM bursar.quota_events AS event
    JOIN bursar.quota_windows AS quota_window
      ON quota_window.id = event.quota_window_id
    JOIN bursar.credit_accounts AS account
      ON account.id = quota_window.account_id
    WHERE account.subject_id = p_subject_id
      AND account.account_kind = 'personal'
      AND p_limit BETWEEN 1 AND 500
      AND (
          p_after IS NULL
          OR (
              p_after_id IS NULL
              AND event.created_at > p_after
          )
          OR (event.created_at, event.id) > (p_after, p_after_id)
      )
      AND (
          p_idempotency_key IS NULL
          OR event.idempotency_key = p_idempotency_key
      )
    ORDER BY event.created_at DESC, event.id DESC
    LIMIT p_limit
$$;

CREATE FUNCTION bursar.get_checkout_intent(
    p_intent_id uuid,
    p_subject_id uuid
)
RETURNS SETOF bursar.billing_checkout_intents
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT *
    FROM bursar.billing_checkout_intents
    WHERE id = p_intent_id
      AND subject_id = p_subject_id
      AND provider_environment = bursar.current_provider_environment()
$$;

CREATE FUNCTION bursar.resolve_active_catalog_offer(
    p_offer_key text
)
RETURNS SETOF bursar.catalog_offers
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT offer.*
    FROM bursar.catalog_offers AS offer
    JOIN bursar.catalog_revisions AS revision
      ON revision.id = offer.catalog_revision_id
     AND revision.status = 'active'
    WHERE offer.offer_key = p_offer_key
    ORDER BY revision.revision_no DESC
    LIMIT 1
$$;

CREATE FUNCTION bursar.get_catalog_offer_context(
    p_offer_id uuid,
    p_catalog_revision_id uuid
)
RETURNS TABLE (
    offer_key text,
    plan_id uuid,
    plan_key text,
    billing_unit text,
    billing_count integer
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        offer.offer_key,
        plan.id,
        offer.plan_key,
        offer.billing_unit,
        offer.billing_count
    FROM bursar.catalog_offers AS offer
    LEFT JOIN bursar.catalog_plans AS plan
      ON plan.catalog_revision_id = offer.catalog_revision_id
     AND plan.plan_key = offer.plan_key
    WHERE offer.id = p_offer_id
      AND offer.catalog_revision_id = p_catalog_revision_id
$$;

CREATE FUNCTION bursar.get_open_billing_subscription_change(
    p_provider text,
    p_provider_subscription_id text
)
RETURNS SETOF bursar.billing_subscription_changes
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT change.*
    FROM bursar.billing_subscription_changes AS change
    JOIN bursar.billing_subscriptions AS subscription
      ON subscription.id = change.subscription_id
    WHERE subscription.provider = p_provider
      AND subscription.provider_environment =
          bursar.current_provider_environment()
      AND subscription.provider_subscription_id =
          p_provider_subscription_id
      AND change.state IN ('awaiting_payment', 'scheduled')
    ORDER BY change.created_at DESC
    LIMIT 1
$$;

CREATE FUNCTION bursar.get_billing_subscription_change(
    p_change_id bigint
)
RETURNS SETOF bursar.billing_subscription_changes
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT *
    FROM bursar.billing_subscription_changes
    WHERE id = p_change_id
$$;

CREATE FUNCTION bursar.get_billing_credit_grant_by_payment(
    p_payment_id uuid
)
RETURNS SETOF bursar.billing_credit_grants
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT *
    FROM bursar.billing_credit_grants
    WHERE payment_id = p_payment_id
    ORDER BY created_at, id
    LIMIT 1
$$;

CREATE FUNCTION bursar.list_billing_invoices(
    p_subject_id uuid,
    p_before_sort_at timestamptz DEFAULT NULL,
    p_before_id uuid DEFAULT NULL,
    p_page_size integer DEFAULT 100
)
RETURNS TABLE (
    id uuid,
    subject_id uuid,
    provider text,
    provider_environment text,
    provider_invoice_id text,
    subscription_id uuid,
    status text,
    amount_due_minor bigint,
    amount_paid_minor bigint,
    currency text,
    period_start timestamptz,
    period_end timestamptz,
    provider_updated_at timestamptz,
    metadata jsonb,
    created_at timestamptz,
    updated_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        invoice.id,
        invoice.subject_id,
        invoice.provider,
        invoice.provider_environment,
        invoice.provider_invoice_id,
        invoice.subscription_id,
        invoice.status,
        invoice.amount_due_minor,
        invoice.amount_paid_minor,
        invoice.currency,
        invoice.period_start,
        invoice.period_end,
        invoice.provider_updated_at,
        invoice.metadata,
        invoice.created_at,
        invoice.updated_at
    FROM bursar.billing_invoices AS invoice
    WHERE invoice.subject_id = p_subject_id
      AND invoice.provider_environment =
          bursar.current_provider_environment()
      AND p_page_size BETWEEN 1 AND 500
      AND (
          (p_before_sort_at IS NULL AND p_before_id IS NULL)
          OR (
              p_before_sort_at IS NOT NULL
              AND p_before_id IS NOT NULL
              AND (
                  COALESCE(invoice.period_end, invoice.created_at),
                  invoice.id
              ) < (p_before_sort_at, p_before_id)
          )
      )
    ORDER BY
        COALESCE(invoice.period_end, invoice.created_at) DESC,
        invoice.id DESC
    LIMIT p_page_size
$$;

CREATE FUNCTION bursar.get_auto_recharge_attempt_by_provider(
    p_provider text,
    p_provider_attempt_id text
)
RETURNS SETOF bursar.billing_auto_recharge_attempts
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT *
    FROM bursar.billing_auto_recharge_attempts
    WHERE provider = p_provider
      AND provider_environment = bursar.current_provider_environment()
      AND provider_attempt_id = p_provider_attempt_id
$$;

CREATE FUNCTION bursar.count_auto_recharge_attempts(
    p_subject_id uuid,
    p_since timestamptz
)
RETURNS integer
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT count(*)::integer
    FROM bursar.billing_auto_recharge_attempts
    WHERE subject_id = p_subject_id
      AND provider_environment = bursar.current_provider_environment()
      AND created_at >= p_since
      AND state IN (
          'submitted',
          'processing',
          'succeeded',
          'action_required'
      )
$$;
