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

-- Exact retries are resolved from immutable caller input before current plan
-- policy or due-expiry work. Targeted lot retries likewise resolve before the
-- lot's now-consumed availability is revalidated.
DO $$
DECLARE
    v_doc jsonb := $json$
    {
      "version": 1,
      "credits": {
        "buckets": {"default": {"priority": 10}},
        "default_bucket": "default",
        "policies": {
          "prepaid": {"type": "prepaid"},
          "line": {"type": "credit_line", "limit": "20"}
        }
      },
      "plans": {
        "prepaid": {
          "display_name": "Prepaid",
          "credit_policy": "prepaid"
        },
        "line": {
          "display_name": "Credit line",
          "credit_policy": "line"
        }
      }
    }
    $json$::jsonb;
    v_doc_2 jsonb;
    v_revision_1 uuid;
    v_revision_2 uuid;
    v_prepaid_plan uuid;
    v_line_plan uuid;
    v_policy_subject uuid := '00000000-0000-0000-0000-000000000140';
    v_expiry_subject uuid := '00000000-0000-0000-0000-000000000141';
    v_lot_subject uuid := '00000000-0000-0000-0000-000000000142';
    v_first record;
    v_replay record;
    v_expiring_entry uuid;
    v_expiring_lot uuid;
    v_explicit_expiry timestamptz := now() + interval '1 day';
    v_lot uuid;
    v_first_revoke uuid;
    v_second_revoke uuid;
BEGIN
    SELECT revision_id
    INTO v_revision_1
    FROM bursar.publish_and_activate_catalog(
        1,
        v_doc,
        'idempotency-ordering-v1'
    );

    SELECT id
    INTO v_prepaid_plan
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision_1
      AND plan_key = 'prepaid';

    SELECT id
    INTO v_line_plan
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision_1
      AND plan_key = 'line';

    PERFORM bursar.assign_plan(
        v_policy_subject,
        v_prepaid_plan,
        now(),
        NULL
    );
    PERFORM bursar.post_credit(
        v_policy_subject,
        'grant',
        10,
        'policy-retry-seed',
        'policy-retry-seed'
    );

    SELECT *
    INTO v_first
    FROM bursar.post_credit(
        v_policy_subject,
        'usage',
        -1,
        'policy-retry-debit',
        'policy-retry-debit'
    );

    PERFORM bursar.assign_plan(
        v_policy_subject,
        v_line_plan,
        now(),
        NULL
    );

    SELECT *
    INTO v_replay
    FROM bursar.post_credit(
        v_policy_subject,
        'usage',
        -1,
        'policy-retry-debit',
        'policy-retry-debit'
    );

    IF v_first.error_code IS NOT NULL
       OR v_replay.error_code IS NOT NULL
       OR NOT v_replay.replayed
       OR v_replay.entry_id <> v_first.entry_id
    THEN
        RAISE EXCEPTION
            'post_credit did not replay before mutable plan policy: % / %',
            row_to_json(v_first),
            row_to_json(v_replay);
    END IF;

    SELECT entry_id
    INTO v_expiring_entry
    FROM bursar.post_credit(
        v_expiry_subject,
        'grant',
        5,
        'expiry-retry-grant',
        'expiry-retry-grant',
        p_expires_at => v_explicit_expiry
    );

    SELECT id
    INTO v_expiring_lot
    FROM bursar.credit_lots
    WHERE source_entry_id = v_expiring_entry;

    PERFORM set_config('bursar.mutation_context', 'internal', true);
    UPDATE bursar.credit_lots
    SET expires_at = now() - interval '1 second'
    WHERE id = v_expiring_lot;

    SELECT *
    INTO v_replay
    FROM bursar.post_credit(
        v_expiry_subject,
        'grant',
        5,
        'expiry-retry-grant',
        'expiry-retry-grant',
        p_expires_at => v_explicit_expiry
    );

    IF v_replay.error_code IS NOT NULL
       OR NOT v_replay.replayed
       OR v_replay.entry_id <> v_expiring_entry
       OR EXISTS (
           SELECT 1
           FROM bursar.credit_ledger_entries
           WHERE account_id = bursar.account_for_subject(v_expiry_subject)
             AND kind = 'expiry'
       )
       OR (
           SELECT consumed
           FROM bursar.credit_lots
           WHERE id = v_expiring_lot
       ) <> 0
    THEN
        RAISE EXCEPTION
            'post_credit retry performed expiry work before replay';
    END IF;

    PERFORM bursar.post_credit(
        v_lot_subject,
        'grant',
        4,
        'targeted-retry-seed',
        'targeted-retry-seed'
    );
    SELECT id
    INTO v_lot
    FROM bursar.credit_lots
    WHERE account_id = bursar.account_for_subject(v_lot_subject)
      AND consumed = 0
    ORDER BY created_at, id
    LIMIT 1;

    SELECT entry_id
    INTO v_first_revoke
    FROM bursar.revoke_lot(v_lot, 4, 'targeted-retry-revoke');
    SELECT entry_id
    INTO v_second_revoke
    FROM bursar.revoke_lot(v_lot, 4, 'targeted-retry-revoke');

    IF v_first_revoke IS NULL
       OR v_second_revoke IS DISTINCT FROM v_first_revoke
       OR (
           SELECT count(*)
           FROM bursar.credit_ledger_entries
           WHERE account_id = bursar.account_for_subject(v_lot_subject)
             AND idempotency_key = 'targeted-retry-revoke'
       ) <> 1
    THEN
        RAISE EXCEPTION 'targeted lot debit was not exactly replayable';
    END IF;

    v_doc_2 := jsonb_set(
        v_doc,
        '{plans,prepaid,display_name}',
        '"Prepaid v2"'::jsonb
    );
    SELECT revision_id
    INTO v_revision_2
    FROM bursar.publish_and_activate_catalog(
        1,
        v_doc_2,
        'idempotency-ordering-v2',
        false
    );

    BEGIN
        PERFORM bursar.expiry_policy_at(
            v_policy_subject,
            v_revision_2,
            '{
              "type":"end_of_window",
              "window":{
                "type":"plan_assignment",
                "interval":{"unit":"month","count":1},
                "timezone":"UTC"
              }
            }'::jsonb,
            now(),
            NULL
        );
        RAISE EXCEPTION
            'expiry policy crossed catalog revisions to find an assignment';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            IF SQLERRM NOT LIKE '%plan assignment required%' THEN
                RAISE;
            END IF;
    END;
END
$$;

-- PostgreSQL orders NaN above every finite numeric, so relational checks alone
-- can accept non-finite upper/rearm values. The financial projection must
-- reject every such value at its table boundary.
DO $$
DECLARE
    v_policy bursar.catalog_auto_recharge_policies;
    v_case integer;
    v_nonfinite numeric;
BEGIN
    SELECT policy.*
    INTO v_policy
    FROM bursar.catalog_auto_recharge_policies AS policy
    ORDER BY policy.catalog_revision_id DESC
    LIMIT 1;

    IF v_policy.catalog_revision_id IS NULL THEN
        RAISE EXCEPTION 'auto-recharge fixture policy is missing';
    END IF;

    FOR v_case IN 1..4 LOOP
        v_nonfinite := CASE
            WHEN v_case IN (1, 3) THEN 'NaN'::numeric
            ELSE 'Infinity'::numeric
        END;

        BEGIN
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
            VALUES (
                v_policy.catalog_revision_id,
                v_policy.eligible_topup_keys,
                v_policy.default_topup_key,
                v_policy.quantity_min,
                v_policy.quantity_max,
                v_policy.quantity,
                v_policy.balance_min,
                CASE
                    WHEN v_case <= 2 THEN v_nonfinite
                    ELSE v_policy.balance_max
                END,
                v_policy.balance_below,
                CASE
                    WHEN v_case > 2 THEN v_nonfinite
                    ELSE v_policy.rearm_above
                END,
                v_policy.max_purchases,
                v_policy.max_charge_minor,
                v_policy.cooldown_seconds,
                v_policy.max_consecutive_failures,
                v_policy.failure_action,
                v_policy.period_unit,
                v_policy.period_count,
                v_policy.period_anchor,
                v_policy.period_timezone,
                v_policy.definition
            );

            RAISE EXCEPTION
                'auto-recharge policy accepted non-finite numeric case %',
                v_case;
        EXCEPTION
            -- Fixed-scale numeric columns can reject Infinity during the
            -- cast before the table CHECK runs; either SQLSTATE proves the
            -- persistence boundary rejected the non-finite value.
            WHEN check_violation OR numeric_value_out_of_range THEN
                NULL;
        END;
    END LOOP;
END
$$;

-- NULLs must be rejected by the checkout boundary itself rather than falling
-- through three-valued predicates into table NOT NULL failures after subject
-- provisioning has begun.
DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000143';
    v_case integer;
BEGIN
    FOR v_case IN 1..3 LOOP
        BEGIN
            PERFORM bursar.create_checkout_intent(
                v_subject,
                'test-provider',
                CASE WHEN v_case = 1 THEN NULL ELSE 'credit_topup' END,
                'test-product',
                CASE
                    WHEN v_case = 2 THEN NULL
                    ELSE decode(repeat('00', 32), 'hex')
                END,
                CASE
                    WHEN v_case = 3 THEN NULL
                    ELSE now() + interval '1 hour'
                END
            );
            RAISE EXCEPTION 'checkout accepted NULL required field case %',
                v_case;
        EXCEPTION
            WHEN invalid_parameter_value THEN
                NULL;
        END;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM bursar.subjects
        WHERE id = v_subject
    ) THEN
        RAISE EXCEPTION 'invalid checkout provisioned a subject';
    END IF;
END
$$;

-- Required enum, boolean, interval, and batch arguments must not exploit SQL's
-- three-valued logic to reach mutation code as NULL.
DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000144';
    v_team uuid := '00000000-0000-0000-0000-000000000145';
    v_result record;
BEGIN
    BEGIN
        PERFORM bursar.account_for_subject(v_subject, NULL);
        RAISE EXCEPTION 'account_for_subject accepted a NULL account kind';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    BEGIN
        PERFORM bursar.targeted_lot_debit(
            '00000000-0000-0000-0000-000000000001',
            NULL,
            1,
            'null-targeted-kind'
        );
        RAISE EXCEPTION 'targeted_lot_debit accepted a NULL kind';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    BEGIN
        PERFORM bursar.policy_period_window(
            now(),
            NULL,
            1,
            'rolling',
            'UTC'
        );
        RAISE EXCEPTION 'policy_period_window accepted a NULL unit';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    BEGIN
        PERFORM bursar.policy_period_window(
            now(),
            'day',
            1,
            NULL,
            'UTC'
        );
        RAISE EXCEPTION 'policy_period_window accepted a NULL anchor';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    IF bursar.set_team_member(v_team, v_subject, NULL) THEN
        RAISE EXCEPTION 'set_team_member accepted a NULL role';
    END IF;

    BEGIN
        PERFORM bursar.upsert_billing_refund(
            '00000000-0000-0000-0000-000000000001',
            'null-refund-status',
            1,
            NULL,
            NULL,
            now(),
            NULL,
            NULL,
            '{}'::jsonb
        );
        RAISE EXCEPTION 'upsert_billing_refund accepted a NULL status';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    BEGIN
        PERFORM bursar.upsert_billing_refund(
            '00000000-0000-0000-0000-000000000001',
            'zero-refund-amount',
            0,
            'pending',
            NULL,
            now(),
            NULL,
            NULL,
            '{}'::jsonb
        );
        RAISE EXCEPTION 'upsert_billing_refund accepted a zero amount';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    BEGIN
        PERFORM bursar.upsert_auto_recharge_profile(
            v_subject,
            NULL,
            'test-provider',
            '00000000-0000-0000-0000-000000000001',
            1,
            1,
            1,
            'day',
            1,
            'rolling',
            'UTC'
        );
        RAISE EXCEPTION 'auto-recharge profile accepted NULL enabled';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    BEGIN
        PERFORM bursar.upsert_auto_recharge_profile(
            v_subject,
            true,
            'test-provider',
            '00000000-0000-0000-0000-000000000001',
            1,
            1,
            1,
            'day',
            1,
            'rolling',
            'UTC',
            true,
            NULL
        );
        RAISE EXCEPTION 'auto-recharge profile accepted NULL state';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    IF bursar.advance_auto_recharge_attempt(
        '00000000-0000-0000-0000-000000000001',
        NULL
    ) THEN
        RAISE EXCEPTION 'advance_auto_recharge_attempt accepted NULL state';
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.open_subscription_change(
        '00000000-0000-0000-0000-000000000001',
        '00000000-0000-0000-0000-000000000002',
        now(),
        NULL,
        'null-effective-behavior'
    );
    IF v_result.error_code <> 'invalid_request' THEN
        RAISE EXCEPTION 'open_subscription_change accepted NULL behavior';
    END IF;

    IF bursar.advance_subscription_change(1, NULL) THEN
        RAISE EXCEPTION 'advance_subscription_change accepted NULL state';
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.create_lease_for_operation(
        v_subject,
        'null-ttl',
        1,
        'null-ttl',
        NULL
    );
    IF v_result.error_code <> 'invalid_request' THEN
        RAISE EXCEPTION 'create_lease accepted NULL TTL';
    END IF;

    BEGIN
        PERFORM bursar.claim_outbox_events(
            NULL::integer,
            60,
            NULL::text[]
        );
        RAISE EXCEPTION 'claim_outbox_events accepted NULL limit';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    BEGIN
        PERFORM bursar.fail_outbox_event(
            1,
            '00000000-0000-0000-0000-000000000001',
            'failure',
            NULL,
            10
        );
        RAISE EXCEPTION 'fail_outbox_event accepted NULL retry delay';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    BEGIN
        PERFORM bursar.run_storage_partition_maintenance(NULL, now());
        RAISE EXCEPTION 'partition maintenance accepted a NULL parent';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            NULL;
    END;

    IF EXISTS (
        SELECT 1
        FROM bursar.subjects
        WHERE id = v_subject
    ) THEN
        RAISE EXCEPTION 'invalid NULL-boundary requests provisioned a subject';
    END IF;
END
$$;

-- Full-refund remainder is recalculated under the account lock. A distinct
-- key after the full amount was returned must observe no remainder rather than
-- attempting a second refund.
DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000146';
    v_debit uuid;
    v_first record;
    v_second record;
BEGIN
    PERFORM bursar.post_credit(
        v_subject,
        'grant',
        5,
        'refund-lock-seed',
        'refund-lock-seed'
    );
    SELECT entry_id
    INTO v_debit
    FROM bursar.post_credit(
        v_subject,
        'usage',
        -5,
        'refund-lock-debit',
        'refund-lock-debit'
    );

    SELECT *
    INTO v_first
    FROM bursar.refund_credit_by_entry(
        v_debit,
        NULL,
        'refund-lock-first'
    );
    SELECT *
    INTO v_second
    FROM bursar.refund_credit_by_entry(
        v_debit,
        NULL,
        'refund-lock-second'
    );

    IF v_first.error_code IS NOT NULL
       OR v_first.replayed
       OR v_second.error_code <> 'nothing_to_refund'
    THEN
        RAISE EXCEPTION
            'full refund remainder was not serialized: % / %',
            row_to_json(v_first),
            row_to_json(v_second);
    END IF;
END
$$;

ROLLBACK;
