-- RLS, backend-only policies, and explicit RPC grants.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

REVOKE ALL ON SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL TABLES IN SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA bursar FROM PUBLIC;

DO $$
DECLARE
    t record;

    v_roles text;

BEGIN
    SELECT string_agg(quote_ident(rolname),',')
    INTO v_roles
    FROM pg_roles
    WHERE rolname IN ('anon','authenticated');

    FOR t IN
        SELECT tablename FROM pg_tables WHERE schemaname='bursar'
    LOOP
        EXECUTE format(
            'ALTER TABLE bursar.%I ENABLE ROW LEVEL SECURITY',
            t.tablename
        );

        IF v_roles IS NOT NULL THEN
            EXECUTE format(
                'CREATE POLICY %I ON bursar.%I FOR ALL TO %s USING (false) WITH CHECK (false)',
                'backend_only_'||t.tablename,
                t.tablename,
                v_roles
            );

        END IF;

    END LOOP;

END $$;

DO $$
DECLARE
    v_function text;

    v_service_functions constant text[]:=ARRAY[
        'bursar.publish_and_activate_catalog(integer,jsonb,text,boolean)',
        'bursar.catalog_revision_by_number(bigint)',
        'bursar.list_catalog_revisions(integer)',
        'bursar.activate_catalog_revision(bigint)',
        'bursar.provision_subject_account_on_insert()',
        'bursar.active_catalog_revision()',
        'bursar.execute_grant_program(text,text,uuid,text,uuid,text,jsonb)',
        'bursar.post_credit(uuid,bursar.ledger_entry_kind,numeric,text,text,jsonb,text,uuid,timestamptz,numeric)',
        'bursar.charge_usage_for_operation(uuid,text,numeric,text,text,text,text,jsonb,jsonb,jsonb)',
        'bursar.refund_credit_by_entry(uuid,numeric,text,text,jsonb)',
        'bursar.revoke_subject_credits_by_operation(uuid,text)',
        'bursar.deduct_team(uuid,uuid,numeric,text,text,jsonb)',
        'bursar.create_team(uuid,text,numeric)',
        'bursar.set_team_member(uuid,uuid,text,numeric)',
        'bursar.remove_team_member(uuid,uuid)',
        'bursar.list_team_members(uuid)',
        'bursar.get_team_balance(uuid)',
        'bursar.grant_billing_credit(uuid,text)',
        'bursar.post_billing_refund(uuid,uuid,bigint,text)',
        'bursar.create_lease_for_operation(uuid,text,numeric,text,interval,jsonb,text,jsonb,jsonb,numeric,integer)',
        'bursar.settle_lease(uuid,uuid,numeric,text,text,text,text,jsonb,jsonb,jsonb)',
        'bursar.renew_lease(uuid,uuid,interval)',
        'bursar.release_lease(uuid,uuid)',
        'bursar.sweep_expired_lots(integer,uuid,boolean)',
        'bursar.expire_leases(integer)',
        'bursar.revoke_lot(uuid,numeric,text)',
        'bursar.claim_billing_event(text,text,text,jsonb,integer,integer)',
        'bursar.complete_billing_event(text,text,uuid)',
        'bursar.fail_billing_event(text,text,uuid,text)',
        'bursar.record_subscription_conflict(uuid,text,text,text,text,jsonb)',
        'bursar.select_entitlement_source(uuid,uuid)',
        'bursar.mark_subscription_grace_expired(uuid,timestamptz,timestamptz)',
        'bursar.pseudonymize_financial_subject(uuid)',
        'bursar.claim_auto_recharge_attempt(uuid,text)',
        'bursar.advance_auto_recharge_attempt(uuid,bursar.recharge_attempt_status,text,text,text,jsonb)',
        'bursar.upsert_billing_customer(uuid,text,text,text)',
        'bursar.upsert_billing_subscription(uuid,text,text,text,uuid,bursar.billing_subscription_status,timestamptz,timestamptz,boolean,jsonb,timestamptz,timestamptz,timestamptz,timestamptz,timestamptz)',
        'bursar.upsert_billing_payment(uuid,text,text,bigint,bigint,character,text,bursar.billing_payment_status,timestamptz,text,jsonb)',
        'bursar.create_billing_credit_grant(uuid,uuid,uuid,numeric,integer,uuid)',
        'bursar.upsert_billing_refund(uuid,text,bigint,text,text,timestamptz,uuid,character,jsonb)',
        'bursar.upsert_auto_recharge_profile(uuid,boolean,text,uuid,integer,numeric,integer,text,integer,text,text)',
        'bursar.upsert_billing_preferences(uuid,boolean,boolean,boolean,boolean,boolean)',
        'bursar.upsert_billing_invoice(uuid,text,text,uuid,text,bigint,bigint,character,timestamptz,timestamptz,jsonb,timestamptz)',
        'bursar.upsert_billing_dispute(text,text,uuid,text,text,jsonb,timestamptz)',
        'bursar.create_checkout_intent(uuid,text,text,text,bytea,timestamptz,text,text,text)',
        'bursar.advance_checkout_intent(uuid,text,text,text)',
        'bursar.assign_plan(uuid,uuid,timestamptz,timestamptz)',
        'bursar.unassign_plan(uuid,text)',
        'bursar.start_plan_migration(uuid,uuid)',
        'bursar.migrate_plan_batch(uuid,integer)',
        'bursar.migrate_plan_users(text,bigint)',
        'bursar.open_subscription_change(uuid,uuid,timestamptz,text,text)',
        'bursar.advance_subscription_change(uuid,text,text,text)',
        'bursar.apply_due_plan_assignment_changes(integer)',
 'bursar.get_credit_bucket_balances(uuid)',
 'bursar.get_credit_state(uuid)',
 'bursar.get_credit_operation_details(uuid,uuid,text)',
 'bursar.get_credit_grant_details(uuid,uuid)',
 'bursar.get_credit_lease(uuid,uuid)',
 'bursar.get_credit_lease_pricing_context(uuid,uuid)',
 'bursar.resolve_active_plan(text)',
 'bursar.get_subject_plan(uuid)',
 'bursar.get_subject_allowance(uuid,timestamptz)',
 'bursar.get_subject_entitlements(uuid,timestamptz)',
 'bursar.get_subject_quota_state(uuid,text)',
 'bursar.list_subject_quota_events(uuid,timestamptz,integer,text)',
 'bursar.get_billing_customer(uuid,text)',
 'bursar.get_billing_customer_by_provider(text,text)',
 'bursar.get_billing_subscription_by_provider(text,text)',
 'bursar.list_billing_subscriptions(uuid)',
 'bursar.list_expired_grace_subscriptions(timestamptz,integer)',
 'bursar.get_billing_payment_by_provider(text,text)',
 'bursar.get_billing_preferences(uuid)',
 'bursar.get_auto_recharge_profile(uuid)',
 'bursar.get_auto_recharge_attempt(uuid)',
 'bursar.get_auto_recharge_attempt_by_provider(text,text)',
 'bursar.count_auto_recharge_attempts(uuid,timestamptz)',
 'bursar.resolve_catalog_offer(text,text,text)',
 'bursar.resolve_active_catalog_offer(text)',
 'bursar.get_catalog_offer_context(uuid,uuid)',
 'bursar.resolve_catalog_topup(text,text,text)',
 'bursar.get_checkout_intent(uuid,uuid)',
 'bursar.get_open_billing_subscription_change(text,text)',
 'bursar.get_billing_subscription_change(uuid)',
 'bursar.get_billing_credit_grant_by_payment(uuid)',
 'bursar.list_billing_invoices(uuid)',
        'bursar.list_ledger(uuid,timestamptz,uuid,integer,text[],timestamptz,timestamptz,boolean)',
        'bursar.get_ledger_entry(uuid,uuid)',
        'bursar.spend_by_user(timestamptz,timestamptz)',
        'bursar.spend_by_model(timestamptz,timestamptz)',
        'bursar.daily_spend(timestamptz,timestamptz)',
        'bursar.aggregate_usage_stats(timestamptz,timestamptz)'
    ];

BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
        GRANT USAGE ON SCHEMA bursar TO service_role;

        REVOKE ALL ON ALL TABLES IN SCHEMA bursar FROM service_role;

        REVOKE ALL ON ALL SEQUENCES IN SCHEMA bursar FROM service_role;

        FOREACH v_function IN ARRAY v_service_functions LOOP
            EXECUTE format(
                'GRANT EXECUTE ON FUNCTION %s TO service_role',
                v_function
            );

        END LOOP;

    END IF;

END $$;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
    REVOKE ALL ON TABLES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
    REVOKE ALL ON SEQUENCES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
    REVOKE ALL ON FUNCTIONS FROM PUBLIC;
