-- Regression coverage for the irreversible data-model decisions in the clean-slate schema.
DO $$
DECLARE
    v_schema jsonb := bursar.catalog_document_shape_schema();
    v_error text;
BEGIN
    v_error := bursar.catalog_shape_error(
        '{"version":1,"credits":{},"catalog":{"activation":{"mode":"on_publish"}}}'::jsonb,
        v_schema,
        v_schema->'$defs'
    );
    IF v_error NOT LIKE '%.catalog.activation is not allowed' THEN
        RAISE EXCEPTION 'legacy catalog.activation was not rejected: %', v_error;
    END IF;

    v_error := bursar.catalog_shape_error(
        '{"max_purchases":1,"window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"},"max_charge_minor":100,"cooldown":{"unit":"hour","count":1},"max_failures":3}'::jsonb,
        v_schema #> '{$defs,AutoRechargeLimits}',
        v_schema->'$defs',
        '$.commerce.auto_recharge.limits'
    );
    IF v_error NOT LIKE '%.max_failures is not allowed' THEN
        RAISE EXCEPTION 'legacy max_failures was not rejected: %', v_error;
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
    v_direct_subject uuid := '00000000-0000-0000-0000-000000000556';
    v_lease_subject uuid := '00000000-0000-0000-0000-000000000557';
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
    v_subject uuid := '00000000-0000-0000-0000-000000000558';
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
            "legacy": {
              "display_name": "Legacy",
              "credit_allowance": {
                "amount": "50",
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
        'legacy-allowance-order'
    );

    SELECT id
    INTO v_plan
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision
      AND plan_key = 'legacy';

    PERFORM bursar.assign_plan(v_subject, v_plan, now(), NULL);
    PERFORM bursar.post_credit(
        v_subject,
        'grant',
        20,
        'seed',
        'legacy-gifted',
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
        'legacy-allowance-charge'
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
            'omitted allowance priority did not preserve allowance-first behavior';
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
        '{"version":1,"pricing":{"operations":{"chat":{"measures":{"tokens":{"unit":"token"}},"dimensions":{}}},"rate_cards":{"standard":{"operations":{"chat":{"rules":[],"unmatched":{"action":"charge","charge":{"type":"per_unit","measure":"tokens","rate":"1"}}}}}}},"credits":{"accounting":{"unit":"credit","scale":6,"rounding":"half_up"},"buckets":{"expiring":{"priority":10,"expiry":{"type":"after_grant","interval":{"unit":"month","count":2},"timezone":"Asia/Kolkata"}}},"default_bucket":"expiring"},"plans":{"p":{"display_name":"P","rate_card":"standard","credit_allowance":{"amount":"3","window":{"type":"plan_assignment","interval":{"unit":"month","count":1},"timezone":"Asia/Kolkata"}}}},"commerce":{"providers":{"stripe":{"type":"stripe"},"vendor":{"type":"custom","adapter":"tests.vendor"}},"offers":{"o":{"type":"subscription","display_name":"O","price":{"amount_minor":1000,"currency":"USD"},"providers":{"stripe":{"type":"stripe_price","price_id":"price_1"}},"plan":"p","billing_interval":{"unit":"year","count":1}},"pack":{"type":"topup","display_name":"Pack","price":{"amount_minor":1000,"currency":"USD"},"providers":{"vendor":{"type":"custom_object","object_kind":"one_time","external_id":"pack_1"}},"credits_per_unit":"10","bucket":"expiring","quantity":{"minimum":1,"maximum":3,"default":2}}},"auto_recharge":{"eligible_topups":["pack"],"balance_below":{"minimum":"0","maximum":"2","default":"2"},"rearm_above":"3","quantity":{"minimum":1,"maximum":3,"default":2},"limits":{"max_purchases":3,"window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"},"max_consecutive_failures":3}}}}'::jsonb,
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
    v_subject uuid := '00000000-0000-0000-0000-000000000555';
    v_plan uuid;
    v_result record;
BEGIN
    PERFORM bursar.publish_and_activate_catalog(
        1,
        '{"version":1,"pricing":{"operations":{"chat":{"measures":{"calls":{"unit":"call"}},"dimensions":{}}},"rate_cards":{"standard":{"operations":{"chat":{"rules":[],"unmatched":{"action":"charge","charge":{"type":"per_unit","measure":"calls","rate":"1"}}}}}}},"credits":{"accounting":{"unit":"credit","scale":6,"rounding":"half_up"},"buckets":{"default":{"priority":10,"expiry":{"type":"never"}}},"default_bucket":"default"},"plans":{"metered":{"display_name":"Metered","rate_card":"standard","credit_allowance":{"amount":"3","window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"}},"quotas":{"chat":{"operation":"chat","measure":"calls","limit":"1","window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"},"enforcement":"block"}}}}}'::jsonb,
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
