-- Human-readable data-model and RPC contract documentation.
-- Keep these comments close to the baseline so schema browsers expose the
-- same domain language used by the SDKs.

COMMENT ON TABLE bursar.subjects IS
'Stable application principals that own credit and billing state, '
'with irreversible financial pseudonymization state.';
COMMENT ON TABLE bursar.storage_settings IS
'Singleton PostgreSQL hot-storage retention, lateness, and maintenance policy.';
COMMENT ON TABLE bursar.storage_partitions IS
'Registry of Bursar-managed monthly payload partitions used for low-cost expiry.';
COMMENT ON TABLE bursar.external_identities IS 'Provider-specific identities mapped to a Bursar subject.';
COMMENT ON TABLE bursar.catalog_revisions IS 'Immutable published pricing/configuration revisions.';
COMMENT ON TABLE bursar.catalog_activation_history IS 'Append-only audit trail of catalog activation and supersession.';
COMMENT ON TABLE bursar.catalog_buckets IS 'Credit bucket projections for a catalog revision.';
COMMENT ON TABLE bursar.catalog_operations IS
'Metered operation and pricing-measure definitions for a catalog revision.';
COMMENT ON TABLE bursar.catalog_rate_cards IS
'Operation rate cards, rules, and charge-model snapshots for a catalog revision.';
COMMENT ON TABLE bursar.catalog_credit_policies IS
'Credit consumption, debt, and enforcement policies projected from the catalog.';
COMMENT ON TABLE bursar.catalog_admission_policies IS
'Default concurrent-work admission policies projected from the catalog.';
COMMENT ON TABLE bursar.catalog_admission_operation_policies IS 'Operation-level overrides for admission policies.';
COMMENT ON TABLE bursar.catalog_entitlement_features IS 'Typed entitlement feature definitions and defaults.';
COMMENT ON TABLE bursar.catalog_plans IS 'Plan projections and policy defaults for a catalog revision.';
COMMENT ON TABLE bursar.catalog_plan_features IS 'Typed feature values granted by a plan.';
COMMENT ON TABLE bursar.catalog_plan_quotas IS 'Plan-level numeric usage quota policies.';
COMMENT ON TABLE bursar.catalog_grant_programs IS 'Promotional, referral, manual, and account-creation grant programs.';
COMMENT ON TABLE bursar.catalog_grant_awards IS 'Per-recipient credit awards belonging to grant programs.';
COMMENT ON TABLE bursar.catalog_offers IS 'Subscription offer projections for a catalog revision.';
COMMENT ON TABLE bursar.catalog_topups IS 'Credit top-up projections for a catalog revision.';
COMMENT ON TABLE bursar.catalog_provider_refs IS 'Provider lookup references projected from catalog configuration.';
COMMENT ON TABLE bursar.credit_accounts IS 'Canonical monetary balance per subject/account kind.';
COMMENT ON TABLE bursar.credit_ledger_entries IS 'Append-only monetary ledger and balance snapshots.';
COMMENT ON TABLE bursar.credit_lots IS 'Bucket-level credit grants with consumption and expiry state.';
COMMENT ON TABLE bursar.credit_lot_sources IS 'Many-to-one provenance for grants merged into a spendable credit lot.';
COMMENT ON TABLE bursar.credit_lot_allocations IS 'Allocation of debit ledger entries to credit lots.';
COMMENT ON TABLE bursar.credit_lot_source_allocations IS
'Allocation of lot consumption to individual grant sources inside merged lots.';
COMMENT ON TABLE bursar.credit_lot_restorations IS 'Append-only audit of credits restored to their original lots.';
COMMENT ON TABLE bursar.credit_lot_source_restorations IS
'Source-level restoration audit preserving provenance when usage is refunded.';
COMMENT ON TABLE bursar.credit_unallocated_debits IS 'Debit amounts not backed by lots when a policy permits debt.';
COMMENT ON TABLE bursar.credit_debt_repayments IS 'Positive ledger amounts applied to outstanding account debt.';
COMMENT ON TABLE bursar.credit_usage_charges IS
'Permanent compact idempotent usage receipt, accounting, and allowance evidence.';
COMMENT ON TABLE bursar.usage_charge_payloads IS
'Retention-bounded usage details, pricing snapshot, dimensions, and application metadata, partitioned by event time.';
COMMENT ON TABLE bursar.usage_daily_rollups IS 'Bounded exact daily usage aggregates for PostgreSQL-only analytics.';
COMMENT ON FUNCTION bursar.uuid_v7()
IS 'Generate an RFC 9562 UUIDv7 with millisecond time locality for database row identifiers.';
COMMENT ON TABLE bursar.event_outbox IS
'Bounded claimable versioned events for optional external delivery and analytics sinks.';
COMMENT ON TABLE bursar.account_plan_assignments IS 'Current effective plan assignment for each account.';
COMMENT ON TABLE bursar.account_plan_assignment_history IS 'Append-only effective-dated plan assignment history.';
COMMENT ON TABLE bursar.plan_assignment_changes IS 'Scheduled or applied plan changes used for renewal-safe rollouts.';
COMMENT ON TABLE bursar.allowance_windows IS 'Allowance consumption and reservation state per policy window.';
COMMENT ON TABLE bursar.quota_windows IS 'Current numeric usage and reservations for plan quota windows.';
COMMENT ON TABLE bursar.quota_usage_events IS
'Immutable metered quota deltas supporting rolling windows, late events, and corrections.';
COMMENT ON TABLE bursar.quota_events IS 'Idempotent quota threshold and blocked-admission notifications.';
COMMENT ON TABLE bursar.credit_leases IS 'Temporary credit and concurrent-operation admission reservations.';
COMMENT ON TABLE bursar.credit_lease_quota_reservations IS 'Per-quota capacity reserved by an active credit lease.';
COMMENT ON TABLE bursar.credit_teams IS 'Team principals backed by team credit accounts.';
COMMENT ON TABLE bursar.credit_team_members IS 'Subject membership and role assignments for teams.';
COMMENT ON TABLE bursar.credit_team_usage_charges IS
'Member-attributed team usage used for audit and atomic member spend-cap enforcement.';
COMMENT ON TABLE bursar.grant_program_events IS 'Idempotent business events that can execute catalog grant programs.';
COMMENT ON TABLE bursar.grant_award_executions IS 'Append-only recipient-level executions of catalog grant awards.';
COMMENT ON TABLE bursar.credit_plan_migrations IS 'Batched account migration cursors between plans.';
COMMENT ON TABLE bursar.billing_customers IS 'Provider customer identities linked to subjects.';
COMMENT ON TABLE bursar.billing_subscriptions IS
'Provider subscription truth, catalog entitlement context, '
'and independently tracked past-due grace state.';
COMMENT ON TABLE bursar.billing_entitlement_sources IS 'Source-of-entitlement links for billing subscriptions.';
COMMENT ON TABLE bursar.billing_subscription_conflicts IS
'Append-only audit of duplicate current-subscription admissions requiring reconciliation.';
COMMENT ON TABLE bursar.billing_payments IS
'Provider payment records, invoice correlation, source metadata, and settlement state.';
COMMENT ON TABLE bursar.billing_events IS
'Permanent claim, digest, archive-pointer, and processing state for provider webhooks.';
COMMENT ON TABLE bursar.billing_event_payloads IS
'Retention-bounded raw provider webhook envelopes, partitioned by receipt time.';
COMMENT ON TABLE bursar.billing_credit_grants IS 'Credit grants linked to billing payments, subscriptions, or top-ups.';
COMMENT ON TABLE bursar.billing_refunds IS
'Provider refund records, source metadata, payment-identity validation, and reconciliation state.';
COMMENT ON TABLE bursar.billing_refund_grants IS 'Credit reversals allocated to billing refunds.';
COMMENT ON TABLE bursar.billing_subscription_changes IS 'Scheduled and payment-gated subscription transitions.';
COMMENT ON TABLE bursar.billing_auto_recharge_profiles IS
'Canonical auto-recharge configuration per subject and provider environment.';
COMMENT ON TABLE bursar.billing_auto_recharge_attempts IS 'Claimable auto-recharge attempts and provider outcomes.';
COMMENT ON TABLE bursar.catalog_auto_recharge_policies IS
'Auto-recharge policy projections from catalog configuration.';
COMMENT ON TABLE bursar.billing_checkout_intents IS 'Idempotent checkout intents and provider references.';
COMMENT ON TABLE bursar.billing_invoices IS 'Provider invoice records associated with billing state.';
COMMENT ON TABLE bursar.billing_disputes IS 'Provider dispute records and linked payment context.';
COMMENT ON TABLE bursar.billing_preferences IS 'Subject-level billing notification and payment preferences.';

COMMENT ON FUNCTION bursar.publish_and_activate_catalog(integer, jsonb, text, boolean)
IS 'Validate and project one immutable catalog revision, optionally activating it.';
COMMENT ON FUNCTION bursar.post_credit(
    uuid, bursar.ledger_entry_kind, numeric, text, text, jsonb, text, uuid, timestamptz, numeric
)
IS 'Idempotent public ledger mutation that creates or consumes bucketed credits.';
COMMENT ON FUNCTION bursar.execute_grant_program(text, text, uuid, text, uuid, text, jsonb)
IS 'Execute an eligible catalog grant program with lifetime award limits and event- or subject-scoped idempotency.';
COMMENT ON FUNCTION bursar.charge_usage_for_operation(uuid, text, numeric, text, text, text, text, jsonb, jsonb, jsonb)
IS 'Authorize and record one operation charge using plan policy and allowance state.';
COMMENT ON FUNCTION bursar.create_lease_for_operation(
    uuid, text, numeric, text, interval, jsonb, text, jsonb, jsonb, numeric, integer
)
IS 'Create an idempotent operation lease with credit and concurrent-admission reservations.';
COMMENT ON FUNCTION bursar.settle_lease(uuid, uuid, numeric, text, text, text, text, jsonb, jsonb, jsonb)
IS 'Settle or replay a lease while preserving ledger and reservation invariants.';
COMMENT ON FUNCTION bursar.renew_lease(uuid, uuid, interval)
IS 'Extend an active lease without weakening its captured plan, allowance, quota, or credit policy.';
COMMENT ON FUNCTION bursar.get_credit_lease_pricing_context(uuid, uuid)
IS
'Read the immutable catalog revision and rate-card context captured by a '
'subject-owned lease for deterministic settlement pricing.';
COMMENT ON FUNCTION bursar.upsert_auto_recharge_profile(
    uuid, boolean, text, uuid, integer, numeric, integer, text, integer, text, text, boolean, text, boolean
)
IS 'Upsert subject auto-recharge settings validated against the active catalog policy.';
COMMENT ON FUNCTION bursar.record_subscription_conflict(uuid, text, text, text, text, jsonb)
IS
'Idempotently record a duplicate provider-subscription admission conflict '
'without falsifying either subscription state.';
COMMENT ON FUNCTION bursar.mark_subscription_grace_expired(uuid, timestamptz, timestamptz)
IS 'Compare-and-set completion marker for a past-due entitlement grace deadline.';
COMMENT ON FUNCTION bursar.pseudonymize_financial_subject(uuid)
IS
'Irreversibly remove Bursar-held external identity links and mutable PII '
'while retaining financial reconciliation records.';
COMMENT ON FUNCTION bursar.get_subject_entitlements(uuid, timestamptz)
IS 'Resolve typed entitlement values from the subject pinned plan revision, falling back to feature defaults.';
COMMENT ON FUNCTION bursar.get_subject_quota_state(uuid, text)
IS 'Read current normalized quota consumption, reservations, remaining capacity, and overage for a subject.';
COMMENT ON FUNCTION bursar.list_subject_quota_events(
    uuid,
    timestamptz,
    integer,
    text,
    uuid
)
IS
'List persisted threshold and blocked-admission quota notifications for a '
'subject with a stable composite cursor.';
COMMENT ON FUNCTION bursar.list_billing_invoices(
    uuid,
    timestamptz,
    uuid,
    integer
)
IS 'List one bounded keyset page of invoices for a subject and provider environment.';
COMMENT ON FUNCTION bursar.create_checkout_intent(
    uuid,
    text,
    text,
    text,
    bytea,
    timestamptz,
    text,
    text,
    text
)
IS
'Create or replay an idempotent checkout pinned to an available catalog '
'product, provider environment, and region; terminal attempts require a '
'new request digest.';
