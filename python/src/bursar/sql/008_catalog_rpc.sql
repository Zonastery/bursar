-- Catalog publication and resolution RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

-- The public catalog document is the validated Bursar YAML/JSON document.  The
-- relational rows below are an immutable projection of that document, never a
-- second, caller-defined catalogue format.
CREATE FUNCTION bursar.publish_and_activate_catalog(
    p_yaml_schema_version integer,
    p_source_document jsonb,
    p_label text DEFAULT NULL
)
RETURNS TABLE(
    revision_id uuid,
    revision_no bigint,
    status bursar.catalog_revision_status
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
 v_revision uuid;

 v_no bigint;

 v_digest bytea;

 v_bucket_count integer;

 v_order_count integer;

 v_distinct_order_count integer;

BEGIN
    IF p_yaml_schema_version <> 1
       OR jsonb_typeof(p_source_document) <> 'object'
       OR COALESCE((p_source_document->>'version')::integer, 1) <> p_yaml_schema_version THEN
        RAISE EXCEPTION 'invalid_catalog' USING ERRCODE = '22023';

    END IF;

    IF EXISTS (
        SELECT 1 FROM jsonb_object_keys(p_source_document) AS k(key)
        WHERE key NOT IN ('version', 'usage', 'credits', 'plans', 'payments')
    ) THEN
        RAISE EXCEPTION 'unknown_catalog_field' USING ERRCODE = '22023';

    END IF;

 IF jsonb_typeof(p_source_document->'credits')='object' THEN
  IF jsonb_typeof(p_source_document #> '{credits,buckets}') <> 'object'
     OR jsonb_typeof(p_source_document #> '{credits,spend_order}') <> 'array'
  THEN
   RAISE EXCEPTION 'invalid_credits' USING ERRCODE='22023';

  END IF;

  SELECT count(*) INTO v_bucket_count
  FROM jsonb_object_keys(p_source_document #> '{credits,buckets}');

  SELECT count(*),count(DISTINCT value) INTO v_order_count,v_distinct_order_count
  FROM jsonb_array_elements_text(p_source_document #> '{credits,spend_order}');

  IF v_bucket_count<>v_order_count OR v_order_count<>v_distinct_order_count
     OR EXISTS (
       SELECT 1
       FROM jsonb_array_elements_text(p_source_document #> '{credits,spend_order}') AS o(value)
       WHERE NOT (p_source_document #> '{credits,buckets}') ? o.value
     )
  THEN
   RAISE EXCEPTION 'invalid_spend_order' USING ERRCODE='22023';

  END IF;

 END IF;

 PERFORM pg_advisory_xact_lock(hashtextextended('bursar.catalog.active', 0));

    v_digest := extensions.digest(convert_to(p_source_document::text, 'UTF8'), 'sha256');

    INSERT INTO bursar.catalog_revisions(
        yaml_schema_version, source_document, digest, status, label, published_at
    ) VALUES (p_yaml_schema_version, p_source_document, v_digest, 'published', p_label, now())
    RETURNING id, catalog_revisions.revision_no INTO v_revision, v_no;

    INSERT INTO bursar.catalog_buckets(
        catalog_revision_id, bucket_key, label, priority, expires,
        expires_after_unit, expires_after_count, expires_after_anchor,
        expires_after_timezone, allow_overdraft, is_default
    )
    SELECT
        v_revision,
        b.key,
        initcap(replace(b.key, '_', ' ')),
        COALESCE((
            SELECT ordinality::integer - 1
            FROM jsonb_array_elements_text(COALESCE(p_source_document #> '{credits,spend_order}', '[]'::jsonb))
                 WITH ORDINALITY AS s(bucket_key, ordinality)
            WHERE s.bucket_key = b.key
        ), 2147483647),
        b.value ? 'expires_after',
        b.value #>> '{expires_after,unit}',
        NULLIF(b.value #>> '{expires_after,count}', '')::integer,
        b.value #>> '{expires_after,anchor}',
        b.value #>> '{expires_after,timezone}',
        b.key = COALESCE(p_source_document #>> '{credits,overdraft_bucket}', ''),
        b.key = COALESCE(p_source_document #>> '{credits,default_bucket}', '')
    FROM jsonb_each(COALESCE(p_source_document #> '{credits,buckets}', '{}'::jsonb)) AS b(key, value);

    IF EXISTS (SELECT 1 FROM bursar.catalog_buckets WHERE catalog_revision_id = v_revision)
       AND NOT EXISTS (SELECT 1 FROM bursar.catalog_buckets WHERE catalog_revision_id = v_revision AND is_default) THEN
        RAISE EXCEPTION 'credits.default_bucket must select a configured bucket' USING ERRCODE = '22023';

    END IF;

    INSERT INTO bursar.catalog_plans(
        catalog_revision_id, plan_key, display_name, rate_card, features, limits,
        spending, included_credits, included_credits_bucket,
        included_credits_reset_unit, included_credits_reset_count,
        included_credits_reset_anchor, included_credits_reset_timezone
    )
    SELECT
        v_revision, p.key, p.value->>'display_name', p.value->>'rate_card',
        COALESCE(p.value->'features', '{}'::jsonb),
        COALESCE(p.value->'limits', '{}'::jsonb),
        COALESCE(p.value->'spending', '{}'::jsonb),
        NULLIF(p.value #>> '{included_credits,amount}', '')::numeric,
        CASE WHEN p.value ? 'included_credits' THEN p_source_document #>> '{credits,default_bucket}' END,
        CASE WHEN p.value ? 'included_credits' THEN p.value #>> '{included_credits,reset,unit}' END,
        CASE WHEN p.value ? 'included_credits' THEN (p.value #>> '{included_credits,reset,count}')::integer END,
        CASE WHEN p.value ? 'included_credits' THEN p.value #>> '{included_credits,reset,anchor}' END,
        CASE WHEN p.value ? 'included_credits' THEN p.value #>> '{included_credits,reset,timezone}' END
    FROM jsonb_each(COALESCE(p_source_document->'plans', '{}'::jsonb)) AS p(key, value);

    INSERT INTO bursar.catalog_signup_grants(catalog_revision_id, amount, bucket_key)
    SELECT v_revision,
           (p_source_document #>> '{credits,signup_grant,amount}')::numeric,
           p_source_document #>> '{credits,signup_grant,bucket}'
    WHERE p_source_document #> '{credits,signup_grant}' IS NOT NULL;

    INSERT INTO bursar.catalog_offers(
        catalog_revision_id, offer_key, plan_key, billing_unit, billing_count,
        billing_anchor, billing_timezone, grant_mode, grant_credits,
        grant_bucket_key, renewal_replacement, subscription_end_behavior,
        credit_stacking
    )
    SELECT
        v_revision, o.key, o.value->>'plan',
        o.value #>> '{billing_period,unit}',
        COALESCE((o.value #>> '{billing_period,count}')::integer, 1),
        COALESCE(o.value #>> '{billing_period,anchor}', 'calendar'),
        COALESCE(o.value #>> '{billing_period,timezone}', 'UTC'),
        CASE WHEN o.value ? 'renewal_credits' THEN 'credits' ELSE 'allowance' END,
        COALESCE((o.value #>> '{renewal_credits,amount}')::numeric, 0),
        o.value #>> '{renewal_credits,bucket}',
        COALESCE(o.value #>> '{renewal_credits,behavior}', 'replace'),
        COALESCE(o.value #>> '{renewal_credits,on_subscription_end}', 'expire'),
        CASE WHEN COALESCE((o.value->>'stack_credits')::boolean, false) THEN 'stack' ELSE 'replace' END
    FROM jsonb_each(COALESCE(p_source_document #> '{payments,subscriptions}', '{}'::jsonb)) AS o(key, value);

    INSERT INTO bursar.catalog_topups(
        catalog_revision_id, topup_key, credits_per_unit, bucket_key, min_quantity, max_quantity
    )
    SELECT v_revision, t.key, (t.value->>'credits')::numeric, t.value->>'bucket', 1, NULL
    FROM jsonb_each(COALESCE(p_source_document #> '{payments,topups}', '{}'::jsonb)) AS t(key, value);

    INSERT INTO bursar.catalog_auto_recharge_policies(
        catalog_revision_id, topup_key, quantity, balance_below, max_purchases,
        period_unit, period_count, period_anchor, period_timezone
    )
    SELECT
        v_revision,
        p_source_document #>> '{payments,auto_recharge,purchase,topup}',
        COALESCE((p_source_document #>> '{payments,auto_recharge,purchase,quantity}')::integer, 1),
        (p_source_document #>> '{payments,auto_recharge,trigger,balance_below}')::numeric,
        (p_source_document #>> '{payments,auto_recharge,limit,max_purchases}')::integer,
        p_source_document #>> '{payments,auto_recharge,limit,period,unit}',
        COALESCE((p_source_document #>> '{payments,auto_recharge,limit,period,count}')::integer, 1),
        COALESCE(p_source_document #>> '{payments,auto_recharge,limit,period,anchor}', 'calendar'),
        COALESCE(p_source_document #>> '{payments,auto_recharge,limit,period,timezone}', 'UTC')
    WHERE p_source_document #> '{payments,auto_recharge}' IS NOT NULL;

    INSERT INTO bursar.catalog_provider_refs(
        catalog_revision_id, provider, lookup_type, lookup_value, object_type, object_key
    )
    SELECT v_revision, pr.provider, pr.ref #>> '{lookup,type}', pr.ref #>> '{lookup,value}', 'offer', o.key
    FROM jsonb_each(COALESCE(p_source_document #> '{payments,subscriptions}', '{}'::jsonb)) AS o(key, value)
    CROSS JOIN LATERAL jsonb_each(COALESCE(o.value->'providers', '{}'::jsonb)) AS pr(provider, ref)
    UNION ALL
    SELECT v_revision, pr.provider, pr.ref #>> '{lookup,type}', pr.ref #>> '{lookup,value}', 'topup', t.key
    FROM jsonb_each(COALESCE(p_source_document #> '{payments,topups}', '{}'::jsonb)) AS t(key, value)
    CROSS JOIN LATERAL jsonb_each(COALESCE(t.value->'providers', '{}'::jsonb)) AS pr(provider, ref);

    UPDATE bursar.catalog_revisions
    SET status = 'active', activated_at = now()
    WHERE id = v_revision;

    RETURN QUERY SELECT v_revision, v_no, 'active'::bursar.catalog_revision_status;

END $$;

CREATE FUNCTION bursar.active_catalog_revision()
RETURNS bursar.catalog_revisions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT * FROM bursar.catalog_revisions WHERE status = 'active'
$$;

CREATE FUNCTION bursar.bucket_expiry_at(
    p_subject_id uuid,
    p_revision_id uuid,
    p_bucket_key text
)
RETURNS timestamptz
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    b bursar.catalog_buckets;

    v_base timestamptz;

    v_expiry timestamptz;

    v_step interval;

    v_iterations integer := 0;

BEGIN
    SELECT * INTO b FROM bursar.catalog_buckets
    WHERE catalog_revision_id = p_revision_id AND bucket_key = p_bucket_key;

    IF NOT FOUND THEN RAISE EXCEPTION 'catalog bucket not found' USING ERRCODE = '23503';
 END IF;

    IF NOT b.expires THEN RETURN NULL;
 END IF;

    v_step := CASE b.expires_after_unit
        WHEN 'day' THEN make_interval(days => b.expires_after_count)
        WHEN 'week' THEN make_interval(weeks => b.expires_after_count)
        WHEN 'month' THEN make_interval(months => b.expires_after_count)
        WHEN 'year' THEN make_interval(years => b.expires_after_count)
    END;

    IF b.expires_after_anchor = 'rolling' THEN
        RETURN (now() AT TIME ZONE b.expires_after_timezone + v_step) AT TIME ZONE b.expires_after_timezone;

    ELSIF b.expires_after_anchor = 'calendar' THEN
        v_base := date_trunc(b.expires_after_unit, now() AT TIME ZONE b.expires_after_timezone)
                  AT TIME ZONE b.expires_after_timezone;

    ELSIF b.expires_after_anchor = 'plan_assignment' THEN
        SELECT a.starts_at INTO v_base
        FROM bursar.account_plan_assignments a
        JOIN bursar.credit_accounts ca ON ca.id = a.account_id
        WHERE ca.subject_id = p_subject_id AND ca.account_kind = 'personal';

        IF NOT FOUND THEN RAISE EXCEPTION 'plan assignment required for bucket expiry' USING ERRCODE = '22023';
 END IF;

    ELSE
        RAISE EXCEPTION 'unsupported bucket expiry anchor' USING ERRCODE = '22023';

    END IF;

    v_expiry := (v_base AT TIME ZONE b.expires_after_timezone + v_step) AT TIME ZONE b.expires_after_timezone;

    WHILE v_expiry <= now() LOOP
        v_expiry := (v_expiry AT TIME ZONE b.expires_after_timezone + v_step) AT TIME ZONE b.expires_after_timezone;

        v_iterations := v_iterations + 1;

        IF v_iterations > 10000 THEN RAISE EXCEPTION 'bucket expiry iteration limit exceeded' USING ERRCODE = '22023';
 END IF;

    END LOOP;

    RETURN v_expiry;

END $$;
