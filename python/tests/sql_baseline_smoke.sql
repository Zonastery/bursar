-- Run after applying bundled migrations to an empty database.
CREATE TEMP TABLE bursar_trigger_users (id uuid PRIMARY KEY);
CREATE TRIGGER bursar_trigger_users_account_created
AFTER INSERT ON bursar_trigger_users
FOR EACH ROW
EXECUTE FUNCTION bursar.provision_subject_account_on_insert();

DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000099';
    v_lease uuid;
    v_entry uuid;
    v_plan uuid;
BEGIN
    PERFORM bursar.publish_and_activate_catalog(
        1,
        '{"version":1,"credits":{"accounting":{"unit":"credit","scale":6,"rounding":"half_up"},"buckets":{"included":{"priority":10,"expiry":{"type":"never"}},"purchased":{"priority":20,"expiry":{"type":"never"}}},"default_bucket":"purchased","policies":{"line":{"type":"credit_line","limit":"20"}},"grant_programs":{"welcome":{"trigger":"account_created","awards":[{"recipient":"subject","amount":"2","bucket":"included"}],"max_awards_per_subject":1,"idempotency_scope":"subject"}}},"admission":{"policies":{"pro":{"max_in_flight":1}}},"plans":{"pro":{"display_name":"Pro","credit_policy":"line","admission_policy":"pro"}}}'::jsonb,
        'smoke'
    );
    INSERT INTO bursar_trigger_users(id) VALUES (v_subject);
    IF (SELECT balance FROM bursar.credit_accounts WHERE subject_id = v_subject AND account_kind = 'personal') <> 2 THEN
        RAISE EXCEPTION 'signup grant was not posted';
    END IF;
    SELECT id INTO v_plan FROM bursar.catalog_plans WHERE plan_key = 'pro';
    IF NOT bursar.assign_plan(v_subject, v_plan, now(), NULL) THEN RAISE EXCEPTION 'plan assignment failed'; END IF;
    SELECT lease_id INTO v_lease FROM bursar.create_lease_for_operation(v_subject, 'chat', 10, 'smoke-lease');
    IF v_lease IS NULL THEN RAISE EXCEPTION 'lease failed'; END IF;
    IF (SELECT error_code FROM bursar.create_lease_for_operation(v_subject, 'chat', 1, 'smoke-lease-2')) <> 'max_concurrent_reached' THEN
        RAISE EXCEPTION 'configured concurrent lease limit failed';
    END IF;
    SELECT ledger_entry_id INTO v_entry FROM bursar.settle_lease(v_subject, v_lease, 15, 'smoke-settlement');
    IF v_entry IS NULL OR (SELECT balance FROM bursar.credit_accounts WHERE subject_id = v_subject AND account_kind = 'personal') <> -13 THEN
        RAISE EXCEPTION 'overdraft settlement failed';
    END IF;
    IF (SELECT replayed FROM bursar.post_credit(v_subject,'grant',10,'smoke-grant','smoke-grant')) IS DISTINCT FROM false THEN
        RAISE EXCEPTION 'grant failed';
    END IF;
    IF (SELECT replayed FROM bursar.post_credit(v_subject,'grant',10,'smoke-grant','smoke-grant')) IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'idempotent replay failed';
    END IF;
END $$;
