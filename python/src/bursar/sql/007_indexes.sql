-- Indexes supporting catalog resolution, accounting locks, worker queues, and
-- time-series reporting. Partial indexes deliberately exclude terminal rows.

CREATE UNIQUE INDEX catalog_one_active_idx
ON bursar.catalog_revisions (tenant_id)
WHERE status = 'active';

CREATE UNIQUE INDEX catalog_one_open_activation_idx
ON bursar.catalog_activation_history (tenant_id)
WHERE deactivated_at IS null;

CREATE UNIQUE INDEX catalog_one_default_bucket_idx
ON bursar.catalog_buckets (catalog_revision_id)
WHERE is_default;

CREATE INDEX catalog_activation_revision_idx
ON bursar.catalog_activation_history (catalog_revision_id, activated_at DESC);

CREATE INDEX catalog_plans_rate_card_idx
ON bursar.catalog_plans (catalog_revision_id, rate_card)
WHERE rate_card IS NOT null;

CREATE INDEX catalog_plans_credit_policy_idx
ON bursar.catalog_plans (catalog_revision_id, credit_policy_key)
WHERE credit_policy_key IS NOT null;

CREATE INDEX catalog_plans_admission_policy_idx
ON bursar.catalog_plans (catalog_revision_id, admission_policy_key)
WHERE admission_policy_key IS NOT null;

CREATE INDEX catalog_plans_allowance_bucket_idx
ON bursar.catalog_plans (catalog_revision_id, credit_allowance_bucket)
WHERE credit_allowance_bucket IS NOT null;

CREATE INDEX catalog_plan_features_feature_idx
ON bursar.catalog_plan_features (catalog_revision_id, feature_key);

CREATE INDEX catalog_plan_quotas_operation_idx
ON bursar.catalog_plan_quotas (
    catalog_revision_id,
    operation_key,
    measure_key
);

CREATE INDEX catalog_grant_programs_trigger_idx
ON bursar.catalog_grant_programs (catalog_revision_id, trigger_type);

CREATE INDEX catalog_offers_plan_idx
ON bursar.catalog_offers (catalog_revision_id, plan_key);

CREATE INDEX catalog_offers_cycle_bucket_idx
ON bursar.catalog_offers (catalog_revision_id, cycle_grant_bucket_key)
WHERE cycle_grant_bucket_key IS NOT null;

CREATE INDEX catalog_topups_bucket_idx
ON bursar.catalog_topups (catalog_revision_id, bucket_key);

CREATE INDEX catalog_provider_refs_active_lookup_idx
ON bursar.catalog_provider_refs (
    provider,
    provider_environment,
    lookup_type,
    lookup_value,
    catalog_revision_id
);

CREATE INDEX ledger_account_created_idx
ON bursar.credit_ledger_entries (account_id, created_at DESC, id DESC);

CREATE INDEX ledger_reference_idx
ON bursar.credit_ledger_entries (reference_entry_id)
WHERE reference_entry_id IS NOT null;

CREATE INDEX ledger_catalog_revision_idx
ON bursar.credit_ledger_entries (catalog_revision_id)
WHERE catalog_revision_id IS NOT null;

CREATE INDEX lots_available_idx
ON bursar.credit_lots (
    account_id,
    priority,
    expires_at,
    created_at,
    id
)
WHERE consumed < granted;

CREATE INDEX lots_expiry_worker_idx
ON bursar.credit_lots (expires_at, id)
WHERE expires_at IS NOT null AND consumed < granted;

CREATE INDEX credit_lots_catalog_bucket_idx
ON bursar.credit_lots (catalog_revision_id, bucket_key);

CREATE INDEX credit_lots_source_idx
ON bursar.credit_lots (source_type, source_id)
WHERE source_id IS NOT null;

CREATE INDEX credit_lot_sources_lot_idx
ON bursar.credit_lot_sources (lot_id, created_at);

CREATE INDEX credit_lot_sources_business_source_idx
ON bursar.credit_lot_sources (source_type, source_id)
WHERE source_id IS NOT null;

CREATE INDEX lot_allocations_lot_idx
ON bursar.credit_lot_allocations (lot_id);

CREATE INDEX lot_source_allocations_source_idx
ON bursar.credit_lot_source_allocations (lot_source_id, created_at);

CREATE INDEX lot_restorations_allocation_idx
ON bursar.credit_lot_restorations (original_allocation_id);

CREATE INDEX lot_restorations_lot_idx
ON bursar.credit_lot_restorations (lot_id);

CREATE INDEX lot_source_restorations_source_idx
ON bursar.credit_lot_source_restorations (
    source_allocation_id,
    created_at
);

CREATE INDEX credit_unallocated_debits_account_idx
ON bursar.credit_unallocated_debits (account_id, created_at);

CREATE INDEX credit_debt_repayments_account_idx
ON bursar.credit_debt_repayments (account_id, created_at);

CREATE INDEX usage_charge_account_created_idx
ON bursar.credit_usage_charges (account_id, created_at DESC, id DESC);

CREATE INDEX usage_charge_account_event_idx
ON bursar.credit_usage_charges (account_id, event_at DESC, id DESC);

CREATE INDEX usage_charge_account_billable_event_idx
ON bursar.credit_usage_charges (account_id, event_at DESC, id DESC)
WHERE billing_disposition = 'billable';

CREATE INDEX usage_charge_operation_event_idx
ON bursar.credit_usage_charges (operation, event_at, id);

CREATE INDEX usage_charge_allowance_window_idx
ON bursar.credit_usage_charges (
    account_id,
    plan_id,
    catalog_revision_id,
    event_at
)
INCLUDE (allowance_covered)
WHERE allowance_covered > 0;

CREATE INDEX usage_charge_event_brin_idx
ON bursar.credit_usage_charges USING brin (event_at);

CREATE INDEX credit_usage_charges_record_only_retention_idx
ON bursar.credit_usage_charges (event_at, id)
WHERE billing_disposition = 'record_only';

CREATE INDEX usage_charge_payload_charge_idx
ON bursar.usage_charge_payloads (charge_id, event_at);

CREATE INDEX usage_rollup_account_day_idx
ON bursar.usage_daily_rollups (account_id, usage_day);

CREATE INDEX usage_rollup_operation_day_idx
ON bursar.usage_daily_rollups (operation, usage_day);

CREATE INDEX usage_rollup_model_day_idx
ON bursar.usage_daily_rollups (model_key, usage_day);

CREATE INDEX event_outbox_claimable_idx
ON bursar.event_outbox (
    (
        CASE status
            WHEN 'pending' THEN available_at
            WHEN 'processing' THEN claim_expires_at
        END
    ),
    created_at,
    id
)
WHERE status IN ('pending', 'processing');

CREATE INDEX event_outbox_topic_claimable_idx
ON bursar.event_outbox (
    topic,
    (
        CASE status
            WHEN 'pending' THEN available_at
            WHEN 'processing' THEN claim_expires_at
        END
    ),
    created_at,
    id
)
WHERE status IN ('pending', 'processing');

CREATE INDEX event_outbox_aggregate_idx
ON bursar.event_outbox (aggregate_type, aggregate_id);

CREATE INDEX event_outbox_retention_idx
ON bursar.event_outbox (created_at, id);

CREATE INDEX event_outbox_delivered_retention_idx
ON bursar.event_outbox (delivered_at, id)
WHERE status = 'delivered';

CREATE INDEX credit_usage_charges_ledger_entry_idx
ON bursar.credit_usage_charges (ledger_entry_id)
WHERE ledger_entry_id IS NOT null;

CREATE INDEX plan_assignments_revision_idx
ON bursar.account_plan_assignments (catalog_revision_id, plan_id);

CREATE INDEX plan_assignments_source_idx
ON bursar.account_plan_assignments (source_type, source_id)
WHERE source_id IS NOT null;

CREATE INDEX plan_assignment_history_account_idx
ON bursar.account_plan_assignment_history (
    account_id,
    starts_at DESC,
    ends_at DESC
);

CREATE INDEX plan_assignment_history_plan_idx
ON bursar.account_plan_assignment_history (
    catalog_revision_id,
    plan_id,
    starts_at
);

CREATE UNIQUE INDEX one_open_plan_assignment_change_idx
ON bursar.plan_assignment_changes (account_id, change_kind)
WHERE state = 'scheduled';

CREATE INDEX plan_assignment_changes_due_idx
ON bursar.plan_assignment_changes (effective_at, id)
WHERE state = 'scheduled';

CREATE INDEX allowance_window_lookup_idx
ON bursar.allowance_windows (
    account_id,
    allowance_key,
    window_start,
    window_end
);

CREATE INDEX allowance_windows_plan_idx
ON bursar.allowance_windows (plan_id, catalog_revision_id);

CREATE INDEX quota_window_lookup_idx
ON bursar.quota_windows (
    account_id,
    operation_key,
    measure_key,
    window_start,
    window_end
);

CREATE INDEX quota_windows_plan_idx
ON bursar.quota_windows (plan_id, catalog_revision_id, quota_key);

CREATE INDEX quota_usage_events_rolling_idx
ON bursar.quota_usage_events (
    account_id,
    catalog_quota_id,
    event_at DESC
)
INCLUDE (amount);

CREATE INDEX quota_usage_events_lineage_idx
ON bursar.quota_usage_events (
    account_id,
    quota_key,
    operation_key,
    measure_key,
    event_at DESC
)
INCLUDE (
    plan_id,
    catalog_revision_id,
    amount,
    correction_of_event_id
);

CREATE INDEX quota_usage_events_retention_idx
ON bursar.quota_usage_events (event_at, id);

CREATE INDEX quota_usage_events_correction_idx
ON bursar.quota_usage_events (correction_of_event_id)
WHERE correction_of_event_id IS NOT null;

CREATE INDEX quota_usage_events_charge_idx
ON bursar.quota_usage_events (usage_charge_id)
WHERE usage_charge_id IS NOT null;

CREATE INDEX quota_events_charge_idx
ON bursar.quota_events (usage_charge_id)
WHERE usage_charge_id IS NOT null;

CREATE INDEX quota_events_retention_idx
ON bursar.quota_events (created_at, id);

CREATE INDEX credit_lease_quota_active_idx
ON bursar.credit_lease_quota_reservations (
    catalog_quota_id,
    window_start,
    window_end,
    lease_id
)
WHERE released_at IS null;

CREATE INDEX active_leases_idx
ON bursar.credit_leases (account_id, expires_at)
WHERE status = 'active';

CREATE INDEX active_operation_leases_idx
ON bursar.credit_leases (account_id, operation, expires_at)
WHERE status = 'active';

CREATE INDEX terminal_lease_payload_retention_idx
ON bursar.credit_leases (updated_at, id)
WHERE status IN ('settled', 'released', 'expired')
AND (
    dimensions <> '{}'::jsonb
    OR metadata <> '{}'::jsonb
);

CREATE INDEX active_plan_allowance_leases_idx
ON bursar.credit_leases (
    account_id,
    plan_id,
    catalog_revision_id,
    expires_at
)
INCLUDE (reserved_allowance)
WHERE status = 'active' AND reserved_allowance > 0;

CREATE INDEX credit_leases_catalog_revision_idx
ON bursar.credit_leases (catalog_revision_id);

CREATE INDEX credit_leases_plan_idx
ON bursar.credit_leases (plan_id, catalog_revision_id)
WHERE plan_id IS NOT null;

CREATE INDEX credit_leases_ledger_entry_idx
ON bursar.credit_leases (ledger_entry_id)
WHERE ledger_entry_id IS NOT null;

CREATE INDEX credit_team_members_subject_idx
ON bursar.credit_team_members (subject_id);

CREATE INDEX credit_team_usage_member_created_idx
ON bursar.credit_team_usage_charges (
    team_id,
    subject_id,
    created_at DESC,
    id DESC
);

CREATE INDEX grant_program_events_subject_idx
ON bursar.grant_program_events (subject_id, occurred_at DESC);

CREATE INDEX grant_program_events_referrer_idx
ON bursar.grant_program_events (referrer_subject_id)
WHERE referrer_subject_id IS NOT null;

CREATE INDEX credit_plan_migrations_from_plan_idx
ON bursar.credit_plan_migrations (from_plan_id)
WHERE from_plan_id IS NOT null;

CREATE INDEX credit_plan_migrations_to_plan_idx
ON bursar.credit_plan_migrations (to_plan_id);

CREATE INDEX external_identities_subject_idx
ON bursar.external_identities (subject_id);

CREATE INDEX billing_subscriptions_subject_idx
ON bursar.billing_subscriptions (subject_id, created_at DESC);

CREATE INDEX billing_subscriptions_offer_idx
ON bursar.billing_subscriptions (offer_id, catalog_revision_id);

CREATE INDEX billing_subscriptions_period_end_idx
ON bursar.billing_subscriptions (current_period_end, id)
WHERE status IN ('trialing', 'active', 'past_due', 'paused');

CREATE INDEX billing_subscriptions_expired_grace_idx
ON bursar.billing_subscriptions (grace_ends_at, id)
WHERE status = 'past_due'
AND grace_ends_at IS NOT null
AND grace_expired_at IS null;

CREATE UNIQUE INDEX one_selected_entitlement_idx
ON bursar.billing_entitlement_sources (
    tenant_id,
    subject_id,
    provider_environment
)
WHERE selected;

CREATE INDEX billing_entitlement_sources_subscription_idx
ON bursar.billing_entitlement_sources (
    subscription_id,
    subject_id,
    provider_environment
);

CREATE INDEX billing_payments_subject_idx
ON bursar.billing_payments (subject_id, created_at DESC);

CREATE INDEX billing_payments_provider_invoice_idx
ON bursar.billing_payments (provider, provider_environment, provider_invoice_id)
WHERE provider_invoice_id IS NOT null;

CREATE INDEX billing_events_claimable_idx
ON bursar.billing_events (claim_expires_at, created_at, id)
WHERE status IN ('processing', 'failed');

CREATE INDEX billing_event_payload_event_idx
ON bursar.billing_event_payloads (event_id, received_at);

CREATE INDEX billing_subscription_conflicts_subject_idx
ON bursar.billing_subscription_conflicts (subject_id, created_at DESC)
WHERE subject_id IS NOT null;

CREATE INDEX billing_credit_grants_subject_idx
ON bursar.billing_credit_grants (subject_id, created_at DESC);

CREATE INDEX billing_credit_grants_payment_idx
ON bursar.billing_credit_grants (payment_id, subject_id)
WHERE payment_id IS NOT null;

CREATE INDEX billing_credit_grants_subscription_idx
ON bursar.billing_credit_grants (subscription_id, subject_id)
WHERE subscription_id IS NOT null;

CREATE INDEX billing_credit_grants_topup_idx
ON bursar.billing_credit_grants (topup_id, catalog_revision_id)
WHERE topup_id IS NOT null;

CREATE INDEX billing_credit_grants_ledger_entry_idx
ON bursar.billing_credit_grants (ledger_entry_id)
WHERE ledger_entry_id IS NOT null;

CREATE INDEX billing_credit_grants_event_idx
ON bursar.billing_credit_grants (billing_event_id)
WHERE billing_event_id IS NOT null;

CREATE INDEX billing_refunds_payment_idx
ON bursar.billing_refunds (payment_id);

CREATE INDEX billing_refund_grants_grant_idx
ON bursar.billing_refund_grants (grant_id);

CREATE INDEX billing_refund_grants_ledger_entry_idx
ON bursar.billing_refund_grants (ledger_entry_id)
WHERE ledger_entry_id IS NOT null;

CREATE UNIQUE INDEX open_subscription_changes_idx
ON bursar.billing_subscription_changes (subscription_id)
WHERE state IN ('awaiting_payment', 'scheduled');

CREATE INDEX billing_subscription_changes_from_offer_idx
ON bursar.billing_subscription_changes (
    from_offer_id,
    from_catalog_revision_id
);

CREATE INDEX billing_subscription_changes_to_offer_idx
ON bursar.billing_subscription_changes (to_offer_id, to_catalog_revision_id);

CREATE INDEX billing_auto_recharge_profiles_topup_idx
ON bursar.billing_auto_recharge_profiles (topup_id, catalog_revision_id)
WHERE topup_id IS NOT null;

CREATE INDEX billing_auto_recharge_attempt_count_idx
ON bursar.billing_auto_recharge_attempts (
    subject_id,
    provider_environment,
    created_at
)
WHERE state IN (
    'submitted', 'processing', 'succeeded', 'action_required'
);

CREATE INDEX billing_auto_recharge_attempts_topup_idx
ON bursar.billing_auto_recharge_attempts (topup_id, catalog_revision_id);

CREATE INDEX billing_invoices_subject_idx
ON bursar.billing_invoices (
    subject_id,
    provider_environment,
    (COALESCE(period_end, created_at)) DESC,
    id DESC
);

CREATE INDEX billing_invoices_subscription_idx
ON bursar.billing_invoices (subscription_id)
WHERE subscription_id IS NOT null;

CREATE INDEX billing_disputes_subject_idx
ON bursar.billing_disputes (subject_id)
WHERE subject_id IS NOT null;

CREATE INDEX billing_disputes_payment_idx
ON bursar.billing_disputes (payment_id)
WHERE payment_id IS NOT null;

-- Every foreign key needs a leading-column index on the referencing side.
-- PostgreSQL does not create these automatically, and parent deletes or key
-- updates otherwise require full scans of the child relation.
CREATE INDEX billing_auto_recharge_profiles_revision_fk_idx
ON bursar.billing_auto_recharge_profiles (catalog_revision_id)
WHERE catalog_revision_id IS NOT null;

CREATE INDEX billing_checkout_intents_revision_fk_idx
ON bursar.billing_checkout_intents (catalog_revision_id);

CREATE INDEX billing_subscription_conflicts_event_fk_idx
ON bursar.billing_subscription_conflicts (billing_event_id)
WHERE billing_event_id IS NOT null;

CREATE INDEX billing_subscription_conflicts_existing_fk_idx
ON bursar.billing_subscription_conflicts (existing_subscription_id)
WHERE existing_subscription_id IS NOT null;

CREATE INDEX catalog_admission_operation_policy_fk_idx
ON bursar.catalog_admission_operation_policies (
    catalog_revision_id,
    operation_key
);

CREATE INDEX catalog_auto_recharge_default_topup_fk_idx
ON bursar.catalog_auto_recharge_policies (
    catalog_revision_id,
    default_topup_key
);

CREATE INDEX catalog_grant_awards_bucket_fk_idx
ON bursar.catalog_grant_awards (catalog_revision_id, bucket_key);

CREATE INDEX catalog_grant_awards_program_revision_fk_idx
ON bursar.catalog_grant_awards (grant_program_id, catalog_revision_id);

CREATE INDEX catalog_rate_cards_extends_fk_idx
ON bursar.catalog_rate_cards (catalog_revision_id, extends_key)
WHERE extends_key IS NOT null;

CREATE INDEX credit_lease_quota_window_fk_idx
ON bursar.credit_lease_quota_reservations (quota_window_id);

CREATE INDEX credit_leases_allowance_window_fk_idx
ON bursar.credit_leases (allowance_window_id)
WHERE allowance_window_id IS NOT null;

CREATE INDEX credit_team_usage_subject_fk_idx
ON bursar.credit_team_usage_charges (subject_id);

CREATE INDEX credit_usage_charges_revision_fk_idx
ON bursar.credit_usage_charges (catalog_revision_id)
WHERE catalog_revision_id IS NOT null;

CREATE INDEX credit_usage_charges_plan_revision_fk_idx
ON bursar.credit_usage_charges (plan_id, catalog_revision_id)
WHERE plan_id IS NOT null;

CREATE INDEX grant_award_executions_award_revision_fk_idx
ON bursar.grant_award_executions (
    catalog_grant_award_id,
    catalog_revision_id
);

CREATE INDEX grant_award_executions_recipient_fk_idx
ON bursar.grant_award_executions (recipient_subject_id);

CREATE INDEX grant_program_events_program_revision_fk_idx
ON bursar.grant_program_events (grant_program_id, catalog_revision_id);

CREATE INDEX plan_assignment_changes_from_plan_fk_idx
ON bursar.plan_assignment_changes (from_plan_id);

CREATE INDEX plan_assignment_changes_to_plan_fk_idx
ON bursar.plan_assignment_changes (to_plan_id);

CREATE INDEX quota_usage_events_catalog_quota_fk_idx
ON bursar.quota_usage_events (catalog_quota_id);

CREATE INDEX quota_usage_events_plan_revision_fk_idx
ON bursar.quota_usage_events (plan_id, catalog_revision_id);

-- Add one tenant-leading index wherever a composite relationship or unique
-- key did not already create one. This keeps RLS from degrading into scans.
DO $$
DECLARE
    v_table record;
    v_tenant_attnum smallint;
BEGIN
    FOR v_table IN
        SELECT table_info.oid, table_info.relname
        FROM pg_class AS table_info
        JOIN pg_namespace AS namespace_info
          ON namespace_info.oid = table_info.relnamespace
        WHERE namespace_info.nspname = 'bursar'
          AND table_info.relkind IN ('r', 'p')
          AND NOT table_info.relispartition
          AND EXISTS (
              SELECT 1
              FROM pg_attribute AS attribute_info
              WHERE attribute_info.attrelid = table_info.oid
                AND attribute_info.attname = 'tenant_id'
                AND NOT attribute_info.attisdropped
          )
        ORDER BY table_info.relname
    LOOP
        SELECT attribute_info.attnum
        INTO v_tenant_attnum
        FROM pg_attribute AS attribute_info
        WHERE attribute_info.attrelid = v_table.oid
          AND attribute_info.attname = 'tenant_id'
          AND NOT attribute_info.attisdropped;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_index AS index_info
            WHERE index_info.indrelid = v_table.oid
              AND index_info.indisvalid
              AND index_info.indkey[0] = v_tenant_attnum
        ) THEN
            EXECUTE format(
                'CREATE INDEX %I ON bursar.%I (tenant_id)',
                left(v_table.relname || '_tenant_id_idx', 63),
                v_table.relname
            );
        END IF;
    END LOOP;
END
$$;

-- Composite foreign-key indexes are created before the business uniqueness
-- changes above. Remove any non-unique index whose key and predicate are now
-- fully covered by another valid index.
DO $$
DECLARE
    v_index record;
BEGIN
    FOR v_index IN
        SELECT DISTINCT smaller_class.relname AS index_name
        FROM pg_index AS smaller
        JOIN pg_class AS smaller_class
          ON smaller_class.oid = smaller.indexrelid
        JOIN pg_index AS covering
          ON covering.indrelid = smaller.indrelid
         AND covering.indexrelid <> smaller.indexrelid
        JOIN pg_class AS covering_class
          ON covering_class.oid = covering.indexrelid
        WHERE smaller_class.relnamespace = 'bursar'::regnamespace
          AND NOT smaller.indisunique
          AND smaller.indisvalid
          AND covering.indisvalid
          AND smaller_class.relam = covering_class.relam
          AND smaller.indnkeyatts <= covering.indnkeyatts
          AND (
              regexp_split_to_array(trim(smaller.indkey::text), ' +')
          )[1:smaller.indnkeyatts]
              = (
                  regexp_split_to_array(
                      trim(covering.indkey::text),
                      ' +'
                  )
              )[1:smaller.indnkeyatts]
          AND pg_get_expr(smaller.indpred, smaller.indrelid)
              IS NOT DISTINCT FROM
              pg_get_expr(covering.indpred, covering.indrelid)
          AND pg_get_expr(smaller.indexprs, smaller.indrelid)
              IS NOT DISTINCT FROM
              pg_get_expr(covering.indexprs, covering.indrelid)
        ORDER BY smaller_class.relname
    LOOP
        EXECUTE format(
            'DROP INDEX bursar.%I',
            v_index.index_name
        );
    END LOOP;
END
$$;
