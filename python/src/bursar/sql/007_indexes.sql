-- Migration: 007_indexes.sql
-- Purpose: Add business uniqueness, query-path, worker, retention, foreign-key,
--   and tenant-leading indexes across catalog, accounting, and billing tables.
-- Depends on: 006_triggers.sql.
-- Security: Keeps tenant filters indexable and enforces singleton active states.

-- Contents
--   1. Catalog activation and resolution
--   2. Ledger, lots, usage, and outbox
--   3. Plan, quota, and lease operations
--   4. Team, grant, migration, and identity lookups
--   5. Billing lifecycle and worker lookups
--   6. Referencing-side foreign-key support

-- 1. Catalog activation and resolution

-- Enforce one active revision, open activation interval, and default bucket per
-- scope; each partial predicate indexes only the state that must be singleton.

CREATE UNIQUE INDEX catalog_one_active_idx
ON bursar.catalog_revisions (tenant_id)
WHERE status = 'active';

CREATE UNIQUE INDEX catalog_one_open_activation_idx
ON bursar.catalog_activation_history (tenant_id)
WHERE deactivated_at IS null;

CREATE UNIQUE INDEX catalog_one_default_bucket_idx
ON bursar.catalog_buckets (catalog_revision_id)
WHERE is_default;

-- Resolve a revision's activation history in newest-first order.
CREATE INDEX catalog_activation_revision_idx
ON bursar.catalog_activation_history (catalog_revision_id, activated_at DESC);

-- Support revision-local reverse lookups from pricing/admission/allowance
-- projections to plans; nullable optional references stay out of the indexes.
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

-- Resolve feature and quota consumers by the same revision-local keys used by
-- catalog validation and relationship checks.
CREATE INDEX catalog_plan_features_feature_idx
ON bursar.catalog_plan_features (catalog_revision_id, feature_key);

CREATE INDEX catalog_plan_quotas_operation_idx
ON bursar.catalog_plan_quotas (
    catalog_revision_id,
    operation_key,
    measure_key
);

-- Serve catalog discovery by trigger, plan, and bucket without scanning all
-- projections; optional cycle-grant buckets are indexed only when configured.
CREATE INDEX catalog_grant_programs_trigger_idx
ON bursar.catalog_grant_programs (catalog_revision_id, trigger_type);

CREATE INDEX catalog_offers_plan_idx
ON bursar.catalog_offers (catalog_revision_id, plan_key);

CREATE INDEX catalog_offers_cycle_bucket_idx
ON bursar.catalog_offers (catalog_revision_id, cycle_grant_bucket_key)
WHERE cycle_grant_bucket_key IS NOT null;

CREATE INDEX catalog_topups_bucket_idx
ON bursar.catalog_topups (catalog_revision_id, bucket_key);

-- Match provider objects by provider/environment/type/value and retain revision
-- as the final key for selecting the relevant catalog mapping.
CREATE INDEX catalog_provider_refs_active_lookup_idx
ON bursar.catalog_provider_refs (
    provider,
    provider_environment,
    lookup_type,
    lookup_value,
    catalog_revision_id
);

-- 2. Ledger, lots, usage, and outbox

-- Read an account ledger newest-first and follow optional correction/reference
-- or catalog lineage without indexing rows where those links are absent.
CREATE INDEX ledger_account_created_idx
ON bursar.credit_ledger_entries (account_id, created_at DESC, id DESC);

CREATE INDEX ledger_reference_idx
ON bursar.credit_ledger_entries (reference_entry_id)
WHERE reference_entry_id IS NOT null;

CREATE INDEX ledger_catalog_revision_idx
ON bursar.credit_ledger_entries (catalog_revision_id)
WHERE catalog_revision_id IS NOT null;

-- Order non-exhausted lots for spend selection; callers apply expiry eligibility,
-- while the worker index narrows further to non-exhausted rows with an expiry.
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

-- Support bucket/source reconciliation and every reverse provenance traversal
-- through lot sources, allocations, and restorations.
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

-- Reconstruct outstanding debt and repayments per account in creation order.
CREATE INDEX credit_unallocated_debits_account_idx
ON bursar.credit_unallocated_debits (account_id, created_at);

CREATE INDEX credit_debt_repayments_account_idx
ON bursar.credit_debt_repayments (account_id, created_at);

-- Serve account audit/history by ingestion or event time, operation reporting,
-- and a billable-only path that excludes record-only telemetry.
CREATE INDEX usage_charge_account_created_idx
ON bursar.credit_usage_charges (account_id, created_at DESC, id DESC);

CREATE INDEX usage_charge_account_event_idx
ON bursar.credit_usage_charges (account_id, event_at DESC, id DESC);

CREATE INDEX usage_charge_account_billable_event_idx
ON bursar.credit_usage_charges (account_id, event_at DESC, id DESC)
WHERE billing_disposition = 'billable';

CREATE INDEX usage_charge_operation_event_idx
ON bursar.credit_usage_charges (operation, event_at, id);

-- Cover allowance reconciliation only for charges that consumed allowance,
-- keeping the partial index small and allowing index-only plans when visible.
CREATE INDEX usage_charge_allowance_window_idx
ON bursar.credit_usage_charges (
    account_id,
    plan_id,
    catalog_revision_id,
    event_at
)
INCLUDE (allowance_covered)
WHERE allowance_covered > 0;

-- Use BRIN for broad time-range scans on naturally time-correlated usage, and a
-- narrow B-tree for retention of record-only facts selected by exact predicate.
CREATE INDEX usage_charge_event_brin_idx
ON bursar.credit_usage_charges USING brin (event_at);

CREATE INDEX credit_usage_charges_record_only_retention_idx
ON bursar.credit_usage_charges (event_at, id)
WHERE billing_disposition = 'record_only';

-- Join partitioned payloads back to charge facts and support daily rollup slices
-- by their account, operation, or model reporting dimensions.
CREATE INDEX usage_charge_payload_charge_idx
ON bursar.usage_charge_payloads (charge_id, event_at);

CREATE INDEX usage_rollup_account_day_idx
ON bursar.usage_daily_rollups (account_id, usage_day);

CREATE INDEX usage_rollup_operation_day_idx
ON bursar.usage_daily_rollups (operation, usage_day);

CREATE INDEX usage_rollup_model_day_idx
ON bursar.usage_daily_rollups (model_key, usage_day);

-- Order global and topic workers by the next actionable instant: availability
-- for pending rows or lease expiry for processing rows; terminal states are omitted.
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

-- Support aggregate tracing plus general and delivered-only retention scans; the
-- delivered predicate matches the cleanup job's terminal-state selection.
CREATE INDEX event_outbox_aggregate_idx
ON bursar.event_outbox (aggregate_type, aggregate_id);

CREATE INDEX event_outbox_retention_idx
ON bursar.event_outbox (created_at, id);

CREATE INDEX event_outbox_delivered_retention_idx
ON bursar.event_outbox (delivered_at, id)
WHERE status = 'delivered';

-- Mirror claim, status, and dead-letter investigation paths inside one tenant so
-- tenant-scoped workers and support queries avoid global scans.
CREATE INDEX event_outbox_tenant_claimable_idx
ON bursar.event_outbox (
    tenant_id,
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

CREATE INDEX event_outbox_tenant_status_idx
ON bursar.event_outbox (tenant_id, status);

CREATE INDEX event_outbox_tenant_dead_letter_idx
ON bursar.event_outbox (tenant_id, created_at, id)
WHERE status = 'dead_letter';

-- Follow the optional financial ledger entry for charged usage without indexing
-- record-only or allowance-only facts whose link is NULL.
CREATE INDEX credit_usage_charges_ledger_entry_idx
ON bursar.credit_usage_charges (ledger_entry_id)
WHERE ledger_entry_id IS NOT null;

-- 3. Plan, quota, and lease operations

-- Resolve current assignments by revision or business source and read archived
-- assignment intervals newest-first by account or plan.
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

-- Permit one scheduled change per account/kind and feed the due worker directly
-- by effective time; terminal change history does not occupy either index.
CREATE UNIQUE INDEX one_open_plan_assignment_change_idx
ON bursar.plan_assignment_changes (account_id, change_kind)
WHERE state = 'scheduled';

CREATE INDEX plan_assignment_changes_due_idx
ON bursar.plan_assignment_changes (effective_at, id)
WHERE state = 'scheduled';

-- Locate exact allowance/quota windows by account, policy dimension, and bounds,
-- with companion plan keys for catalog or plan migration traversals.
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

-- Cover rolling-quota sums by account/quota/time and retain a broader lineage
-- path for correction, audit, and revision-aware measurements.
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

-- Drive event retention and reverse correction/charge lookups; optional lineage
-- indexes exclude the common rows with no correction or linked charge.
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

-- Sum unreleased quota reservations over intersecting windows; released rows no
-- longer contribute to admission and are deliberately absent.
CREATE INDEX credit_lease_quota_active_idx
ON bursar.credit_lease_quota_reservations (
    catalog_quota_id,
    window_start,
    window_end,
    lease_id
)
WHERE released_at IS null;

-- Count and expire active leases by account/operation, excluding terminal rows
-- that cannot consume concurrency or reserved balance.
CREATE INDEX active_leases_idx
ON bursar.credit_leases (account_id, expires_at)
WHERE status = 'active';

CREATE INDEX active_operation_leases_idx
ON bursar.credit_leases (account_id, operation, expires_at)
WHERE status = 'active';

-- Find terminal leases that still carry purgeable dimensions or metadata; empty
-- terminal receipts need no payload-cleanup work and stay out of the index.
CREATE INDEX terminal_lease_payload_retention_idx
ON bursar.credit_leases (updated_at, id)
WHERE status IN ('settled', 'released', 'expired')
AND (
    dimensions <> '{}'::jsonb
    OR metadata <> '{}'::jsonb
);

-- Cover active allowance reservations for a plan/revision, returning the exact
-- reserved value from the index and excluding zero-reservation leases.
CREATE INDEX active_plan_allowance_leases_idx
ON bursar.credit_leases (
    account_id,
    plan_id,
    catalog_revision_id,
    expires_at
)
INCLUDE (reserved_allowance)
WHERE status = 'active' AND reserved_allowance > 0;

-- Support lease reverse relationships to catalog, plan, and settlement ledger;
-- nullable links are indexed only on rows where a relationship exists.
CREATE INDEX credit_leases_catalog_revision_idx
ON bursar.credit_leases (catalog_revision_id);

CREATE INDEX credit_leases_plan_idx
ON bursar.credit_leases (plan_id, catalog_revision_id)
WHERE plan_id IS NOT null;

CREATE INDEX credit_leases_ledger_entry_idx
ON bursar.credit_leases (ledger_entry_id)
WHERE ledger_entry_id IS NOT null;

-- 4. Team, grant, migration, and identity lookups

-- Resolve team membership by subject and cover active role/spend-cap checks;
-- departed members remain history but are omitted from authorization lookups.
CREATE INDEX credit_team_members_subject_idx
ON bursar.credit_team_members (subject_id);

CREATE INDEX credit_team_members_active_team_idx
ON bursar.credit_team_members (team_id, role, subject_id)
INCLUDE (spend_cap, created_at)
WHERE left_at IS null;

-- Read member-attributed team spend newest-first for cap audits and statements.
CREATE INDEX credit_team_usage_member_created_idx
ON bursar.credit_team_usage_charges (
    team_id,
    subject_id,
    created_at DESC,
    id DESC
);

-- Trace qualifying grant events by subject/referrer and omit absent referrers.
CREATE INDEX grant_program_events_subject_idx
ON bursar.grant_program_events (subject_id, occurred_at DESC);

CREATE INDEX grant_program_events_referrer_idx
ON bursar.grant_program_events (referrer_subject_id)
WHERE referrer_subject_id IS NOT null;

-- Find migrations by source/target plan; a NULL source means an all-plan rollout
-- and is excluded from the source-specific path.
CREATE INDEX credit_plan_migrations_from_plan_idx
ON bursar.credit_plan_migrations (from_plan_id)
WHERE from_plan_id IS NOT null;

CREATE INDEX credit_plan_migrations_to_plan_idx
ON bursar.credit_plan_migrations (to_plan_id);

-- Reverse provider-neutral subject identity relationships for lifecycle cleanup.
CREATE INDEX external_identities_subject_idx
ON bursar.external_identities (subject_id);

-- 5. Billing lifecycle and worker lookups

-- Serve subject history, offer reconciliation, renewable-period workers, and
-- overdue grace expiry; partial predicates include only actionable subscriptions.
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

-- Select at most one entitlement source per tenant subject/environment while
-- retaining a reverse subscription lookup for source handoff and reconciliation.
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

-- Read subject payment history and reconcile optional provider invoice IDs using
-- provider/environment keys; uninvoiced payments stay out of the latter index.
CREATE INDEX billing_payments_subject_idx
ON bursar.billing_payments (subject_id, created_at DESC);

CREATE INDEX billing_payments_provider_invoice_idx
ON bursar.billing_payments (provider, provider_environment, provider_invoice_id)
WHERE provider_invoice_id IS NOT null;

-- Feed webhook workers processing rows or retryable failures by lease/creation
-- order, and join partitioned envelopes back to their durable event.
CREATE INDEX billing_events_claimable_idx
ON bursar.billing_events (claim_expires_at, created_at, id)
WHERE status IN ('processing', 'failed');

-- Keep durable webhook attribution queryable for subject privacy workflows;
-- unattributed provider envelopes stay out of this index until reconciled.
CREATE INDEX billing_events_subject_idx
ON bursar.billing_events (subject_id, created_at DESC)
WHERE subject_id IS NOT null;

CREATE INDEX billing_event_payload_event_idx
ON bursar.billing_event_payloads (event_id, received_at);

-- Investigate subject-associated conflicts without bloating the index with
-- provider conflicts that have not yet resolved to a subject.
CREATE INDEX billing_subscription_conflicts_subject_idx
ON bursar.billing_subscription_conflicts (subject_id, created_at DESC)
WHERE subject_id IS NOT null;

-- Traverse credit grants from subject, payment, subscription, topup, ledger, or
-- webhook provenance; optional-source indexes contain only populated links.
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

-- Reconcile refunds against payments and their grant/clawback ledger mappings.
CREATE INDEX billing_refunds_payment_idx
ON bursar.billing_refunds (payment_id);

CREATE INDEX billing_refund_grants_grant_idx
ON bursar.billing_refund_grants (grant_id);

CREATE INDEX billing_refund_grants_ledger_entry_idx
ON bursar.billing_refund_grants (ledger_entry_id)
WHERE ledger_entry_id IS NOT null;

-- Permit one awaiting/scheduled change per subscription and support both old and
-- new offer reverse lookups for revision-aware transition processing.
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

-- Resolve profile/attempt topups and count submitted, processing, succeeded, or
-- action-required attempts per subject window using the worker's exact predicate.
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

-- Read invoices newest-first by effective period, follow optional subscriptions,
-- and support dispute investigation from subject or payment when populated.
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

-- 6. Referencing-side foreign-key support

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
