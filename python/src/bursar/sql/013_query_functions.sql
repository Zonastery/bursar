CREATE FUNCTION bursar.get_credit_bucket_balances(p_subject_id uuid)
RETURNS TABLE(bucket_key text,balance numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    WITH active_buckets AS (
        SELECT b.bucket_key
        FROM bursar.catalog_buckets b
        JOIN bursar.catalog_revisions r
          ON r.id=b.catalog_revision_id AND r.status='active'
    ),
    balances AS (
        SELECT l.bucket_key, SUM(l.granted-l.consumed) AS balance
        FROM bursar.credit_lots l
        JOIN bursar.credit_accounts a ON a.id=l.account_id
        WHERE a.subject_id=p_subject_id
          AND a.account_kind='personal'
          AND l.consumed<l.granted
          AND (l.expires_at IS NULL OR l.expires_at>now())
        GROUP BY l.bucket_key
    )
    SELECT COALESCE(a.bucket_key,b.bucket_key), COALESCE(b.balance,0)
    FROM active_buckets a
    FULL JOIN balances b USING (bucket_key)
    ORDER BY 1
$$;

CREATE FUNCTION bursar.spend_by_operation(p_start timestamptz,p_end timestamptz)
RETURNS TABLE(operation text,total_spend numeric,charge_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT operation,SUM(charged),COUNT(*)
    FROM bursar.credit_usage_charges
    WHERE created_at>=p_start AND created_at<p_end
    GROUP BY operation
    ORDER BY SUM(charged) DESC
$$;

CREATE FUNCTION bursar.list_feature_limit_events(
    p_subject_id uuid,
    p_start timestamptz DEFAULT NULL,
    p_end timestamptz DEFAULT NULL
)
RETURNS SETOF bursar.feature_limit_events
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT e.*
    FROM bursar.feature_limit_events e
    JOIN bursar.credit_accounts a ON a.id=e.account_id
    WHERE a.subject_id=p_subject_id
      AND (p_start IS NULL OR e.created_at>=p_start)
      AND (p_end IS NULL OR e.created_at<p_end)
    ORDER BY e.created_at,e.id
$$;

CREATE FUNCTION bursar.list_ledger(
    p_subject_id uuid,
    p_after_created_at timestamptz DEFAULT NULL,
    p_after_id uuid DEFAULT NULL,
    p_page_size integer DEFAULT 50
)
RETURNS SETOF bursar.credit_ledger_entries
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT e.*
    FROM bursar.credit_ledger_entries e
    JOIN bursar.credit_accounts a ON a.id=e.account_id
    WHERE a.subject_id=p_subject_id
      AND p_page_size BETWEEN 1 AND 200
      AND (
          (p_after_created_at IS NULL AND p_after_id IS NULL)
          OR (
              p_after_created_at IS NOT NULL AND p_after_id IS NOT NULL
              AND (e.created_at,e.id)<(p_after_created_at,p_after_id)
          )
      )
    ORDER BY e.created_at DESC,e.id DESC
    LIMIT p_page_size
$$;

CREATE FUNCTION bursar.spend_by_user(p_start timestamptz,p_end timestamptz)
RETURNS TABLE(subject_id uuid,total_spend numeric,charge_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT a.subject_id,SUM(c.charged),COUNT(*)
    FROM bursar.credit_usage_charges c
    JOIN bursar.credit_accounts a ON a.id=c.account_id
    WHERE c.created_at>=p_start AND c.created_at<p_end
    GROUP BY a.subject_id
    ORDER BY SUM(c.charged) DESC
$$;

CREATE FUNCTION bursar.spend_by_model(p_start timestamptz,p_end timestamptz)
RETURNS TABLE(model text,total_spend numeric,charge_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT COALESCE(model,'unknown'),SUM(charged),COUNT(*)
    FROM bursar.credit_usage_charges
    WHERE created_at>=p_start AND created_at<p_end
    GROUP BY COALESCE(model,'unknown')
    ORDER BY SUM(charged) DESC
$$;

CREATE FUNCTION bursar.daily_spend(p_start timestamptz,p_end timestamptz)
RETURNS TABLE(day date,total_spend numeric,charge_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT (created_at AT TIME ZONE 'UTC')::date,SUM(charged),COUNT(*)
    FROM bursar.credit_usage_charges
    WHERE created_at>=p_start AND created_at<p_end
    GROUP BY 1
 ORDER BY 1
$$;

CREATE FUNCTION bursar.get_billing_customer(
 p_subject_id uuid,p_provider text DEFAULT NULL
)
RETURNS SETOF bursar.billing_customers
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_customers
 WHERE subject_id=p_subject_id
 AND (p_provider IS NULL OR provider=p_provider)
 ORDER BY provider
$$;

CREATE FUNCTION bursar.get_billing_customer_by_provider(
 p_provider text,p_provider_customer_id text
)
RETURNS bursar.billing_customers
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_customers
 WHERE provider=p_provider
 AND provider_customer_id=p_provider_customer_id
$$;

CREATE FUNCTION bursar.get_billing_subscription_by_provider(
 p_provider text,p_provider_subscription_id text
)
RETURNS bursar.billing_subscriptions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_subscriptions
 WHERE provider=p_provider
 AND provider_subscription_id=p_provider_subscription_id
$$;

CREATE FUNCTION bursar.list_billing_subscriptions(p_subject_id uuid)
RETURNS SETOF bursar.billing_subscriptions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_subscriptions
 WHERE subject_id=p_subject_id
 ORDER BY created_at,id
$$;

CREATE FUNCTION bursar.get_billing_payment_by_provider(
 p_provider text,p_provider_payment_id text
)
RETURNS bursar.billing_payments
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_payments
 WHERE provider=p_provider
 AND provider_payment_id=p_provider_payment_id
$$;

CREATE FUNCTION bursar.get_billing_refund_by_provider(
 p_provider text,p_provider_refund_id text
)
RETURNS bursar.billing_refunds
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_refunds
 WHERE provider=p_provider
 AND provider_refund_id=p_provider_refund_id
$$;

CREATE FUNCTION bursar.get_billing_preferences(p_subject_id uuid)
RETURNS bursar.billing_preferences
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_preferences
 WHERE subject_id=p_subject_id
$$;

CREATE FUNCTION bursar.get_auto_recharge_profile(p_subject_id uuid)
RETURNS bursar.billing_auto_recharge_profiles
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_auto_recharge_profiles
 WHERE subject_id=p_subject_id
$$;

CREATE FUNCTION bursar.get_auto_recharge_attempt(p_attempt_id uuid)
RETURNS bursar.billing_auto_recharge_attempts
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT *
 FROM bursar.billing_auto_recharge_attempts
 WHERE id=p_attempt_id
$$;

CREATE FUNCTION bursar.resolve_catalog_offer(
 p_provider text,p_lookup_type text,p_lookup_value text
)
RETURNS bursar.catalog_offers
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT o.*
 FROM bursar.catalog_provider_refs r
 JOIN bursar.catalog_revisions cr
 ON cr.id=r.catalog_revision_id AND cr.status='active'
 JOIN bursar.catalog_offers o
 ON o.catalog_revision_id=r.catalog_revision_id
 AND o.offer_key=r.object_key
 WHERE r.provider=p_provider
 AND r.lookup_type=p_lookup_type
 AND r.lookup_value=p_lookup_value
 AND r.object_type='offer'
$$;

CREATE FUNCTION bursar.resolve_catalog_topup(
 p_provider text,p_lookup_type text,p_lookup_value text
)
RETURNS bursar.catalog_topups
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
 SELECT t.*
 FROM bursar.catalog_provider_refs r
 JOIN bursar.catalog_revisions cr
 ON cr.id=r.catalog_revision_id AND cr.status='active'
 JOIN bursar.catalog_topups t
 ON t.catalog_revision_id=r.catalog_revision_id
 AND t.topup_key=r.object_key
 WHERE r.provider=p_provider
 AND r.lookup_type=p_lookup_type
 AND r.lookup_value=p_lookup_value
 AND r.object_type='topup'
$$;
