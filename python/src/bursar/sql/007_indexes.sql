-- Indexes supporting catalog resolution, accounting locks, worker queues, and
-- time-series reporting. Partial indexes deliberately exclude terminal rows.

CREATE UNIQUE INDEX catalog_one_active_idx
ON bursar.catalog_revisions ((status))
WHERE status = 'active';

CREATE UNIQUE INDEX catalog_one_open_activation_idx
ON bursar.catalog_activation_history ((true))
WHERE deactivated_at IS NULL;

CREATE UNIQUE INDEX catalog_one_default_bucket_idx
ON bursar.catalog_buckets(catalog_revision_id)
WHERE is_default;

CREATE INDEX catalog_activation_revision_idx
ON bursar.catalog_activation_history(catalog_revision_id, activated_at DESC);

CREATE INDEX catalog_plans_rate_card_idx
ON bursar.catalog_plans(catalog_revision_id, rate_card)
WHERE rate_card IS NOT NULL;

CREATE INDEX catalog_plans_credit_policy_idx
ON bursar.catalog_plans(catalog_revision_id, credit_policy_key)
WHERE credit_policy_key IS NOT NULL;

CREATE INDEX catalog_plans_admission_policy_idx
ON bursar.catalog_plans(catalog_revision_id, admission_policy_key)
WHERE admission_policy_key IS NOT NULL;

CREATE INDEX catalog_plans_allowance_bucket_idx
ON bursar.catalog_plans(catalog_revision_id, credit_allowance_bucket)
WHERE credit_allowance_bucket IS NOT NULL;

CREATE INDEX catalog_plan_features_feature_idx
ON bursar.catalog_plan_features(catalog_revision_id, feature_key);

CREATE INDEX catalog_plan_quotas_operation_idx
ON bursar.catalog_plan_quotas(
    catalog_revision_id,
    operation_key,
    measure_key
);

CREATE INDEX catalog_grant_programs_trigger_idx
ON bursar.catalog_grant_programs(catalog_revision_id, trigger_type);

CREATE INDEX catalog_grant_awards_program_idx
ON bursar.catalog_grant_awards(grant_program_id, award_index);

CREATE INDEX catalog_offers_plan_idx
ON bursar.catalog_offers(catalog_revision_id, plan_key);

CREATE INDEX catalog_offers_cycle_bucket_idx
ON bursar.catalog_offers(catalog_revision_id, cycle_grant_bucket_key)
WHERE cycle_grant_bucket_key IS NOT NULL;

CREATE INDEX catalog_topups_bucket_idx
ON bursar.catalog_topups(catalog_revision_id, bucket_key);

CREATE INDEX catalog_provider_refs_active_lookup_idx
ON bursar.catalog_provider_refs(
    provider,
    provider_environment,
    lookup_type,
    lookup_value,
    catalog_revision_id
);

CREATE INDEX ledger_account_created_idx
ON bursar.credit_ledger_entries(account_id, created_at DESC, id DESC);

CREATE INDEX ledger_reference_idx
ON bursar.credit_ledger_entries(reference_entry_id)
WHERE reference_entry_id IS NOT NULL;

CREATE INDEX ledger_catalog_revision_idx
ON bursar.credit_ledger_entries(catalog_revision_id)
WHERE catalog_revision_id IS NOT NULL;

CREATE INDEX lots_available_idx
ON bursar.credit_lots(
    account_id,
    priority,
    expires_at,
    created_at,
    id
)
WHERE consumed < granted;

CREATE INDEX lots_expiry_worker_idx
ON bursar.credit_lots(expires_at, id)
WHERE expires_at IS NOT NULL AND consumed < granted;

CREATE INDEX credit_lots_catalog_bucket_idx
ON bursar.credit_lots(catalog_revision_id, bucket_key);

CREATE INDEX credit_lots_source_idx
ON bursar.credit_lots(source_type, source_id)
WHERE source_id IS NOT NULL;

CREATE INDEX credit_lot_sources_lot_idx
ON bursar.credit_lot_sources(lot_id, created_at);

CREATE INDEX credit_lot_sources_business_source_idx
ON bursar.credit_lot_sources(source_type, source_id)
WHERE source_id IS NOT NULL;

CREATE INDEX lot_allocations_debit_idx
ON bursar.credit_lot_allocations(debit_entry_id);

CREATE INDEX lot_allocations_lot_idx
ON bursar.credit_lot_allocations(lot_id);

CREATE INDEX lot_source_allocations_source_idx
ON bursar.credit_lot_source_allocations(lot_source_id, created_at);

CREATE INDEX lot_restorations_allocation_idx
ON bursar.credit_lot_restorations(original_allocation_id);

CREATE INDEX lot_restorations_lot_idx
ON bursar.credit_lot_restorations(lot_id);

CREATE INDEX lot_source_restorations_source_idx
ON bursar.credit_lot_source_restorations(
    source_allocation_id,
    created_at
);

CREATE INDEX credit_unallocated_debits_account_idx
ON bursar.credit_unallocated_debits(account_id, created_at);

CREATE INDEX credit_debt_repayments_account_idx
ON bursar.credit_debt_repayments(account_id, created_at);

CREATE INDEX usage_charge_account_created_idx
ON bursar.credit_usage_charges(account_id, created_at DESC, id DESC);

CREATE INDEX usage_charge_operation_event_idx
ON bursar.credit_usage_charges(operation, event_at, id);

CREATE INDEX usage_charge_allowance_window_idx
ON bursar.credit_usage_charges(
    account_id,
    plan_id,
    catalog_revision_id,
    event_at
)
INCLUDE (allowance_covered)
WHERE allowance_covered > 0;

CREATE INDEX usage_charge_dimensions_gin_idx
ON bursar.credit_usage_charges USING gin(dimensions);

CREATE INDEX usage_charge_measures_gin_idx
ON bursar.credit_usage_charges USING gin(measures);

CREATE INDEX usage_charge_created_brin_idx
ON bursar.credit_usage_charges USING brin(created_at);

CREATE INDEX credit_usage_charges_ledger_entry_idx
ON bursar.credit_usage_charges(ledger_entry_id)
WHERE ledger_entry_id IS NOT NULL;

CREATE INDEX credit_usage_charges_correction_idx
ON bursar.credit_usage_charges(correction_of_charge_id)
WHERE correction_of_charge_id IS NOT NULL;

CREATE INDEX plan_assignments_revision_idx
ON bursar.account_plan_assignments(catalog_revision_id, plan_id);

CREATE INDEX plan_assignments_source_idx
ON bursar.account_plan_assignments(source_type, source_id)
WHERE source_id IS NOT NULL;

CREATE INDEX plan_assignment_history_account_idx
ON bursar.account_plan_assignment_history(
    account_id,
    starts_at DESC,
    ends_at DESC
);

CREATE INDEX plan_assignment_history_plan_idx
ON bursar.account_plan_assignment_history(
    catalog_revision_id,
    plan_id,
    starts_at
);

CREATE UNIQUE INDEX one_open_plan_assignment_change_idx
ON bursar.plan_assignment_changes(account_id)
WHERE state = 'scheduled';

CREATE INDEX plan_assignment_changes_due_idx
ON bursar.plan_assignment_changes(effective_at, id)
WHERE state = 'scheduled';

CREATE INDEX allowance_window_lookup_idx
ON bursar.allowance_windows(
    account_id,
    allowance_key,
    window_start,
    window_end
);

CREATE INDEX allowance_windows_plan_idx
ON bursar.allowance_windows(plan_id, catalog_revision_id);

CREATE INDEX quota_window_lookup_idx
ON bursar.quota_windows(
    account_id,
    operation_key,
    measure_key,
    window_start,
    window_end
);

CREATE INDEX quota_windows_plan_idx
ON bursar.quota_windows(plan_id, catalog_revision_id, quota_key);

CREATE INDEX quota_usage_events_rolling_idx
ON bursar.quota_usage_events(
    account_id,
    catalog_quota_id,
    event_at DESC
)
INCLUDE (amount);

CREATE INDEX quota_usage_events_charge_idx
ON bursar.quota_usage_events(usage_charge_id)
WHERE usage_charge_id IS NOT NULL;

CREATE INDEX quota_events_charge_idx
ON bursar.quota_events(usage_charge_id)
WHERE usage_charge_id IS NOT NULL;

CREATE INDEX credit_lease_quota_active_idx
ON bursar.credit_lease_quota_reservations(
    catalog_quota_id,
    window_start,
    window_end,
    lease_id
)
WHERE released_at IS NULL;

CREATE INDEX active_leases_idx
ON bursar.credit_leases(account_id, expires_at)
WHERE status = 'active';

CREATE INDEX active_operation_leases_idx
ON bursar.credit_leases(account_id, operation, expires_at)
WHERE status = 'active';

CREATE INDEX active_plan_allowance_leases_idx
ON bursar.credit_leases(
    account_id,
    plan_id,
    catalog_revision_id,
    expires_at
)
INCLUDE (reserved_allowance)
WHERE status = 'active' AND reserved_allowance > 0;

CREATE INDEX credit_leases_catalog_revision_idx
ON bursar.credit_leases(catalog_revision_id);

CREATE INDEX credit_leases_plan_idx
ON bursar.credit_leases(plan_id, catalog_revision_id)
WHERE plan_id IS NOT NULL;

CREATE INDEX credit_leases_ledger_entry_idx
ON bursar.credit_leases(ledger_entry_id)
WHERE ledger_entry_id IS NOT NULL;

CREATE INDEX credit_team_members_subject_idx
ON bursar.credit_team_members(subject_id);

CREATE INDEX credit_team_usage_member_created_idx
ON bursar.credit_team_usage_charges(
    team_id,
    subject_id,
    created_at DESC,
    id DESC
);

CREATE INDEX grant_program_events_subject_idx
ON bursar.grant_program_events(subject_id, occurred_at DESC);

CREATE INDEX grant_program_events_referrer_idx
ON bursar.grant_program_events(referrer_subject_id)
WHERE referrer_subject_id IS NOT NULL;

CREATE INDEX grant_award_executions_event_idx
ON bursar.grant_award_executions(grant_event_id);

CREATE INDEX account_creation_grants_revision_idx
ON bursar.account_creation_grants(catalog_revision_id);

CREATE INDEX credit_plan_migrations_from_plan_idx
ON bursar.credit_plan_migrations(from_plan_id)
WHERE from_plan_id IS NOT NULL;

CREATE INDEX credit_plan_migrations_to_plan_idx
ON bursar.credit_plan_migrations(to_plan_id);

CREATE INDEX external_identities_subject_idx
ON bursar.external_identities(subject_id);

CREATE INDEX billing_subscriptions_subject_idx
ON bursar.billing_subscriptions(subject_id, created_at DESC);

CREATE INDEX billing_subscriptions_offer_idx
ON bursar.billing_subscriptions(offer_id, catalog_revision_id);

CREATE INDEX billing_subscriptions_period_end_idx
ON bursar.billing_subscriptions(current_period_end, id)
WHERE status IN ('trialing', 'active', 'past_due', 'paused');

CREATE INDEX billing_subscriptions_expired_grace_idx
ON bursar.billing_subscriptions(grace_ends_at, id)
WHERE status = 'past_due'
  AND grace_ends_at IS NOT NULL
  AND grace_expired_at IS NULL;

CREATE UNIQUE INDEX one_selected_entitlement_idx
ON bursar.billing_entitlement_sources(subject_id)
WHERE selected;

CREATE INDEX billing_entitlement_sources_subscription_idx
ON bursar.billing_entitlement_sources(subscription_id, subject_id);

CREATE INDEX billing_payments_subject_idx
ON bursar.billing_payments(subject_id, created_at DESC);

CREATE INDEX billing_payments_provider_invoice_idx
ON bursar.billing_payments(provider, provider_environment, provider_invoice_id)
WHERE provider_invoice_id IS NOT NULL;

CREATE INDEX billing_events_claimable_idx
ON bursar.billing_events(claim_expires_at, created_at, id)
WHERE status IN ('processing', 'failed');

CREATE INDEX billing_subscription_conflicts_subject_idx
ON bursar.billing_subscription_conflicts(subject_id, created_at DESC)
WHERE subject_id IS NOT NULL;

CREATE INDEX billing_credit_grants_subject_idx
ON bursar.billing_credit_grants(subject_id, created_at DESC);

CREATE INDEX billing_credit_grants_payment_idx
ON bursar.billing_credit_grants(payment_id, subject_id)
WHERE payment_id IS NOT NULL;

CREATE INDEX billing_credit_grants_subscription_idx
ON bursar.billing_credit_grants(subscription_id, subject_id)
WHERE subscription_id IS NOT NULL;

CREATE INDEX billing_credit_grants_topup_idx
ON bursar.billing_credit_grants(topup_id, catalog_revision_id)
WHERE topup_id IS NOT NULL;

CREATE INDEX billing_credit_grants_ledger_entry_idx
ON bursar.billing_credit_grants(ledger_entry_id)
WHERE ledger_entry_id IS NOT NULL;

CREATE INDEX billing_credit_grants_event_idx
ON bursar.billing_credit_grants(billing_event_id)
WHERE billing_event_id IS NOT NULL;

CREATE INDEX billing_refunds_payment_idx
ON bursar.billing_refunds(payment_id);

CREATE INDEX billing_refund_grants_grant_idx
ON bursar.billing_refund_grants(grant_id);

CREATE INDEX billing_refund_grants_ledger_entry_idx
ON bursar.billing_refund_grants(ledger_entry_id)
WHERE ledger_entry_id IS NOT NULL;

CREATE UNIQUE INDEX open_subscription_changes_idx
ON bursar.billing_subscription_changes(subscription_id)
WHERE state IN ('awaiting_payment', 'scheduled');

CREATE INDEX billing_subscription_changes_from_offer_idx
ON bursar.billing_subscription_changes(
    from_offer_id,
    from_catalog_revision_id
);

CREATE INDEX billing_subscription_changes_to_offer_idx
ON bursar.billing_subscription_changes(to_offer_id, to_catalog_revision_id);

CREATE INDEX billing_auto_recharge_profiles_topup_idx
ON bursar.billing_auto_recharge_profiles(topup_id, catalog_revision_id)
WHERE topup_id IS NOT NULL;

CREATE INDEX active_recharge_attempts_idx
ON bursar.billing_auto_recharge_attempts(subject_id, created_at DESC)
WHERE state IN (
    'claimed', 'submitted', 'processing', 'unknown', 'action_required'
);

CREATE INDEX billing_auto_recharge_attempts_topup_idx
ON bursar.billing_auto_recharge_attempts(topup_id, catalog_revision_id);

CREATE INDEX billing_invoices_subject_idx
ON bursar.billing_invoices(subject_id, created_at DESC);

CREATE INDEX billing_invoices_subscription_idx
ON bursar.billing_invoices(subscription_id)
WHERE subscription_id IS NOT NULL;

CREATE INDEX billing_disputes_subject_idx
ON bursar.billing_disputes(subject_id)
WHERE subject_id IS NOT NULL;

CREATE INDEX billing_disputes_payment_idx
ON bursar.billing_disputes(payment_id)
WHERE payment_id IS NOT NULL;
