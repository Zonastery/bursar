-- Immutable catalog publication, projection, activation, and expiry helpers.

CREATE FUNCTION bursar.publish_and_activate_catalog(
    p_yaml_schema_version integer,
    p_source_document jsonb,
    p_label text DEFAULT NULL,
    p_activate boolean DEFAULT TRUE
)
RETURNS TABLE (
    revision_id uuid,
    revision_no bigint,
    status bursar.catalog_revision_status
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_revision uuid;
    v_no bigint;
    v_digest bytea;
    v_default_bucket text;
    v_provider_environment text := bursar.current_provider_environment();
    v_status bursar.catalog_revision_status;
BEGIN
    IF p_activate IS NULL
       OR p_yaml_schema_version <> 1
       OR jsonb_typeof(p_source_document) <> 'object'
       OR p_source_document->>'version' IS DISTINCT FROM '1'
    THEN
        RAISE EXCEPTION 'invalid_catalog' USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_object_keys(p_source_document) AS key_name(key)
        WHERE key NOT IN (
            'version',
            'catalog',
            'pricing',
            'credits',
            'entitlements',
            'admission',
            'plans',
            'commerce'
        )
    )
    THEN
        RAISE EXCEPTION 'unknown_catalog_field' USING ERRCODE = '22023';
    END IF;

    PERFORM bursar.require_json_object(
        p_source_document->'credits',
        'credits'
    );
    PERFORM bursar.require_json_object(
        p_source_document #> '{credits,accounting}',
        'credits.accounting'
    );

    IF p_source_document #>> '{credits,accounting,unit}' <> 'credit'
       OR p_source_document #>> '{credits,accounting,scale}' <> '6'
       OR p_source_document #>> '{credits,accounting,rounding}' <> 'half_up'
    THEN
        RAISE EXCEPTION 'unsupported_credit_accounting'
            USING ERRCODE = '22023';
    END IF;

    IF p_source_document #> '{credits,buckets}' IS NOT NULL THEN
        PERFORM bursar.require_json_object(
            p_source_document #> '{credits,buckets}',
            'credits.buckets'
        );
    END IF;

    v_default_bucket := p_source_document #>> '{credits,default_bucket}';

    IF v_default_bucket IS NOT NULL
       AND NOT COALESCE(
           p_source_document #> '{credits,buckets}',
           '{}'::jsonb
       ) ? v_default_bucket
    THEN
        RAISE EXCEPTION 'credits.default_bucket must select configured bucket'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_each(
            COALESCE(
                p_source_document #> '{credits,buckets}',
                '{}'::jsonb
            )
        ) AS bucket_entry(key, value)
        GROUP BY (value->>'priority')::integer
        HAVING count(*) > 1
    )
    THEN
        RAISE EXCEPTION 'credit bucket priorities must be unique'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_each(
            COALESCE(p_source_document->'plans', '{}'::jsonb)
        ) AS plan_entry(key, value)
        WHERE value ? 'credit_allowance'
          AND v_default_bucket IS NULL
    )
    THEN
        RAISE EXCEPTION 'credit allowance requires credits.default_bucket'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_each(
            COALESCE(p_source_document->'plans', '{}'::jsonb)
        ) AS plan_entry(key, value)
        WHERE CASE
            WHEN jsonb_typeof(plan_entry.value->'rank') = 'number'
            THEN (plan_entry.value->>'rank')::numeric < 0
              OR mod((plan_entry.value->>'rank')::numeric, 1) <> 0
            ELSE true
        END
    )
    THEN
        RAISE EXCEPTION 'plans.*.rank must be a non-negative integer'
            USING ERRCODE = '22023';
    END IF;

    IF p_source_document #> '{commerce,subscription_changes}' IS NOT NULL THEN
        PERFORM bursar.require_json_object(
            p_source_document #> '{commerce,subscription_changes}',
            'commerce.subscription_changes'
        );

        IF EXISTS (
            SELECT 1
            FROM jsonb_each(
                p_source_document #> '{commerce,subscription_changes}'
            ) AS change_policy(key, value)
            WHERE change_policy.key NOT IN (
                'upgrade',
                'downgrade',
                'lateral',
                'cadence_change'
            )
               OR jsonb_typeof(change_policy.value) <> 'object'
               OR change_policy.value->>'effective' IS NULL
               OR change_policy.value->>'effective' NOT IN (
                   'immediate',
                   'renewal'
               )
               OR change_policy.value->>'proration' IS NULL
               OR change_policy.value->>'proration' NOT IN (
                   'prorated',
                   'none'
               )
               OR COALESCE(
                   change_policy.value->>'payment_failure',
                   'prevent_change'
               ) NOT IN (
                   'prevent_change',
                   'apply_change'
               )
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_object_keys(
                       CASE
                           WHEN jsonb_typeof(change_policy.value) = 'object'
                           THEN change_policy.value
                           ELSE '{}'::jsonb
                       END
                   )
                       AS policy_field(field_name)
                   WHERE policy_field.field_name NOT IN (
                       'effective',
                       'proration',
                       'payment_failure'
                   )
               )
        )
        THEN
            RAISE EXCEPTION 'invalid commerce subscription change policy'
                USING ERRCODE = '22023';
        END IF;
    END IF;

    IF p_source_document #> '{commerce,auto_recharge}' IS NOT NULL
       AND EXISTS (
           SELECT 1
           FROM jsonb_array_elements_text(
               COALESCE(
                   p_source_document
                       #> '{commerce,auto_recharge,eligible_topups}',
                   '[]'::jsonb
               )
           ) AS eligible(topup_key)
           WHERE NOT COALESCE(
               p_source_document #> '{commerce,offers}',
               '{}'::jsonb
           ) ? eligible.topup_key
              OR p_source_document
                   #>> ARRAY['commerce', 'offers', eligible.topup_key, 'type']
                 <> 'topup'
       )
    THEN
        RAISE EXCEPTION 'auto recharge references unknown topup'
            USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended('bursar.catalog.active', 0)
    );

    v_digest := extensions.digest(
        convert_to(p_source_document::text, 'UTF8'),
        'sha256'
    );

    SELECT
        id,
        catalog_revisions.revision_no,
        catalog_revisions.status
    INTO v_revision, v_no, v_status
    FROM bursar.catalog_revisions
    WHERE yaml_schema_version = p_yaml_schema_version
      AND digest = v_digest
    FOR UPDATE;

    IF FOUND THEN
        IF p_activate AND v_status <> 'active'
        THEN
            UPDATE bursar.catalog_activation_history
            SET deactivated_at = now()
            WHERE deactivated_at IS NULL;

            UPDATE bursar.catalog_revisions
            SET status = 'active'
            WHERE id = v_revision;

            INSERT INTO bursar.catalog_activation_history(
                catalog_revision_id,
                label
            )
            VALUES (v_revision, p_label);

            PERFORM bursar.schedule_catalog_plan_rollout(v_revision);
            v_status := 'active';
        END IF;

        RETURN QUERY
        SELECT
            v_revision,
            v_no,
            v_status;
        RETURN;
    END IF;

    INSERT INTO bursar.catalog_revisions(
        yaml_schema_version,
        source_document,
        digest,
        status,
        label,
        published_at
    )
    VALUES (
        p_yaml_schema_version,
        p_source_document,
        v_digest,
        'published',
        p_label,
        now()
    )
    RETURNING id, catalog_revisions.revision_no
    INTO v_revision, v_no;

    INSERT INTO bursar.catalog_buckets(
        catalog_revision_id,
        bucket_key,
        label,
        priority,
        definition,
        expiry_policy,
        expiry_type,
        expires_after_unit,
        expires_after_count,
        expires_after_anchor,
        expires_after_timezone,
        fixed_expires_at,
        is_default
    )
    SELECT
        v_revision,
        bucket_entry.key,
        initcap(replace(bucket_entry.key, '_', ' ')),
        (bucket_entry.value->>'priority')::integer,
        bucket_entry.value,
        COALESCE(
            bucket_entry.value->'expiry',
            '{"type":"never"}'::jsonb
        ),
        COALESCE(bucket_entry.value #>> '{expiry,type}', 'never'),
        CASE bucket_entry.value #>> '{expiry,type}'
            WHEN 'after_grant'
                THEN bucket_entry.value #>> '{expiry,interval,unit}'
            WHEN 'end_of_window'
                THEN CASE bucket_entry.value #>> '{expiry,window,type}'
                    WHEN 'calendar'
                        THEN bucket_entry.value #>> '{expiry,window,unit}'
                    WHEN 'plan_assignment'
                        THEN bucket_entry.value
                            #>> '{expiry,window,interval,unit}'
                END
        END,
        CASE bucket_entry.value #>> '{expiry,type}'
            WHEN 'after_grant'
                THEN (
                    bucket_entry.value #>> '{expiry,interval,count}'
                )::integer
            WHEN 'end_of_window'
                THEN CASE bucket_entry.value #>> '{expiry,window,type}'
                    WHEN 'calendar'
                        THEN COALESCE(
                            (
                                bucket_entry.value
                                    #>> '{expiry,window,count}'
                            )::integer,
                            1
                        )
                    WHEN 'plan_assignment'
                        THEN COALESCE(
                            (
                                bucket_entry.value
                                    #>> '{expiry,window,interval,count}'
                            )::integer,
                            1
                        )
                END
        END,
        CASE bucket_entry.value #>> '{expiry,type}'
            WHEN 'after_grant' THEN 'rolling'
            WHEN 'end_of_window'
                THEN bucket_entry.value #>> '{expiry,window,type}'
        END,
        CASE bucket_entry.value #>> '{expiry,type}'
            WHEN 'after_grant'
                THEN COALESCE(
                    bucket_entry.value #>> '{expiry,timezone}',
                    'UTC'
                )
            WHEN 'end_of_window'
                THEN COALESCE(
                    bucket_entry.value #>> '{expiry,window,timezone}',
                    'UTC'
                )
        END,
        CASE bucket_entry.value #>> '{expiry,type}'
            WHEN 'fixed_at'
                THEN (
                    bucket_entry.value #>> '{expiry,at}'
                )::timestamptz
        END,
        bucket_entry.key = v_default_bucket
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{credits,buckets}',
            '{}'::jsonb
        )
    ) AS bucket_entry(key, value);

    INSERT INTO bursar.catalog_operations(
        catalog_revision_id,
        operation_key,
        measures,
        dimensions,
        definition
    )
    SELECT
        v_revision,
        operation_entry.key,
        operation_entry.value->'measures',
        COALESCE(operation_entry.value->'dimensions', '{}'::jsonb),
        operation_entry.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{pricing,operations}',
            '{}'::jsonb
        )
    ) AS operation_entry(key, value);

    INSERT INTO bursar.catalog_rate_cards(
        catalog_revision_id,
        rate_card_key,
        extends_key,
        definition
    )
    SELECT
        v_revision,
        rate_card_entry.key,
        rate_card_entry.value->>'extends',
        rate_card_entry.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{pricing,rate_cards}',
            '{}'::jsonb
        )
    ) AS rate_card_entry(key, value);

    SET CONSTRAINTS ALL IMMEDIATE;

    INSERT INTO bursar.catalog_credit_policies(
        catalog_revision_id,
        policy_key,
        policy_type,
        credit_limit,
        definition
    )
    SELECT
        v_revision,
        policy_entry.key,
        policy_entry.value->>'type',
        CASE policy_entry.value->>'type'
            WHEN 'credit_line'
                THEN (policy_entry.value->>'limit')::numeric
        END,
        policy_entry.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{credits,policies}',
            '{}'::jsonb
        )
    ) AS policy_entry(key, value);

    INSERT INTO bursar.catalog_admission_policies(
        catalog_revision_id,
        policy_key,
        max_in_flight,
        definition
    )
    SELECT
        v_revision,
        policy_entry.key,
        (policy_entry.value->>'max_in_flight')::integer,
        policy_entry.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{admission,policies}',
            '{}'::jsonb
        )
    ) AS policy_entry(key, value);

    INSERT INTO bursar.catalog_admission_operation_policies(
        catalog_revision_id,
        admission_policy_key,
        operation_key,
        max_in_flight
    )
    SELECT
        v_revision,
        admission_entry.key,
        operation_entry.key,
        (operation_entry.value->>'max_in_flight')::integer
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{admission,policies}',
            '{}'::jsonb
        )
    ) AS admission_entry(key, value)
    CROSS JOIN LATERAL jsonb_each(
        COALESCE(admission_entry.value->'operations', '{}'::jsonb)
    ) AS operation_entry(key, value);

    INSERT INTO bursar.catalog_entitlement_features(
        catalog_revision_id,
        feature_key,
        value_type,
        default_value,
        definition
    )
    SELECT
        v_revision,
        feature_entry.key,
        feature_entry.value->>'type',
        feature_entry.value->'default',
        feature_entry.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{entitlements,features}',
            '{}'::jsonb
        )
    ) AS feature_entry(key, value);

    INSERT INTO bursar.catalog_plans(
        catalog_revision_id,
        plan_key,
        display_name,
        description,
        rate_card,
        allowed_operations,
        credit_policy_key,
        admission_policy_key,
        revision_policy,
        credit_allowance_amount,
        credit_allowance_bucket,
        credit_allowance_reset_unit,
        credit_allowance_reset_count,
        credit_allowance_reset_anchor,
        credit_allowance_reset_timezone,
        definition
    )
    SELECT
        v_revision,
        plan_entry.key,
        plan_entry.value->>'display_name',
        plan_entry.value->>'description',
        plan_entry.value->>'rate_card',
        ARRAY(
            SELECT jsonb_array_elements_text(
                COALESCE(
                    plan_entry.value->'allowed_operations',
                    '[]'::jsonb
                )
            )
        ),
        plan_entry.value->>'credit_policy',
        plan_entry.value->>'admission_policy',
        COALESCE(
            plan_entry.value->>'revision_policy',
            CASE WHEN EXISTS (
                SELECT 1
                FROM jsonb_each(
                    COALESCE(
                        p_source_document #> '{commerce,offers}',
                        '{}'::jsonb
                    )
                ) AS subscription_offer(key, value)
                WHERE subscription_offer.value->>'type' = 'subscription'
                  AND subscription_offer.value->>'plan' = plan_entry.key
            )
            THEN 'next_renewal'
            ELSE 'immediate'
            END
        ),
        CASE WHEN plan_entry.value ? 'credit_allowance'
            THEN (
                plan_entry.value #>> '{credit_allowance,amount}'
            )::numeric
        END,
        CASE WHEN plan_entry.value ? 'credit_allowance'
            THEN v_default_bucket
        END,
        CASE plan_entry.value #>> '{credit_allowance,window,type}'
            WHEN 'calendar'
                THEN plan_entry.value
                    #>> '{credit_allowance,window,unit}'
            WHEN 'plan_assignment'
                THEN plan_entry.value
                    #>> '{credit_allowance,window,interval,unit}'
            WHEN 'rolling'
                THEN plan_entry.value
                    #>> '{credit_allowance,window,duration,unit}'
        END,
        CASE plan_entry.value #>> '{credit_allowance,window,type}'
            WHEN 'calendar'
                THEN COALESCE(
                    (
                        plan_entry.value
                            #>> '{credit_allowance,window,count}'
                    )::integer,
                    1
                )
            WHEN 'plan_assignment'
                THEN COALESCE(
                    (
                        plan_entry.value
                            #>> '{credit_allowance,window,interval,count}'
                    )::integer,
                    1
                )
            WHEN 'rolling'
                THEN (
                    plan_entry.value
                        #>> '{credit_allowance,window,duration,count}'
                )::integer
        END,
        plan_entry.value #>> '{credit_allowance,window,type}',
        CASE plan_entry.value #>> '{credit_allowance,window,type}'
            WHEN 'calendar'
                THEN COALESCE(
                    plan_entry.value
                        #>> '{credit_allowance,window,timezone}',
                    'UTC'
                )
            WHEN 'plan_assignment'
                THEN COALESCE(
                    plan_entry.value
                        #>> '{credit_allowance,window,timezone}',
                    'UTC'
                )
            WHEN 'rolling' THEN 'UTC'
        END,
        plan_entry.value
    FROM jsonb_each(
        COALESCE(p_source_document->'plans', '{}'::jsonb)
    ) AS plan_entry(key, value);

    INSERT INTO bursar.catalog_plan_features(
        catalog_revision_id,
        plan_key,
        feature_key,
        feature_value
    )
    SELECT
        v_revision,
        plan_entry.key,
        feature_entry.key,
        feature_entry.value
    FROM jsonb_each(
        COALESCE(p_source_document->'plans', '{}'::jsonb)
    ) AS plan_entry(key, value)
    CROSS JOIN LATERAL jsonb_each(
        COALESCE(plan_entry.value->'features', '{}'::jsonb)
    ) AS feature_entry(key, value);

    INSERT INTO bursar.catalog_plan_quotas(
        catalog_revision_id,
        plan_key,
        quota_key,
        operation_key,
        measure_key,
        quota_limit,
        window_policy,
        enforcement,
        emit_at_percent,
        definition
    )
    SELECT
        v_revision,
        plan_entry.key,
        quota_entry.key,
        quota_entry.value->>'operation',
        quota_entry.value->>'measure',
        (quota_entry.value->>'limit')::numeric,
        quota_entry.value->'window',
        quota_entry.value->>'enforcement',
        ARRAY(
            SELECT value::integer
            FROM jsonb_array_elements_text(
                COALESCE(
                    quota_entry.value->'emit_at_percent',
                    '[]'::jsonb
                )
            )
        ),
        quota_entry.value
    FROM jsonb_each(
        COALESCE(p_source_document->'plans', '{}'::jsonb)
    ) AS plan_entry(key, value)
    CROSS JOIN LATERAL jsonb_each(
        COALESCE(plan_entry.value->'quotas', '{}'::jsonb)
    ) AS quota_entry(key, value);

    INSERT INTO bursar.catalog_grant_programs(
        catalog_revision_id,
        program_key,
        trigger_type,
        availability,
        eligibility,
        max_awards_per_subject,
        idempotency_scope,
        definition
    )
    SELECT
        v_revision,
        program_entry.key,
        program_entry.value->>'trigger',
        program_entry.value->'availability',
        COALESCE(program_entry.value->'eligibility', '{}'::jsonb),
        COALESCE(
            (program_entry.value->>'max_awards_per_subject')::integer,
            1
        ),
        COALESCE(
            program_entry.value->>'idempotency_scope',
            'subject'
        ),
        program_entry.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{credits,grant_programs}',
            '{}'::jsonb
        )
    ) AS program_entry(key, value);

    INSERT INTO bursar.catalog_grant_awards(
        catalog_revision_id,
        grant_program_id,
        award_index,
        recipient,
        amount,
        bucket_key,
        expiry_policy,
        definition
    )
    SELECT
        v_revision,
        program.id,
        award.ordinality::integer - 1,
        COALESCE(award.value->>'recipient', 'subject'),
        (award.value->>'amount')::numeric,
        award.value->>'bucket',
        award.value->'expiry',
        award.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{credits,grant_programs}',
            '{}'::jsonb
        )
    ) AS program_entry(key, value)
    JOIN bursar.catalog_grant_programs AS program
      ON program.catalog_revision_id = v_revision
     AND program.program_key = program_entry.key
    CROSS JOIN LATERAL jsonb_array_elements(
        program_entry.value->'awards'
    ) WITH ORDINALITY AS award(value, ordinality);

    INSERT INTO bursar.catalog_offers(
        catalog_revision_id,
        offer_key,
        display_name,
        description,
        sort_order,
        availability,
        amount_minor,
        currency,
        tax_behavior,
        plan_key,
        billing_unit,
        billing_count,
        trial_policy,
        cycle_grant_amount,
        cycle_grant_bucket_key,
        cycle_grant_renewal,
        cycle_grant_expiry_policy,
        definition
    )
    SELECT
        v_revision,
        offer_entry.key,
        offer_entry.value->>'display_name',
        offer_entry.value->>'description',
        COALESCE((offer_entry.value->>'sort_order')::integer, 0),
        offer_entry.value->'availability',
        (offer_entry.value #>> '{price,amount_minor}')::bigint,
        upper(offer_entry.value #>> '{price,currency}'),
        COALESCE(
            offer_entry.value #>> '{price,tax_behavior}',
            'unspecified'
        ),
        offer_entry.value->>'plan',
        offer_entry.value #>> '{billing_interval,unit}',
        COALESCE(
            (
                offer_entry.value
                    #>> '{billing_interval,count}'
            )::integer,
            1
        ),
        offer_entry.value->'trial',
        (
            offer_entry.value #>> '{cycle_grant,amount}'
        )::numeric,
        offer_entry.value #>> '{cycle_grant,bucket}',
        offer_entry.value #>> '{cycle_grant,renewal}',
        offer_entry.value #> '{cycle_grant,expiry}',
        offer_entry.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{commerce,offers}',
            '{}'::jsonb
        )
    ) AS offer_entry(key, value)
    WHERE offer_entry.value->>'type' = 'subscription';

    INSERT INTO bursar.catalog_topups(
        catalog_revision_id,
        topup_key,
        display_name,
        description,
        sort_order,
        availability,
        amount_minor,
        currency,
        tax_behavior,
        credits_per_unit,
        bucket_key,
        min_quantity,
        max_quantity,
        default_quantity,
        expiry_policy,
        lot_behavior,
        definition
    )
    SELECT
        v_revision,
        offer_entry.key,
        offer_entry.value->>'display_name',
        offer_entry.value->>'description',
        COALESCE((offer_entry.value->>'sort_order')::integer, 0),
        offer_entry.value->'availability',
        (offer_entry.value #>> '{price,amount_minor}')::bigint,
        upper(offer_entry.value #>> '{price,currency}'),
        COALESCE(
            offer_entry.value #>> '{price,tax_behavior}',
            'unspecified'
        ),
        (offer_entry.value->>'credits_per_unit')::numeric,
        offer_entry.value->>'bucket',
        COALESCE(
            (offer_entry.value #>> '{quantity,minimum}')::integer,
            1
        ),
        COALESCE(
            (offer_entry.value #>> '{quantity,maximum}')::integer,
            1
        ),
        COALESCE(
            (offer_entry.value #>> '{quantity,default}')::integer,
            1
        ),
        offer_entry.value->'expiry',
        COALESCE(
            offer_entry.value->>'lot_behavior',
            'separate_lots'
        ),
        offer_entry.value
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{commerce,offers}',
            '{}'::jsonb
        )
    ) AS offer_entry(key, value)
    WHERE offer_entry.value->>'type' = 'topup';

    INSERT INTO bursar.catalog_provider_refs(
        catalog_revision_id,
        provider,
        provider_environment,
        lookup_type,
        lookup_value,
        object_type,
        object_key
    )
    SELECT
        v_revision,
        provider_entry.key,
        v_provider_environment,
        CASE provider_entry.value->>'type'
            WHEN 'stripe_price' THEN 'price_id'
            WHEN 'dodo_product' THEN 'product_id'
            WHEN 'custom_object'
                THEN provider_entry.value->>'object_kind'
        END,
        CASE provider_entry.value->>'type'
            WHEN 'stripe_price'
                THEN provider_entry.value->>'price_id'
            WHEN 'dodo_product'
                THEN provider_entry.value->>'product_id'
            WHEN 'custom_object'
                THEN provider_entry.value->>'external_id'
        END,
        CASE offer_entry.value->>'type'
            WHEN 'subscription' THEN 'offer'
            ELSE 'topup'
        END,
        offer_entry.key
    FROM jsonb_each(
        COALESCE(
            p_source_document #> '{commerce,offers}',
            '{}'::jsonb
        )
    ) AS offer_entry(key, value)
    CROSS JOIN LATERAL jsonb_each(
        COALESCE(offer_entry.value->'providers', '{}'::jsonb)
    ) AS provider_entry(key, value);

    IF p_source_document #> '{commerce,auto_recharge}' IS NOT NULL THEN
        INSERT INTO bursar.catalog_auto_recharge_policies(
            catalog_revision_id,
            eligible_topup_keys,
            default_topup_key,
            quantity_min,
            quantity_max,
            quantity,
            balance_min,
            balance_max,
            balance_below,
            rearm_above,
            max_purchases,
            max_charge_minor,
            cooldown_seconds,
            max_consecutive_failures,
            failure_action,
            period_unit,
            period_count,
            period_anchor,
            period_timezone,
            definition
        )
        SELECT
            v_revision,
            ARRAY(
                SELECT jsonb_array_elements_text(
                    p_source_document
                        #> '{commerce,auto_recharge,eligible_topups}'
                )
            ),
            p_source_document
                #>> '{commerce,auto_recharge,eligible_topups,0}',
            (
                p_source_document
                    #>> '{commerce,auto_recharge,quantity,minimum}'
            )::integer,
            (
                p_source_document
                    #>> '{commerce,auto_recharge,quantity,maximum}'
            )::integer,
            (
                p_source_document
                    #>> '{commerce,auto_recharge,quantity,default}'
            )::integer,
            (
                p_source_document
                    #>> '{commerce,auto_recharge,balance_below,minimum}'
            )::numeric,
            (
                p_source_document
                    #>> '{commerce,auto_recharge,balance_below,maximum}'
            )::numeric,
            (
                p_source_document
                    #>> '{commerce,auto_recharge,balance_below,default}'
            )::numeric,
            (
                p_source_document
                    #>> '{commerce,auto_recharge,rearm_above}'
            )::numeric,
            (
                p_source_document
                    #>> '{commerce,auto_recharge,limits,max_purchases}'
            )::integer,
            COALESCE(
                (
                    p_source_document
                        #>> '{commerce,auto_recharge,limits,max_charge_minor}'
                )::bigint,
                (
                    SELECT max(amount_minor * max_quantity)
                    FROM bursar.catalog_topups
                    WHERE catalog_revision_id = v_revision
                      AND topup_key = ANY(ARRAY(
                          SELECT jsonb_array_elements_text(
                              p_source_document
                                  #> '{commerce,auto_recharge,eligible_topups}'
                          )
                      ))
                )
            ),
            COALESCE(
                CASE p_source_document
                    #>> '{commerce,auto_recharge,limits,cooldown,unit}'
                    WHEN 'second' THEN (
                        p_source_document
                            #>> '{commerce,auto_recharge,limits,cooldown,count}'
                    )::integer
                    WHEN 'minute' THEN (
                        p_source_document
                            #>> '{commerce,auto_recharge,limits,cooldown,count}'
                    )::integer * 60
                    WHEN 'hour' THEN (
                        p_source_document
                            #>> '{commerce,auto_recharge,limits,cooldown,count}'
                    )::integer * 3600
                    WHEN 'day' THEN (
                        p_source_document
                            #>> '{commerce,auto_recharge,limits,cooldown,count}'
                    )::integer * 86400
                    WHEN 'week' THEN (
                        p_source_document
                            #>> '{commerce,auto_recharge,limits,cooldown,count}'
                    )::integer * 604800
                END,
                1
            ),
            COALESCE(
                (
                    p_source_document
                        #>> '{commerce,auto_recharge,limits,max_consecutive_failures}'
                )::integer,
                (
                    p_source_document
                        #>> '{commerce,auto_recharge,limits,max_failures}'
                )::integer,
                3
            ),
            COALESCE(
                p_source_document
                    #>> '{commerce,auto_recharge,limits,failure_action}',
                'pause'
            ),
            CASE p_source_document
                #>> '{commerce,auto_recharge,limits,window,type}'
                WHEN 'calendar'
                    THEN p_source_document
                        #>> '{commerce,auto_recharge,limits,window,unit}'
                WHEN 'rolling'
                    THEN p_source_document
                        #>> '{commerce,auto_recharge,limits,window,duration,unit}'
            END,
            CASE p_source_document
                #>> '{commerce,auto_recharge,limits,window,type}'
                WHEN 'calendar' THEN COALESCE(
                    (
                        p_source_document
                            #>> '{commerce,auto_recharge,limits,window,count}'
                    )::integer,
                    1
                )
                WHEN 'rolling' THEN (
                    p_source_document
                        #>> '{commerce,auto_recharge,limits,window,duration,count}'
                )::integer
            END,
            p_source_document
                #>> '{commerce,auto_recharge,limits,window,type}',
            CASE p_source_document
                #>> '{commerce,auto_recharge,limits,window,type}'
                WHEN 'calendar' THEN COALESCE(
                    p_source_document
                        #>> '{commerce,auto_recharge,limits,window,timezone}',
                    'UTC'
                )
                ELSE 'UTC'
            END,
            p_source_document #> '{commerce,auto_recharge}';
    END IF;

    IF p_activate THEN
        UPDATE bursar.catalog_activation_history
        SET deactivated_at = now()
        WHERE deactivated_at IS NULL;

        UPDATE bursar.catalog_revisions
        SET status = 'active'
        WHERE id = v_revision;

        INSERT INTO bursar.catalog_activation_history(
            catalog_revision_id,
            label
        )
        VALUES (v_revision, p_label);

        PERFORM bursar.schedule_catalog_plan_rollout(v_revision);
        v_status := 'active';
    ELSE
        v_status := 'published';
    END IF;

    RETURN QUERY
    SELECT v_revision, v_no, v_status;
EXCEPTION
    WHEN invalid_text_representation
       OR numeric_value_out_of_range
       OR not_null_violation
       OR check_violation
       OR foreign_key_violation
    THEN
        RAISE EXCEPTION 'invalid_catalog: %', SQLERRM
            USING ERRCODE = '22023';
END
$$;

CREATE FUNCTION bursar.active_catalog_revision()
RETURNS bursar.catalog_revisions
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT *
    FROM bursar.catalog_revisions
    WHERE status = 'active'
$$;

CREATE FUNCTION bursar.catalog_revision_by_number(
    p_revision_no bigint
)
RETURNS bursar.catalog_revisions
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT *
    FROM bursar.catalog_revisions
    WHERE revision_no = p_revision_no
$$;

CREATE FUNCTION bursar.list_catalog_revisions(
    p_limit integer DEFAULT 100
)
RETURNS SETOF bursar.catalog_revisions
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT *
    FROM bursar.catalog_revisions
    WHERE p_limit BETWEEN 1 AND 500
    ORDER BY revision_no DESC
    LIMIT p_limit
$$;

CREATE FUNCTION bursar.activate_catalog_revision(
    p_revision_no bigint
)
RETURNS bursar.catalog_revisions
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_revision bursar.catalog_revisions;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('bursar.catalog.active', 0)
    );

    SELECT *
    INTO v_revision
    FROM bursar.catalog_revisions
    WHERE revision_no = p_revision_no
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'catalog_revision_not_found'
            USING ERRCODE = '22023';
    END IF;

    IF v_revision.status = 'draft' THEN
        RAISE EXCEPTION 'catalog_revision_not_published'
            USING ERRCODE = '55000';
    END IF;

    IF v_revision.status <> 'active' THEN
        UPDATE bursar.catalog_activation_history
        SET deactivated_at = now()
        WHERE deactivated_at IS NULL;

        UPDATE bursar.catalog_revisions
        SET status = 'active'
        WHERE id = v_revision.id
        RETURNING * INTO v_revision;

        INSERT INTO bursar.catalog_activation_history(
            catalog_revision_id,
            label
        )
        VALUES (v_revision.id, v_revision.label);

        PERFORM bursar.schedule_catalog_plan_rollout(v_revision.id);
    END IF;

    RETURN v_revision;
END
$$;

CREATE FUNCTION bursar.expiry_policy_at(
    p_subject_id uuid,
    p_catalog_revision_id uuid,
    p_policy jsonb,
    p_granted_at timestamptz DEFAULT now(),
    p_subscription_id uuid DEFAULT NULL
)
RETURNS timestamptz
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_type text := COALESCE(p_policy->>'type', 'never');
    v_unit text;
    v_count integer;
    v_timezone text := 'UTC';
    v_anchor text;
    v_base timestamptz;
    v_expiry timestamptz;
    v_step interval;
BEGIN
    IF v_type = 'never' THEN
        RETURN NULL;
    END IF;

    IF v_type = 'fixed_at' THEN
        RETURN (p_policy->>'at')::timestamptz;
    END IF;

    IF v_type = 'subscription_end' THEN
        SELECT COALESCE(
            ended_at,
            cancel_at,
            CASE WHEN cancel_at_period_end THEN current_period_end END
        )
        INTO v_expiry
        FROM bursar.billing_subscriptions
        WHERE id = p_subscription_id
          AND subject_id = p_subject_id;

        RETURN v_expiry;
    END IF;

    IF v_type = 'after_grant' THEN
        v_unit := p_policy #>> '{interval,unit}';
        v_count := COALESCE(
            (p_policy #>> '{interval,count}')::integer,
            1
        );
        v_timezone := COALESCE(p_policy->>'timezone', 'UTC');
        v_anchor := 'rolling';
        v_base := p_granted_at;
    ELSIF v_type = 'end_of_window' THEN
        v_anchor := p_policy #>> '{window,type}';
        v_timezone := COALESCE(
            p_policy #>> '{window,timezone}',
            'UTC'
        );

        IF v_anchor = 'calendar' THEN
            v_unit := p_policy #>> '{window,unit}';
            v_count := COALESCE(
                (p_policy #>> '{window,count}')::integer,
                1
            );
            v_base := date_trunc(
                v_unit,
                p_granted_at AT TIME ZONE v_timezone
            ) AT TIME ZONE v_timezone;
        ELSIF v_anchor = 'plan_assignment' THEN
            v_unit := p_policy #>> '{window,interval,unit}';
            v_count := COALESCE(
                (p_policy #>> '{window,interval,count}')::integer,
                1
            );

            SELECT assignment.starts_at
            INTO v_base
            FROM bursar.account_plan_assignments AS assignment
            JOIN bursar.credit_accounts AS account
              ON account.id = assignment.account_id
            WHERE account.subject_id = p_subject_id
              AND account.account_kind = 'personal'
              AND assignment.starts_at <= p_granted_at
              AND (
                  assignment.ends_at IS NULL
                  OR assignment.ends_at > p_granted_at
              );

            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'plan assignment required for expiry policy'
                    USING ERRCODE = '22023';
            END IF;
        ELSE
            RAISE EXCEPTION 'unsupported expiry window'
                USING ERRCODE = '22023';
        END IF;
    ELSE
        RAISE EXCEPTION 'unsupported expiry policy: %', v_type
            USING ERRCODE = '22023';
    END IF;

    v_step := CASE v_unit
        WHEN 'day' THEN make_interval(days => v_count)
        WHEN 'week' THEN make_interval(weeks => v_count)
        WHEN 'month' THEN make_interval(months => v_count)
        WHEN 'year' THEN make_interval(years => v_count)
    END;

    IF v_step IS NULL THEN
        RAISE EXCEPTION 'unsupported expiry interval'
            USING ERRCODE = '22023';
    END IF;

    v_expiry := (
        v_base AT TIME ZONE v_timezone + v_step
    ) AT TIME ZONE v_timezone;

    IF v_anchor <> 'rolling' THEN
        WHILE v_expiry <= p_granted_at LOOP
            v_expiry := (
                v_expiry AT TIME ZONE v_timezone + v_step
            ) AT TIME ZONE v_timezone;
        END LOOP;
    END IF;

    RETURN v_expiry;
END
$$;

CREATE FUNCTION bursar.bucket_expiry_at(
    p_subject_id uuid,
    p_revision_id uuid,
    p_bucket_key text
)
RETURNS timestamptz
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_policy jsonb;
BEGIN
    SELECT expiry_policy
    INTO v_policy
    FROM bursar.catalog_buckets
    WHERE catalog_revision_id = p_revision_id
      AND bucket_key = p_bucket_key;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'catalog bucket not found'
            USING ERRCODE = '23503';
    END IF;

    RETURN bursar.expiry_policy_at(
        p_subject_id,
        p_revision_id,
        v_policy,
        now(),
        NULL
    );
END
$$;
