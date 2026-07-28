-- Focused regressions for the final database freeze pass.
DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000701';
    v_plan uuid;
    v_first record;
    v_replay record;
    v_lease uuid;
BEGIN
    PERFORM bursar.account_for_subject(v_subject);
    PERFORM bursar.post_credit(v_subject,'grant',10,'freeze-grant','freeze-grant-701');
    SELECT p.id INTO v_plan
    FROM bursar.catalog_plans p
    JOIN bursar.catalog_revisions r ON r.id=p.catalog_revision_id AND r.status='active'
    WHERE p.plan_key='metered';
    IF v_plan IS NULL OR NOT bursar.assign_plan(v_subject,v_plan,now(),NULL) THEN
        RAISE EXCEPTION 'freeze test plan assignment failed';
    END IF;

    SELECT * INTO v_first
    FROM bursar.charge_usage_for_operation(
        v_subject,
        'chat',
        2,
        'freeze-charge-1',
        p_model => 'model-x',
        p_region => 'region-y',
        p_metadata => '{"trace":"a"}'::jsonb,
        p_measures => '{"calls":1}'::jsonb,
        p_dimensions => '{"model":"model-x","region":"region-y"}'::jsonb
    );
    SELECT * INTO v_replay
    FROM bursar.charge_usage_for_operation(
        v_subject,
        'chat',
        2,
        'freeze-charge-1',
        p_model => 'model-x',
        p_region => 'region-y',
        p_metadata => '{"trace":"a"}'::jsonb,
        p_measures => '{"calls":1}'::jsonb,
        p_dimensions => '{"model":"model-x","region":"region-y"}'::jsonb
    );
    IF v_first.error_code IS NOT NULL OR NOT v_replay.replayed OR v_replay.charge_id<>v_first.charge_id THEN
        RAISE EXCEPTION 'canonical usage replay failed';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM bursar.credit_usage_charges
        WHERE id=v_first.charge_id AND model='model-x' AND region='region-y'
          AND measures='{"calls":1}'::jsonb
          AND dimensions='{"model":"model-x","region":"region-y"}'::jsonb
          AND metadata='{"trace":"a"}'::jsonb
    ) THEN
        RAISE EXCEPTION 'usage dimensions or metadata were lost';
    END IF;

    SELECT lease_id INTO v_lease
    FROM bursar.create_lease_for_operation(v_subject,'zero-settle',1,'freeze-lease-701');
    IF v_lease IS NULL THEN RAISE EXCEPTION 'zero-settle lease creation failed'; END IF;
    IF EXISTS (SELECT 1 FROM bursar.settle_lease(v_subject,v_lease,0,'freeze-settle-701') WHERE error_code IS NOT NULL) THEN
        RAISE EXCEPTION 'zero-cost settlement failed';
    END IF;
END $$;

DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000702';
    v_plan uuid;
    v_lease record;
    v_revision uuid;
    v_error_1 text;
    v_error_2 text;
    v_event_count integer;
BEGIN
    SELECT revision_id INTO v_revision
    FROM bursar.publish_and_activate_catalog(
        1,
        '{"version":1,"pricing":{"operations":{"export":{"measures":{"calls":{"unit":"call"}},"dimensions":{}}},"rate_cards":{"standard":{"operations":{"export":{"rules":[],"unmatched":{"action":"charge","charge":{"type":"per_unit","measure":"calls","rate":"1"}}}}}}},"credits":{"accounting":{"unit":"credit","scale":6,"rounding":"half_up"},"buckets":{"default":{"priority":10,"expiry":{"type":"never"}}},"default_bucket":"default"},"plans":{"warn-plan":{"display_name":"Warn","rate_card":"standard","quotas":{"export":{"operation":"export","measure":"calls","limit":"1","window":{"type":"calendar","unit":"month","count":1,"timezone":"UTC"},"enforcement":"allow","emit_at_percent":[100]}}}}}'::jsonb,
        'freeze-warn'
    );
    SELECT id INTO v_plan FROM bursar.catalog_plans WHERE catalog_revision_id=v_revision AND plan_key='warn-plan';
    PERFORM bursar.assign_plan(v_subject,v_plan,now(),NULL);
    PERFORM bursar.post_credit(v_subject,'grant',10,'freeze-grant','freeze-grant-702');
    SELECT * INTO v_lease
    FROM bursar.create_lease_for_operation(
        v_subject,
        'export',
        1,
        'freeze-unlimited-702',
        p_measures => '{"calls":1}'::jsonb
    );
    IF v_lease.error_code IS NOT NULL OR v_lease.lease_id IS NULL THEN
        RAISE EXCEPTION 'unlimited feature lease failed: %',v_lease.error_code;
    END IF;
    PERFORM bursar.release_lease(v_subject,v_lease.lease_id);
    SELECT error_code INTO v_error_1
    FROM bursar.charge_usage_for_operation(
        v_subject,
        'export',
        1,
        'freeze-warn-charge-1',
        p_measures => '{"calls":1}'::jsonb
    );
    SELECT error_code INTO v_error_2
    FROM bursar.charge_usage_for_operation(
        v_subject,
        'export',
        1,
        'freeze-warn-charge-2',
        p_measures => '{"calls":1}'::jsonb
    );
    SELECT count(*) INTO v_event_count
    FROM bursar.quota_events AS event
    JOIN bursar.quota_windows AS quota_window
      ON quota_window.id = event.quota_window_id
    JOIN bursar.credit_accounts AS account
      ON account.id = quota_window.account_id
    WHERE account.subject_id = v_subject
      AND quota_window.quota_key = 'export'
      AND event.event_type = 'threshold';
    IF v_error_1 IS NOT NULL OR v_error_2 IS NOT NULL OR v_event_count=0 THEN
        RAISE EXCEPTION 'allow quota threshold was not recorded (% / %, events=%)',v_error_1,v_error_2,v_event_count;
    END IF;
END $$;
