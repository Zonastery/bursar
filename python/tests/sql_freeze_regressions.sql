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
    FROM bursar.charge_usage_for_operation(v_subject,'chat',2,'freeze-charge-1','chat','model-x','region-y','{"trace":"a"}'::jsonb);
    SELECT * INTO v_replay
    FROM bursar.charge_usage_for_operation(v_subject,'chat',2,'freeze-charge-1','chat','model-x','region-y','{"trace":"a"}'::jsonb);
    IF v_first.error_code IS NOT NULL OR NOT v_replay.replayed OR v_replay.charge_id<>v_first.charge_id THEN
        RAISE EXCEPTION 'canonical usage replay failed';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM bursar.credit_usage_charges
        WHERE id=v_first.charge_id AND model='model-x' AND region='region-y'
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
        '{"version":1,"credits":{"buckets":{"default":{}},"spend_order":["default"],"default_bucket":"default"},"plans":{"warn-plan":{"display_name":"Warn","limits":{"export":{"max_calls":1,"period":{"unit":"month","count":1,"anchor":"calendar","timezone":"UTC"},"action":"warn"}}}},"payments":{"subscriptions":{},"topups":{}}}'::jsonb,
        'freeze-warn'
    );
    SELECT id INTO v_plan FROM bursar.catalog_plans WHERE catalog_revision_id=v_revision AND plan_key='warn-plan';
    PERFORM bursar.assign_plan(v_subject,v_plan,now(),NULL);
    PERFORM bursar.post_credit(v_subject,'grant',10,'freeze-grant','freeze-grant-702');
    SELECT * INTO v_lease FROM bursar.create_lease_for_operation(v_subject,'export',1,'freeze-unlimited-702','unlimited');
    IF v_lease.error_code IS NOT NULL OR v_lease.lease_id IS NULL THEN
        RAISE EXCEPTION 'unlimited feature lease failed: %',v_lease.error_code;
    END IF;
    PERFORM bursar.release_lease(v_subject,v_lease.lease_id);
    SELECT error_code INTO v_error_1 FROM bursar.charge_usage_for_operation(v_subject,'export',1,'freeze-warn-charge-1','export');
    SELECT error_code INTO v_error_2 FROM bursar.charge_usage_for_operation(v_subject,'export',1,'freeze-warn-charge-2','export');
    SELECT count(*) INTO v_event_count
    FROM bursar.feature_limit_events e
    JOIN bursar.credit_accounts a ON a.id=e.account_id
    WHERE a.subject_id=v_subject AND e.feature='export' AND e.action='warn';
    IF v_error_1 IS NOT NULL OR v_error_2 IS NOT NULL OR v_event_count=0 THEN
        RAISE EXCEPTION 'warn feature action was not recorded (% / %, events=%)',v_error_1,v_error_2,v_event_count;
    END IF;
END $$;
