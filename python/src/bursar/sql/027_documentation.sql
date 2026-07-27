-- Human-readable data-model and RPC contract documentation.
-- Keep these comments close to the baseline so schema browsers expose the
-- same domain language used by the SDKs.

COMMENT ON TABLE bursar.subjects IS 'Stable application principals that own credit and billing state.';
COMMENT ON TABLE bursar.external_identities IS 'Provider-specific identities mapped to a Bursar subject.';
COMMENT ON TABLE bursar.catalog_revisions IS 'Immutable published pricing/configuration revisions.';
COMMENT ON TABLE bursar.catalog_buckets IS 'Credit bucket projections for a catalog revision.';
COMMENT ON TABLE bursar.catalog_plans IS 'Plan projections and policy defaults for a catalog revision.';
COMMENT ON TABLE bursar.catalog_signup_grants IS 'One-time signup grant policy projected from the catalog.';
COMMENT ON TABLE bursar.catalog_offers IS 'Subscription offer projections for a catalog revision.';
COMMENT ON TABLE bursar.catalog_topups IS 'Credit top-up projections for a catalog revision.';
COMMENT ON TABLE bursar.catalog_provider_refs IS 'Provider lookup references projected from catalog configuration.';
COMMENT ON TABLE bursar.credit_accounts IS 'Canonical monetary balance per subject/account kind.';
COMMENT ON TABLE bursar.credit_ledger_entries IS 'Append-only monetary ledger and balance snapshots.';
COMMENT ON TABLE bursar.credit_lots IS 'Bucket-level credit grants with consumption and expiry state.';
COMMENT ON TABLE bursar.credit_lot_allocations IS 'Allocation of debit ledger entries to credit lots.';
COMMENT ON TABLE bursar.credit_usage_charges IS 'Idempotent usage charge records and allowance attribution.';
COMMENT ON TABLE bursar.account_plan_assignments IS 'Current and historical plan assignments for accounts.';
COMMENT ON TABLE bursar.allowance_windows IS 'Allowance consumption and reservation state per policy window.';
COMMENT ON TABLE bursar.feature_call_windows IS 'Feature call admission counters for bounded policies.';
COMMENT ON TABLE bursar.feature_limit_events IS 'Idempotent warn/notify events emitted at feature limits.';
COMMENT ON TABLE bursar.credit_leases IS 'Temporary credit and feature-capacity reservations.';
COMMENT ON TABLE bursar.credit_teams IS 'Team principals backed by team credit accounts.';
COMMENT ON TABLE bursar.credit_team_members IS 'Subject membership and role assignments for teams.';
COMMENT ON TABLE bursar.signup_credit_grants IS 'Idempotency records for signup credit grants.';
COMMENT ON TABLE bursar.credit_plan_migrations IS 'Batched account migration cursors between plans.';
COMMENT ON TABLE bursar.billing_customers IS 'Provider customer identities linked to subjects.';
COMMENT ON TABLE bursar.billing_subscriptions IS 'Provider subscription state and catalog entitlement context.';
COMMENT ON TABLE bursar.billing_entitlement_sources IS 'Source-of-entitlement links for billing subscriptions.';
COMMENT ON TABLE bursar.billing_payments IS 'Provider payment records and settlement state.';
COMMENT ON TABLE bursar.billing_events IS 'Claimable, idempotent provider webhook envelopes.';
COMMENT ON TABLE bursar.billing_credit_grants IS 'Credit grants linked to billing payments, subscriptions, or top-ups.';
COMMENT ON TABLE bursar.billing_refunds IS 'Provider refund records and reconciliation state.';
COMMENT ON TABLE bursar.billing_refund_grants IS 'Credit reversals allocated to billing refunds.';
COMMENT ON TABLE bursar.billing_subscription_changes IS 'Scheduled and payment-gated subscription transitions.';
COMMENT ON TABLE bursar.billing_auto_recharge_profiles IS 'Canonical auto-recharge configuration per subject.';
COMMENT ON TABLE bursar.billing_auto_recharge_attempts IS 'Claimable auto-recharge attempts and provider outcomes.';
COMMENT ON TABLE bursar.catalog_auto_recharge_policies IS 'Auto-recharge policy projections from catalog configuration.';
COMMENT ON TABLE bursar.billing_checkout_intents IS 'Idempotent checkout intents and provider references.';
COMMENT ON TABLE bursar.billing_invoices IS 'Provider invoice records associated with billing state.';
COMMENT ON TABLE bursar.billing_disputes IS 'Provider dispute records and linked payment context.';
COMMENT ON TABLE bursar.billing_preferences IS 'Subject-level billing notification and payment preferences.';

COMMENT ON FUNCTION bursar.publish_and_activate_catalog(integer, jsonb, text)
    IS 'Validate, project, and activate one immutable catalog revision.';
COMMENT ON FUNCTION bursar.post_credit(uuid, bursar.ledger_entry_kind, numeric, text, text, jsonb, text, uuid, timestamptz, numeric)
    IS 'Idempotent public ledger mutation that creates or consumes bucketed credits.';
COMMENT ON FUNCTION bursar.charge_usage_for_operation(uuid, text, numeric, text, text, text, text, jsonb)
    IS 'Authorize and record one operation charge using plan policy and allowance state.';
COMMENT ON FUNCTION bursar.create_lease_for_operation(uuid, text, numeric, text, text, integer, interval, jsonb)
    IS 'Create an idempotent operation lease with monetary and feature-capacity reservations.';
COMMENT ON FUNCTION bursar.settle_lease(uuid, uuid, numeric, text)
    IS 'Settle or replay a lease while preserving ledger and reservation invariants.';
COMMENT ON FUNCTION bursar.upsert_auto_recharge_profile(uuid, boolean, text, uuid, integer, numeric, integer, text, integer, text, text)
    IS 'Upsert subject auto-recharge settings validated against the active catalog policy.';
COMMENT ON FUNCTION bursar.list_feature_limit_events(uuid, timestamptz, timestamptz)
    IS 'List durable warn/notify events emitted for a subject feature limit.';
