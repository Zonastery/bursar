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
