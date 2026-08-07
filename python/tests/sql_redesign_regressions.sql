-- Regression coverage for the irreversible data-model decisions in the clean-slate schema.
BEGIN;

SELECT bursar.create_tenant(
    '00000000-0000-0000-0000-000000000888'::uuid,
    'redesign-regression',
    'Redesign regression'
);
SELECT set_config(
    'bursar.tenant_id',
    '00000000-0000-0000-0000-000000000888',
    false
);

DO $$
DECLARE
    v_schema jsonb := bursar.catalog_document_shape_schema();
BEGIN
    IF NOT extensions.jsonschema_is_valid(v_schema::json) THEN
        RAISE EXCEPTION 'canonical catalog JSON Schema is invalid';
    END IF;

    IF NOT extensions.jsonschema_is_valid(bursar.measure_object_schema())
       OR NOT extensions.jsonschema_is_valid(bursar.dimension_object_schema())
       OR NOT extensions.jsonschema_is_valid(
           bursar.usage_pricing_snapshot_schema()
       )
       OR NOT extensions.jsonschema_is_valid(
           bursar.catalog_plan_rollout_schema()
       )
    THEN
        RAISE EXCEPTION 'a Bursar-owned JSON Schema document is invalid';
    END IF;

    IF NOT bursar.valid_measure_object(
        '{"input_tokens":12,"cost_usd":"0.0042"}'::jsonb
    ) OR bursar.valid_measure_object(
        '{"input_tokens":-1}'::jsonb
    ) OR bursar.valid_measure_object(
        '{"input_tokens":{"value":12}}'::jsonb
    ) THEN
        RAISE EXCEPTION 'measure-map JSON Schema contract was not enforced';
    END IF;

    IF NOT bursar.valid_dimension_object(
        '{"provider":"openrouter","cached":false,"attempt":2}'::jsonb
    ) OR bursar.valid_dimension_object(
        '{"provider":{"name":"openrouter"}}'::jsonb
    ) THEN
        RAISE EXCEPTION 'dimension-map JSON Schema contract was not enforced';
    END IF;

    IF NOT bursar.matches_catalog_definitions(
        '{"amount":"10","priority":20,"window":{"type":"calendar","unit":"month"}}'::jsonb,
        'CreditAllowance'
    ) OR bursar.matches_catalog_definitions(
        '{"amount":"10","priority":20,"window":{"type":"calendar","unit":"month"},"unknown":true}'::jsonb,
        'CreditAllowance'
    ) THEN
        RAISE EXCEPTION 'credit-allowance snapshot schema was not enforced';
    END IF;

    IF NOT bursar.matches_catalog_definitions(
        '{"type":"boolean","default":true}'::jsonb,
        'BooleanFeature'
    ) THEN
        RAISE EXCEPTION 'valid catalog schema definition was rejected';
    END IF;

    IF bursar.matches_catalog_definitions(
        '{"type":"boolean","default":true,"unknown":true}'::jsonb,
        'BooleanFeature'
    ) THEN
        RAISE EXCEPTION 'unknown catalog definition property was accepted';
    END IF;

    IF NOT extensions.jsonschema_is_valid(
        bursar.entitlement_value_schema(
            '{"type":"integer","default":2,"minimum":1,"maximum":3}'::jsonb
        )
    ) OR extensions.jsonb_matches_schema(
        bursar.entitlement_value_schema(
            '{"type":"integer","default":2,"minimum":1,"maximum":3}'::jsonb
        ),
        '4'::jsonb
    ) THEN
        RAISE EXCEPTION 'dynamic entitlement JSON Schema did not enforce its bounds';
    END IF;

    IF extensions.jsonb_matches_schema(
        v_schema::json,
        '{"version":1,"credits":{},"catalog":{"activation":{"mode":"on_publish"}}}'::jsonb
    ) THEN
        RAISE EXCEPTION 'removed catalog.activation was not rejected';
    END IF;

    IF extensions.jsonb_matches_schema(
        jsonb_build_object(
            '$defs', v_schema->'$defs',
            '$ref', '#/$defs/AutoRechargeLimits'
        )::json,
        '{"max_purchases":1,"window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"},"max_charge_minor":100,"cooldown":{"unit":"hour","count":1},"max_failures":3}'::jsonb
    ) THEN
        RAISE EXCEPTION 'removed max_failures was not rejected';
    END IF;
END $$;

DO $$
BEGIN
    BEGIN
        PERFORM bursar.publish_and_activate_catalog(
            1,
            $json$
            {
              "version": 1,
              "credits": {
                "buckets": {
                  "gifted": {"priority": 10},
                  "purchased": {"priority": 30}
                },
                "default_bucket": "purchased"
              },
              "plans": {
                "collision": {
                  "display_name": "Collision",
                  "credit_allowance": {
                    "amount": "50",
                    "priority": 10,
                    "window": {
                      "type": "calendar",
                      "unit": "month",
                      "count": 1,
                      "timezone": "UTC"
                    }
                  }
                }
              }
            }
            $json$::jsonb,
            'allowance-priority-collision'
        );
        RAISE EXCEPTION 'colliding allowance priority was accepted';
    EXCEPTION
        WHEN SQLSTATE '22023' THEN
            IF SQLERRM NOT LIKE '%allowance priority%bucket priority%' THEN
                RAISE;
            END IF;
    END;
END $$;

DO $$
DECLARE
    v_revision uuid;
    v_plan uuid;
    v_direct_subject uuid := '00000000-0000-0000-0000-000000000124';
    v_lease_subject uuid := '00000000-0000-0000-0000-000000000125';
    v_charge record;
    v_lease record;
    v_settlement record;
    v_gifted_consumed numeric;
    v_purchased_consumed numeric;
BEGIN
    SELECT revision_id
    INTO v_revision
    FROM bursar.publish_and_activate_catalog(
        1,
        $json$
        {
          "version": 1,
          "credits": {
            "buckets": {
              "gifted": {"priority": 10},
              "purchased": {"priority": 30}
            },
            "default_bucket": "purchased"
          },
          "plans": {
            "ordered": {
              "display_name": "Ordered",
              "credit_allowance": {
                "amount": "50",
                "priority": 20,
                "window": {
                  "type": "calendar",
                  "unit": "month",
                  "count": 1,
                  "timezone": "UTC"
                }
              }
            }
          }
        }
        $json$::jsonb,
        'ordered-allowance'
    );

    SELECT id
    INTO v_plan
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision
      AND plan_key = 'ordered';

    PERFORM bursar.assign_plan(v_direct_subject, v_plan, now(), NULL);
    PERFORM bursar.post_credit(
        v_direct_subject,
        'grant',
        30,
        'seed',
        'ordered-direct-gifted',
        '{}'::jsonb,
        'gifted',
        v_revision
    );
    PERFORM bursar.post_credit(
        v_direct_subject,
        'purchase',
        40,
        'seed',
        'ordered-direct-purchased',
        '{}'::jsonb,
        'purchased',
        v_revision
    );

    SELECT *
    INTO v_charge
    FROM bursar.charge_usage_for_operation(
        v_direct_subject,
        'chat',
        60,
        'ordered-direct-charge-1'
    );

    IF v_charge.error_code IS NOT NULL
       OR v_charge.charged <> 30
       OR v_charge.allowance_covered <> 30
    THEN
        RAISE EXCEPTION
            'direct charge did not apply gifted -> allowance: %',
            row_to_json(v_charge);
    END IF;

    SELECT
        COALESCE(sum(consumed) FILTER (WHERE bucket_key = 'gifted'), 0),
        COALESCE(sum(consumed) FILTER (WHERE bucket_key = 'purchased'), 0)
    INTO v_gifted_consumed, v_purchased_consumed
    FROM bursar.credit_lots
    WHERE account_id = bursar.account_for_subject(v_direct_subject);

    IF v_gifted_consumed <> 30 OR v_purchased_consumed <> 0 THEN
        RAISE EXCEPTION
            'direct charge consumed the wrong buckets: gifted %, purchased %',
            v_gifted_consumed,
            v_purchased_consumed;
    END IF;

    SELECT *
    INTO v_charge
    FROM bursar.charge_usage_for_operation(
        v_direct_subject,
        'chat',
        40,
        'ordered-direct-charge-2'
    );

    IF v_charge.error_code IS NOT NULL
       OR v_charge.charged <> 20
       OR v_charge.allowance_covered <> 20
    THEN
        RAISE EXCEPTION
            'direct charge did not apply allowance -> purchased: %',
            row_to_json(v_charge);
    END IF;

    SELECT COALESCE(sum(consumed), 0)
    INTO v_purchased_consumed
    FROM bursar.credit_lots
    WHERE account_id = bursar.account_for_subject(v_direct_subject)
      AND bucket_key = 'purchased';

    IF v_purchased_consumed <> 20 THEN
        RAISE EXCEPTION
            'direct charge did not consume purchased credits last: %',
            v_purchased_consumed;
    END IF;

    PERFORM bursar.assign_plan(v_lease_subject, v_plan, now(), NULL);
    PERFORM bursar.post_credit(
        v_lease_subject,
        'grant',
        30,
        'seed',
        'ordered-lease-gifted',
        '{}'::jsonb,
        'gifted',
        v_revision
    );
    PERFORM bursar.post_credit(
        v_lease_subject,
        'purchase',
        40,
        'seed',
        'ordered-lease-purchased',
        '{}'::jsonb,
        'purchased',
        v_revision
    );

    SELECT *
    INTO v_lease
    FROM bursar.create_lease_for_operation(
        v_lease_subject,
        'chat',
        60,
        'ordered-lease-create'
    );

    IF v_lease.error_code IS NOT NULL
       OR (
           SELECT reserved_allowance
           FROM bursar.credit_leases
           WHERE id = v_lease.lease_id
       ) <> 30
    THEN
        RAISE EXCEPTION
            'lease did not reserve allowance after gifted credit: %',
            row_to_json(v_lease);
    END IF;

    SELECT *
    INTO v_settlement
    FROM bursar.settle_lease(
        v_lease_subject,
        v_lease.lease_id,
        60,
        'ordered-lease-settle'
    );

    SELECT *
    INTO v_charge
    FROM bursar.credit_usage_charges
    WHERE idempotency_key = 'ordered-lease-settle'
      AND account_id = bursar.account_for_subject(v_lease_subject);

    IF v_settlement.error_code IS NOT NULL
       OR v_charge.charged <> 30
       OR v_charge.allowance_covered <> 30
    THEN
        RAISE EXCEPTION
            'lease settlement did not preserve source ordering: settlement %, charge %',
            row_to_json(v_settlement),
            row_to_json(v_charge);
    END IF;

    SELECT
        COALESCE(sum(consumed) FILTER (WHERE bucket_key = 'gifted'), 0),
        COALESCE(sum(consumed) FILTER (WHERE bucket_key = 'purchased'), 0)
    INTO v_gifted_consumed, v_purchased_consumed
    FROM bursar.credit_lots
    WHERE account_id = bursar.account_for_subject(v_lease_subject);

    IF v_gifted_consumed <> 30 OR v_purchased_consumed <> 0 THEN
        RAISE EXCEPTION
            'lease settlement consumed the wrong buckets: gifted %, purchased %',
            v_gifted_consumed,
            v_purchased_consumed;
    END IF;
END $$;

DO $$
DECLARE
    v_revision uuid;
    v_plan uuid;
    v_subject uuid := '00000000-0000-0000-0000-000000000126';
    v_charge record;
    v_gifted_consumed numeric;
BEGIN
    SELECT revision_id
    INTO v_revision
    FROM bursar.publish_and_activate_catalog(
        1,
        $json$
        {
          "version": 1,
          "credits": {
            "buckets": {
              "gifted": {"priority": 10},
              "purchased": {"priority": 30}
            },
            "default_bucket": "purchased"
          },
          "plans": {
            "priority_first": {
              "display_name": "Priority first",
              "credit_allowance": {
                "amount": "50",
                "priority": 5,
                "window": {
                  "type": "calendar",
                  "unit": "month",
                  "count": 1,
                  "timezone": "UTC"
                }
              }
            }
          }
        }
        $json$::jsonb,
        'explicit-allowance-order'
    );

    SELECT id
    INTO v_plan
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision
      AND plan_key = 'priority_first';

    PERFORM bursar.assign_plan(v_subject, v_plan, now(), NULL);
    PERFORM bursar.post_credit(
        v_subject,
        'grant',
        20,
        'seed',
        'priority-order-gifted',
        '{}'::jsonb,
        'gifted',
        v_revision
    );

    SELECT *
    INTO v_charge
    FROM bursar.charge_usage_for_operation(
        v_subject,
        'chat',
        20,
        'priority-order-charge'
    );

    SELECT COALESCE(sum(consumed), 0)
    INTO v_gifted_consumed
    FROM bursar.credit_lots
    WHERE account_id = bursar.account_for_subject(v_subject)
      AND bucket_key = 'gifted';

    IF v_charge.error_code IS NOT NULL
       OR v_charge.allowance_covered <> 20
       OR v_gifted_consumed <> 0
    THEN
        RAISE EXCEPTION
            'explicit allowance priority was not enforced';
    END IF;
END $$;

DO $$
DECLARE
    v_revision uuid;
    v_subject uuid := '00000000-0000-0000-0000-000000000123';
    v_entry uuid;
BEGIN
    SELECT revision_id INTO v_revision FROM bursar.publish_and_activate_catalog(
        1,
        '{"version":1,"pricing":{"operations":{"chat":{"measures":{"tokens":{"unit":"token"}},"dimensions":{}}},"rate_cards":{"standard":{"operations":{"chat":{"rules":[],"unmatched":{"action":"charge","charge":{"type":"per_unit","measure":"tokens","rate":"1"}}}}}}},"credits":{"accounting":{"unit":"credit","scale":6,"rounding":"half_up"},"buckets":{"expiring":{"priority":10,"expiry":{"type":"after_grant","interval":{"unit":"month","count":2},"timezone":"Asia/Kolkata"}}},"default_bucket":"expiring"},"plans":{"p":{"display_name":"P","rate_card":"standard","credit_allowance":{"amount":"3","priority":5,"window":{"type":"plan_assignment","interval":{"unit":"month","count":1},"timezone":"Asia/Kolkata"}}}},"commerce":{"providers":{"stripe":{"type":"stripe"},"vendor":{"type":"custom","adapter":"tests.vendor"}},"offers":{"o":{"type":"subscription","display_name":"O","price":{"amount_minor":1000,"currency":"USD"},"providers":{"stripe":{"type":"stripe_price","price_id":"price_1"}},"plan":"p","billing_interval":{"unit":"year","count":1}},"pack":{"type":"topup","display_name":"Pack","price":{"amount_minor":1000,"currency":"USD"},"providers":{"vendor":{"type":"custom_object","object_kind":"one_time","external_id":"pack_1"}},"credits_per_unit":"10","bucket":"expiring","quantity":{"minimum":1,"maximum":3,"default":2}}},"auto_recharge":{"eligible_topups":["pack"],"balance_below":{"minimum":"0","maximum":"2","default":"2"},"rearm_above":"3","quantity":{"minimum":1,"maximum":3,"default":2},"limits":{"max_purchases":3,"window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"},"max_charge_minor":10000,"cooldown":{"unit":"hour","count":1},"max_consecutive_failures":3}}}}'::jsonb,
        'regression'
    );
    IF NOT EXISTS (SELECT 1 FROM bursar.catalog_buckets WHERE catalog_revision_id=v_revision AND expires_after_unit='month' AND expires_after_count=2 AND expires_after_timezone='Asia/Kolkata') THEN
        RAISE EXCEPTION 'raw YAML bucket period was not projected';
    END IF;
  IF NOT EXISTS (SELECT 1 FROM bursar.catalog_plans WHERE catalog_revision_id=v_revision AND credit_allowance_reset_anchor='plan_assignment') THEN
        RAISE EXCEPTION 'included-credit period was not projected';
    END IF;
  IF NOT EXISTS (SELECT 1 FROM bursar.catalog_provider_refs WHERE catalog_revision_id=v_revision AND lookup_type='one_time') THEN
    RAISE EXCEPTION 'typed custom provider reference was not projected';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM bursar.catalog_auto_recharge_policies WHERE catalog_revision_id=v_revision AND quantity=2 AND max_purchases=3) THEN
        RAISE EXCEPTION 'auto-recharge policy was not projected';
    END IF;

    SELECT entry_id INTO v_entry FROM bursar.post_credit(v_subject,'grant',5,'grant','digest-1','{"source":"a"}'::jsonb,'expiring',v_revision);
    IF v_entry IS NULL OR (SELECT expires_at FROM bursar.credit_lots WHERE source_entry_id=v_entry) IS NULL THEN
        RAISE EXCEPTION 'rolling bucket expiry was not derived';
    END IF;
    IF (SELECT error_code FROM bursar.post_credit(v_subject,'grant',5,'grant','digest-1','{"source":"b"}'::jsonb,'expiring',v_revision)) <> 'idempotency_conflict' THEN
        RAISE EXCEPTION 'request digest conflict was not detected';
    END IF;
END $$;

DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000127';
    v_plan uuid;
    v_result record;
BEGIN
    PERFORM bursar.publish_and_activate_catalog(
        1,
        '{"version":1,"pricing":{"operations":{"chat":{"measures":{"calls":{"unit":"call"}},"dimensions":{}}},"rate_cards":{"standard":{"operations":{"chat":{"rules":[],"unmatched":{"action":"charge","charge":{"type":"per_unit","measure":"calls","rate":"1"}}}}}}},"credits":{"accounting":{"unit":"credit","scale":6,"rounding":"half_up"},"buckets":{"default":{"priority":10,"expiry":{"type":"never"}}},"default_bucket":"default"},"plans":{"metered":{"display_name":"Metered","rate_card":"standard","credit_allowance":{"amount":"3","priority":5,"window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"}},"quotas":{"chat":{"operation":"chat","measure":"calls","limit":"1","window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"},"enforcement":"block"}}}}}'::jsonb,
        'atomic-policy'
    );
    SELECT id INTO v_plan FROM bursar.catalog_plans WHERE plan_key='metered' AND catalog_revision_id=(SELECT id FROM bursar.catalog_revisions WHERE status='active');
    PERFORM bursar.assign_plan(v_subject,v_plan,now(),NULL);
    SELECT * INTO v_result
    FROM bursar.charge_usage_for_operation(
        v_subject,
        'chat',
        2,
        'policy-charge-1',
        p_measures => '{"calls":1}'::jsonb
    );
    IF v_result.allowance_covered <> 2 OR v_result.error_code IS NOT NULL THEN
        RAISE EXCEPTION 'included allowance was not consumed atomically';
    END IF;
    SELECT * INTO v_result
    FROM bursar.charge_usage_for_operation(
        v_subject,
        'chat',
        1,
        'policy-charge-2',
        p_measures => '{"calls":1}'::jsonb
    );
    IF v_result.error_code <> 'quota_exceeded' THEN
        RAISE EXCEPTION 'plan quota was not enforced atomically';
    END IF;
END $$;

DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000128';
    v_result record;
BEGIN
    SELECT *
    INTO v_result
    FROM bursar.charge_usage(
        v_subject,
        'chat',
        0,
        'normalized-null-metadata',
        p_metadata => NULL
    );
    IF v_result.error_code IS NOT NULL OR v_result.replayed THEN
        RAISE EXCEPTION 'initial null-metadata charge failed: %', row_to_json(v_result);
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.charge_usage(
        v_subject,
        'chat',
        0,
        'normalized-null-metadata',
        p_metadata => '{}'::jsonb
    );
    IF v_result.error_code IS NOT NULL OR NOT v_result.replayed THEN
        RAISE EXCEPTION 'normalized null metadata did not replay: %', row_to_json(v_result);
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.record_usage(
        v_subject,
        'chat',
        0,
        'oversized-record-model',
        p_model => repeat('m', 256)
    );
    IF v_result.error_code <> 'invalid_request' THEN
        RAISE EXCEPTION 'record_usage accepted an oversized model';
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.charge_usage_for_operation(
        v_subject,
        'chat',
        0,
        'oversized-charge-feature',
        p_feature => repeat('f', 256)
    );
    IF v_result.error_code <> 'invalid_request' THEN
        RAISE EXCEPTION 'charge_usage_for_operation accepted an oversized feature';
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.create_lease(
        v_subject,
        'chat',
        0,
        'oversized-lease-feature',
        p_feature => repeat('f', 256)
    );
    IF v_result.error_code <> 'invalid_request' THEN
        RAISE EXCEPTION 'create_lease accepted an oversized feature';
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.settle_lease(
        v_subject,
        '00000000-0000-0000-0000-000000000001',
        0,
        'oversized-settlement-region',
        p_region => repeat('r', 256)
    );
    IF v_result.error_code <> 'invalid_request' THEN
        RAISE EXCEPTION 'settle_lease accepted an oversized region';
    END IF;
END $$;

ROLLBACK;
