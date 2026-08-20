-- Migration: 027_documentation.sql
-- Purpose: Attach catalog-visible domain, lifecycle, and public-RPC documentation.
-- Depends on: All Bursar tables and public SDK RPCs defined through 025.
-- Security: Catalog comments grant no access; 026, 029, and 030 remain the
--   authority for PUBLIC revocation, forced RLS, caller grants, and owner roles.
--
-- Contents
--   1. Subject, storage, catalog, and credit tables
--   2. Shared validation and schema helpers
--   3. Plans, quotas, teams, and billing tables
--   4. Core credit and billing RPC contracts
--   5. Team, provider lifecycle, and document RPC contracts
--   6. Query, plan, quota, and transition RPC contracts

COMMENT ON SCHEMA bursar IS
'Tenant-isolated PostgreSQL accounting, credit, usage, catalog, and billing authority.';

-- ---------------------------------------------------------------------------
-- 1. Subject, storage, catalog, and credit tables
-- ---------------------------------------------------------------------------

COMMENT ON TABLE bursar.subjects IS
'Stable application principals that own credit and billing state, '
'with irreversible financial pseudonymization state.';
COMMENT ON TABLE bursar.storage_settings IS
'Singleton PostgreSQL hot-storage retention, lateness, and maintenance policy.';
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
COMMENT ON COLUMN bursar.catalog_plans.credit_allowance_priority IS
'Required spend priority for plan allowances, shared with credit-lot priorities.';
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
'Common idempotent usage journal for permanent billable receipts and retention-bound record-only workflow telemetry.';
COMMENT ON COLUMN bursar.credit_usage_charges.billing_disposition IS
'Whether the usage event affected billing or was retained only for cost attribution.';
COMMENT ON TABLE bursar.usage_charge_payloads IS
'Retention-bounded usage details, pricing snapshot, dimensions, and application metadata, partitioned by event time.';
COMMENT ON TABLE bursar.usage_daily_rollups IS 'Bounded exact daily usage aggregates for PostgreSQL-only analytics.';
-- ---------------------------------------------------------------------------
-- 2. Shared validation and schema helpers
-- ---------------------------------------------------------------------------

COMMENT ON FUNCTION bursar.uuid_v7()
IS 'Generate an RFC 9562 UUIDv7 with millisecond time locality for database row identifiers.';
COMMENT ON FUNCTION bursar.is_nonempty_bounded_text(text, integer)
IS 'Validate trimmed non-empty text against an explicit character limit.';
COMMENT ON FUNCTION bursar.catalog_document_shape_schema() IS
'Canonical versioned JSON Schema for the complete Bursar pricing catalog.';
COMMENT ON FUNCTION bursar.matches_catalog_fragment(jsonb, jsonb) IS
'Validates a projected catalog fragment against a canonical sub-schema with the catalog definitions in scope.';
COMMENT ON FUNCTION bursar.matches_catalog_definitions(jsonb, VARIADIC text []) IS
'Validates a catalog projection against one or more named definitions from the canonical catalog JSON Schema.';
COMMENT ON FUNCTION bursar.entitlement_value_schema(jsonb) IS
'Builds the JSON Schema for a value from a validated boolean, integer, string, or enum entitlement definition.';
COMMENT ON FUNCTION bursar.measure_object_schema() IS
'JSON Schema for an open set of named numeric usage and job measures.';
COMMENT ON FUNCTION bursar.dimension_object_schema() IS
'JSON Schema for an open set of named scalar usage and job dimensions.';
COMMENT ON FUNCTION bursar.usage_pricing_snapshot_schema() IS
'JSON Schema for immutable usage-charge pricing snapshots.';
COMMENT ON FUNCTION bursar.valid_measure_object(jsonb, integer) IS
'Validates measure structure with pg_jsonschema and enforces PostgreSQL numeric finiteness, sign, and byte limits.';
COMMENT ON FUNCTION bursar.valid_dimension_object(jsonb, integer) IS
'Validates scalar dimension structure with pg_jsonschema and enforces byte limits.';
COMMENT ON FUNCTION bursar.catalog_plan_rollout_schema() IS
'JSON Schema for plan rollout overrides supplied during catalog activation.';
-- ---------------------------------------------------------------------------
-- 3. Plans, quotas, teams, and billing tables
-- ---------------------------------------------------------------------------

COMMENT ON TABLE bursar.event_outbox IS
'Claimable versioned events for optional sinks; delivered payloads are '
'retention-bounded while required undelivered events remain replayable.';
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
COMMENT ON TABLE bursar.credit_team_members IS
'Durable memberships; inactive rows retain historical usage and lifetime spend.';
COMMENT ON COLUMN bursar.credit_team_members.created_at IS
'Timestamp of the subject first joining this team; preserved across reactivation.';
COMMENT ON COLUMN bursar.credit_team_members.left_at IS
'Most recent removal timestamp, or NULL while the membership is active.';
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

-- ---------------------------------------------------------------------------
-- 4. Core credit and billing RPC contracts
-- ---------------------------------------------------------------------------

COMMENT ON FUNCTION bursar.publish_and_activate_catalog(integer, jsonb, text, boolean, jsonb)
IS 'Validate and project one immutable catalog revision, optionally activating it with a per-release rollout manifest.';
COMMENT ON FUNCTION bursar.active_catalog_revision() IS
'Return the active immutable catalog revision visible to the current tenant.';
COMMENT ON FUNCTION bursar.catalog_revision_by_number(bigint) IS
'Resolve one tenant-visible immutable catalog revision by its monotonic revision number.';
COMMENT ON FUNCTION bursar.list_catalog_revisions(integer) IS
'List a bounded newest-first catalog revision history for the current tenant.';
COMMENT ON FUNCTION bursar.activate_catalog_revision(bigint, jsonb) IS
'Serialize activation of one published tenant revision and apply its rollout manifest.';
COMMENT ON FUNCTION bursar.post_credit(
    uuid, bursar.ledger_entry_kind, numeric, text, text, jsonb, text, uuid, timestamptz, numeric
)
IS 'Idempotent ledger mutation that creates or consumes bucketed credits and tracks unallocated debt and repayment.';
COMMENT ON FUNCTION bursar.sweep_expired_lots(integer, uuid, boolean) IS
'Project or claim a bounded tenant batch of due credit lots using stable expiration keys.';
COMMENT ON FUNCTION bursar.revoke_lot(uuid, numeric, text) IS
'Revoke a bounded amount from one tenant-owned lot with stable result codes and replay identity.';
COMMENT ON FUNCTION bursar.refund_credit_by_entry(uuid, numeric, text, text, jsonb) IS
'Refund an exact debit through provenance restoration or debt/new-lot fallback, '
'correcting quotas only after a full refund.';
COMMENT ON FUNCTION bursar.revoke_subject_credits_by_operation(uuid, text) IS
'Atomically revoke all remaining subject credits sourced by one operation key.';
COMMENT ON FUNCTION bursar.execute_grant_program(text, text, uuid, text, uuid, text, jsonb)
IS 'Execute an eligible catalog grant program with lifetime award limits and event- or subject-scoped idempotency.';
COMMENT ON FUNCTION bursar.charge_usage_for_operation(uuid, text, numeric, text, text, text, text, jsonb, jsonb, jsonb)
IS 'Authorize and record one operation charge using plan policy and allowance state.';
COMMENT ON FUNCTION bursar.record_usage(
    uuid, text, numeric, text, text, text, text, jsonb, jsonb, jsonb
)
IS 'Append one idempotent record-only usage event without a ledger debit or allowance consumption.';
COMMENT ON FUNCTION bursar.create_lease_for_operation(
    uuid, text, numeric, text, interval, jsonb, text, jsonb, jsonb, numeric, integer
)
IS 'Create an idempotent operation lease with credit and concurrent-admission reservations.';
COMMENT ON FUNCTION bursar.expire_leases(integer) IS
'Claim a bounded tenant batch of expired leases and release allowance and quota reservations once.';
COMMENT ON FUNCTION bursar.release_lease(uuid, uuid) IS
'Idempotently release one subject-owned lease and all of its reserved capacity.';
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
IS
'Enable or revise auto-recharge against active catalog policy, or disable an '
'existing profile without catalog lookup.';
COMMENT ON FUNCTION bursar.record_subscription_conflict(uuid, text, text, text, text, jsonb)
IS
'Idempotently record a duplicate provider-subscription admission conflict '
'without falsifying either subscription state.';
COMMENT ON FUNCTION bursar.mark_subscription_grace_expired(uuid, timestamptz, timestamptz)
IS 'Compare-and-set completion marker for a past-due entitlement grace deadline.';
COMMENT ON FUNCTION bursar.pseudonymize_financial_subject(uuid)
IS
'Delete application external identities and clear selected PII-bearing fields '
'while retaining provider financial identifiers and reconciliation records; '
'already archived external objects remain referenced until host-confirmed erasure.';
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
'List persisted quota notifications newest-first; timestamp-only and exact '
'timestamp/ID cursors both continue strictly backward.';
COMMENT ON FUNCTION bursar.list_billing_invoices(
    uuid,
    timestamptz,
    uuid,
    integer
)
IS 'List one bounded keyset page of invoices for a subject and provider environment.';
COMMENT ON FUNCTION bursar.resolve_catalog_offer(text, text, text)
IS 'Resolve an active provider reference to its catalog subscription offer.';
COMMENT ON FUNCTION bursar.resolve_catalog_topup(text, text, text)
IS 'Resolve an active provider reference to its catalog credit top-up.';
COMMENT ON FUNCTION bursar.resolve_catalog_plan(text, text, text)
IS 'Resolve the plan attached to an active provider-referenced catalog offer.';
COMMENT ON FUNCTION bursar.create_checkout_intent(
    uuid,
    text,
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
'Create or replay one checkout intent bound to the caller operation key and '
'a stored request digest that callers compare before provider side effects.';
COMMENT ON FUNCTION bursar.advance_checkout_intent(uuid, text, text, text)
IS 'Advance an open checkout intent and reject data attachment to terminal intents.';
COMMENT ON FUNCTION bursar.create_team(uuid, text, text, numeric)
IS 'Create or return one team bound to a tenant-scoped idempotency key and immutable request digest.';
COMMENT ON FUNCTION bursar.deduct_team(uuid, uuid, numeric, text, text, jsonb) IS
'Atomically debit a tenant team under membership, spend-cap, and immutable replay checks.';

-- ---------------------------------------------------------------------------
-- 5. Team, provider lifecycle, and document RPC contracts
-- ---------------------------------------------------------------------------

COMMENT ON FUNCTION bursar.set_team_member(uuid, uuid, text, numeric) IS
'Add, reactivate, or update a member while retaining at least one active owner and validating spend caps.';
COMMENT ON FUNCTION bursar.remove_team_member(uuid, uuid) IS
'Deactivate an active team member except the last owner while retaining history and spend.';
COMMENT ON FUNCTION bursar.list_team_members(uuid) IS
'List one tenant-owned team roster with membership lifecycle and lifetime spend.';
COMMENT ON FUNCTION bursar.get_team_balance(uuid) IS
'Read one tenant-owned team credit balance and active member count.';

COMMENT ON FUNCTION bursar.claim_billing_event(text, text, text, jsonb, integer, integer) IS
'Claim or replay one provider-environment event using an immutable envelope digest and expiring lease.';
COMMENT ON FUNCTION bursar.complete_billing_event(text, text, uuid) IS
'Complete one billing event only while the caller owns its active claim token.';
COMMENT ON FUNCTION bursar.fail_billing_event(text, text, uuid, text) IS
'Release one claimed billing event into retryable failed state with bounded detail.';
COMMENT ON FUNCTION bursar.attribute_billing_event_subject(uuid, uuid) IS
'Compare-and-set durable event attribution and immediately scrub retained local '
'envelopes when the subject was already pseudonymized.';
COMMENT ON FUNCTION bursar.grant_billing_credit(uuid, text) IS
'Post one grant with caller-supplied ledger idempotency or replay its persisted ledger linkage.';
COMMENT ON FUNCTION bursar.post_billing_refund(uuid, uuid, bigint, text) IS
'Claw back one bounded billing grant amount with refund-scoped idempotency and explicit debt for consumed credits.';

COMMENT ON FUNCTION bursar.claim_auto_recharge_attempt(uuid, text) IS
'Claim or replay one subject recharge attempt under catalog, balance, cooldown, and window limits.';
COMMENT ON FUNCTION bursar.advance_auto_recharge_attempt(
    uuid, bursar.recharge_attempt_status, text, text, text, jsonb
) IS
'Advance one tenant-owned recharge attempt without reopening a terminal result '
'or replacing an established provider attempt ID.';

COMMENT ON FUNCTION bursar.upsert_billing_customer(uuid, text, text, text) IS
'Upsert one provider customer without moving that environment-scoped identity between subjects.';
COMMENT ON FUNCTION bursar.upsert_billing_subscription(
    uuid, text, text, text, uuid, bursar.billing_subscription_status,
    timestamptz, timestamptz, boolean, jsonb, timestamptz, timestamptz,
    timestamptz, timestamptz, timestamptz
) IS
'Reconcile provider subscription truth with immutable subject identity, '
'replaceable customer context, and update ordering.';
COMMENT ON FUNCTION bursar.list_expired_grace_subscriptions(timestamptz, integer) IS
'List one bounded tenant batch across provider environments whose entitlement grace deadline elapsed.';
COMMENT ON FUNCTION bursar.upsert_billing_payment(
    uuid, text, text, bigint, bigint, text, text,
    bursar.billing_payment_status, timestamptz, text, jsonb
) IS
'Reconcile provider payment truth with immutable subject, currency, purpose, and environment identity.';

COMMENT ON FUNCTION bursar.create_billing_credit_grant(
    uuid, uuid, uuid, numeric, integer, uuid
) IS
'Create or return a credit grant after settled top-up or subscription eligibility validation.';
COMMENT ON FUNCTION bursar.upsert_billing_refund(
    uuid, text, bigint, text, text, timestamptz, uuid, text, jsonb
) IS
'Reconcile a provider refund after validating payment subject, currency, amount, and update order.';
COMMENT ON FUNCTION bursar.upsert_billing_preferences(
    uuid, boolean, boolean, boolean, boolean, boolean
) IS
'Replace billing notification and payment preference flags for one tenant-owned subject.';

COMMENT ON FUNCTION bursar.upsert_billing_invoice(
    uuid, text, text, uuid, text, bigint, bigint, text,
    timestamptz, timestamptz, jsonb, timestamptz
) IS
'Reconcile one provider invoice with immutable subject and provider-environment identity.';
COMMENT ON FUNCTION bursar.upsert_billing_dispute(
    text, text, uuid, text, text, jsonb, timestamptz
) IS
'Reconcile one provider dispute against a payment in the same tenant and environment.';

-- ---------------------------------------------------------------------------
-- 6. Query, plan, quota, and transition RPC contracts
-- ---------------------------------------------------------------------------

COMMENT ON FUNCTION bursar.get_credit_bucket_balances(uuid) IS
'Read non-expired bucket lot balances before account-level lease reservations.';
COMMENT ON FUNCTION bursar.usage_analytics_slice(timestamptz, timestamptz) IS
'Return bounded aggregate and edge usage rows for the current tenant interval.';
COMMENT ON FUNCTION bursar.list_ledger(
    uuid, timestamptz, uuid, integer, text [], timestamptz, timestamptz, boolean
) IS
'Page one subject monetary ledger with a stable cursor and optional type, time, and usage filters.';
COMMENT ON FUNCTION bursar.get_ledger_entry(uuid, uuid) IS
'Fetch one exact ledger entry only when it belongs to the supplied subject.';
COMMENT ON FUNCTION bursar.list_usage_charges(
    uuid, timestamptz, uuid, integer, timestamptz, timestamptz, boolean
) IS
'Page one subject usage journal, including record-only events when requested.';
COMMENT ON FUNCTION bursar.spend_by_user(timestamptz, timestamptz) IS
'Aggregate exact charged usage by subject for the current tenant interval.';
COMMENT ON FUNCTION bursar.spend_by_model(timestamptz, timestamptz) IS
'Aggregate exact charged usage by model for the current tenant interval.';
COMMENT ON FUNCTION bursar.daily_spend(timestamptz, timestamptz) IS
'Aggregate exact charged usage into UTC calendar days for the current tenant.';
COMMENT ON FUNCTION bursar.aggregate_usage_stats(timestamptz, timestamptz) IS
'Return one tenant-wide exact usage summary for the requested interval.';

COMMENT ON FUNCTION bursar.get_billing_customer(uuid, text) IS
'List a subject''s billing customer identities, optionally narrowed to one provider.';
COMMENT ON FUNCTION bursar.get_billing_customer_by_provider(text, text) IS
'Resolve one customer by provider identity in the current tenant and environment.';
COMMENT ON FUNCTION bursar.get_billing_subscription_by_provider(text, text) IS
'Resolve one subscription by provider identity in the current tenant and environment.';
COMMENT ON FUNCTION bursar.list_billing_subscriptions(uuid) IS
'List all provider subscriptions belonging to one tenant-scoped subject.';
COMMENT ON FUNCTION bursar.get_billing_payment_by_provider(text, text) IS
'Resolve one payment by provider identity in the current tenant and environment.';
COMMENT ON FUNCTION bursar.get_billing_preferences(uuid) IS
'Read billing preferences for one tenant-owned subject.';
COMMENT ON FUNCTION bursar.get_auto_recharge_profile(uuid) IS
'Read the subject recharge profile in the configured provider environment.';
COMMENT ON FUNCTION bursar.get_auto_recharge_attempt(uuid) IS
'Read one tenant-owned auto-recharge attempt by internal identity.';
COMMENT ON FUNCTION bursar.get_auto_recharge_attempt_by_provider(text, text) IS
'Resolve one auto-recharge attempt by environment-specific provider identity.';
COMMENT ON FUNCTION bursar.count_auto_recharge_attempts(uuid, timestamptz) IS
'Count one subject''s provider-environment recharge attempts since a timestamp.';

COMMENT ON FUNCTION bursar.resolve_active_catalog_offer(text) IS
'Resolve one offer key from the current tenant active catalog revision.';
COMMENT ON FUNCTION bursar.get_catalog_offer_context(uuid, uuid) IS
'Read immutable plan and billing-cadence context for an exact offer and catalog revision.';
COMMENT ON FUNCTION bursar.get_credit_state(uuid) IS
'Read one subject account balance, lot state, and active reservation totals.';
COMMENT ON FUNCTION bursar.get_credit_operation_details(uuid, uuid, text) IS
'Resolve the latest matching usage charge by ledger identity or idempotency key, with empty context when unmatched.';
COMMENT ON FUNCTION bursar.get_credit_grant_details(uuid, uuid) IS
'Read lot provenance created by one subject-owned credit grant.';
COMMENT ON FUNCTION bursar.get_credit_lease(uuid, uuid) IS
'Read one exact lease only when it belongs to the supplied subject.';
COMMENT ON FUNCTION bursar.resolve_active_plan(text) IS
'Resolve one plan reference from the current tenant active catalog revision.';
COMMENT ON FUNCTION bursar.get_subject_plan(uuid) IS
'Read the effective plan assignment, revision pin, and transition context for one subject.';
COMMENT ON FUNCTION bursar.get_subject_allowance(uuid, timestamptz) IS
'Read normalized allowance consumption and reservation state for one subject window.';

COMMENT ON FUNCTION bursar.get_checkout_intent(uuid, uuid) IS
'Read one checkout intent only for its tenant-owned subject and provider environment.';
COMMENT ON FUNCTION bursar.get_open_billing_subscription_change(text, text) IS
'Read the open transition for one environment-scoped provider subscription.';
COMMENT ON FUNCTION bursar.get_billing_subscription_change(bigint) IS
'Read one tenant-owned subscription transition by internal identity.';
COMMENT ON FUNCTION bursar.get_billing_credit_grant_by_payment(uuid) IS
'Return the oldest tenant-owned credit grant linked to one payment.';

COMMENT ON FUNCTION bursar.assign_plan(uuid, uuid, timestamptz, timestamptz) IS
'Assign one exact plan while closing prior history and carrying compatible policy-window state.';
COMMENT ON FUNCTION bursar.apply_plan_assignment(
    uuid, uuid, timestamptz, timestamptz, text, uuid, text
) IS
'Internal assignment primitive that commits plan policy and durable business-source ownership together.';
COMMENT ON FUNCTION bursar.set_subject_plan(uuid, text, timestamptz) IS
'Assign the active plan for a key and record explicit SDK assignment ownership as manual.';
COMMENT ON FUNCTION bursar.unassign_plan(uuid, text) IS
'End an existing subject plan assignment with an audited reason without provisioning an account.';
COMMENT ON FUNCTION bursar.unassign_plan_if_source(uuid, text, uuid, text) IS
'End a subject plan assignment only while its durable source identity still matches.';
COMMENT ON FUNCTION bursar.replace_subscription_entitlement_if_source(
    uuid, uuid, text, text
) IS
'Atomically replace only the plan assignment owned by one exact subscription source.';
COMMENT ON FUNCTION bursar.reconcile_subscription_entitlement(
    uuid, uuid, uuid, bursar.billing_subscription_status,
    timestamptz, timestamptz, boolean, text, text
) IS
'Fence one exact provider subscription version and atomically apply, preserve, or revoke its entitlement.';
COMMENT ON FUNCTION bursar.expire_subscription_grace_period(
    uuid, uuid, timestamptz, timestamptz, text
) IS
'Atomically commit one expected grace marker and replace its source-owned assignment when still matched.';
COMMENT ON FUNCTION bursar.set_plan_revision_pin(uuid, boolean) IS
'Pin or unpin a subject against automatic catalog plan rollouts.';
COMMENT ON FUNCTION bursar.carry_catalog_plan_revision_state(
    uuid, uuid, uuid, boolean
) IS
'Carry compatible allowance and quota state between revisions of one logical plan.';
COMMENT ON FUNCTION bursar.schedule_catalog_plan_rollout(uuid, jsonb) IS
'Validate a rollout, honor explicit pin overrides, and schedule or apply eligible plan changes.';
COMMENT ON FUNCTION bursar.apply_due_plan_assignment_changes(integer) IS
'Apply a bounded lock-skipping batch of due plan assignment changes.';
COMMENT ON FUNCTION bursar.start_plan_migration(uuid, uuid) IS
'Create a fresh tenant-scoped migration from an optional source plan to a distinct target plan.';
COMMENT ON FUNCTION bursar.migrate_plan_batch(uuid, integer) IS
'Advance one plan migration through a bounded account batch and stable cursor.';
COMMENT ON FUNCTION bursar.open_subscription_change(
    uuid, uuid, timestamptz, text, text, text
) IS
'Create or replay an immutable offer transition for a tenant-scoped subscription UUID.';
COMMENT ON FUNCTION bursar.advance_subscription_change(bigint, text, text, text) IS
'Advance an open transition through a legal outcome and store the latest non-null provider operation ID.';
