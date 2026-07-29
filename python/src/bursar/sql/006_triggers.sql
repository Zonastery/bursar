-- Trigger declarations and mutation guards.

CREATE TRIGGER catalog_revision_state
BEFORE UPDATE ON bursar.catalog_revisions
FOR EACH ROW EXECUTE FUNCTION bursar.one_active_catalog_revision();

CREATE TRIGGER catalog_revision_delete
BEFORE DELETE ON bursar.catalog_revisions
FOR EACH ROW EXECUTE FUNCTION bursar.reject_revision_delete();

CREATE TRIGGER catalog_bucket_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_buckets
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_operation_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_operations
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_rate_card_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_rate_cards
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_credit_policy_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_credit_policies
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_admission_policy_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_admission_policies
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_admission_operation_policy_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_admission_operation_policies
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_entitlement_feature_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_entitlement_features
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_entitlement_feature_validate
BEFORE INSERT ON bursar.catalog_entitlement_features
FOR EACH ROW EXECUTE FUNCTION bursar.validate_catalog_entitlement_feature();

CREATE TRIGGER catalog_plan_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_plans
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_plan_feature_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_plan_features
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_plan_feature_validate
BEFORE INSERT ON bursar.catalog_plan_features
FOR EACH ROW EXECUTE FUNCTION bursar.validate_catalog_plan_feature();

CREATE TRIGGER catalog_plan_quota_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_plan_quotas
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_plan_quota_validate
BEFORE INSERT ON bursar.catalog_plan_quotas
FOR EACH ROW EXECUTE FUNCTION bursar.validate_catalog_plan_quota();

CREATE TRIGGER catalog_grant_program_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_grant_programs
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_grant_award_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_grant_awards
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_grant_award_validate
BEFORE INSERT ON bursar.catalog_grant_awards
FOR EACH ROW EXECUTE FUNCTION bursar.validate_catalog_grant_award();

CREATE TRIGGER catalog_offer_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_offers
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_topup_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_topups
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_auto_recharge_policy_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_auto_recharge_policies
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_provider_ref_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_provider_refs
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_provider_ref_validate
BEFORE INSERT ON bursar.catalog_provider_refs
FOR EACH ROW EXECUTE FUNCTION bursar.validate_catalog_provider_ref();

CREATE TRIGGER catalog_bucket_timezone
BEFORE INSERT OR UPDATE ON bursar.catalog_buckets
FOR EACH ROW EXECUTE FUNCTION bursar.require_valid_timezone(
    'expires_after_timezone'
);

CREATE TRIGGER catalog_plan_timezone
BEFORE INSERT OR UPDATE ON bursar.catalog_plans
FOR EACH ROW EXECUTE FUNCTION bursar.require_valid_timezone(
    'credit_allowance_reset_timezone'
);

CREATE TRIGGER catalog_auto_recharge_policy_timezone
BEFORE INSERT OR UPDATE ON bursar.catalog_auto_recharge_policies
FOR EACH ROW EXECUTE FUNCTION bursar.require_valid_timezone('period_timezone');

CREATE TRIGGER allowance_window_timezone
BEFORE INSERT OR UPDATE ON bursar.allowance_windows
FOR EACH ROW EXECUTE FUNCTION bursar.require_valid_timezone('period_timezone');

CREATE TRIGGER auto_recharge_profile_timezone
BEFORE INSERT OR UPDATE ON bursar.billing_auto_recharge_profiles
FOR EACH ROW EXECUTE FUNCTION bursar.require_valid_timezone('window_timezone');

CREATE TRIGGER ledger_append_only
BEFORE INSERT OR UPDATE OR DELETE ON bursar.credit_ledger_entries
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER ledger_balance_invariant
BEFORE INSERT ON bursar.credit_ledger_entries
FOR EACH ROW EXECUTE FUNCTION bursar.check_ledger_balance();

CREATE TRIGGER lot_source_invariant
BEFORE INSERT ON bursar.credit_lots
FOR EACH ROW EXECUTE FUNCTION bursar.check_credit_lot();

CREATE TRIGGER lot_funding_source_invariant
BEFORE INSERT ON bursar.credit_lot_sources
FOR EACH ROW EXECUTE FUNCTION bursar.check_credit_lot_source();

CREATE TRIGGER lot_allocation_invariant
BEFORE INSERT ON bursar.credit_lot_allocations
FOR EACH ROW EXECUTE FUNCTION bursar.check_lot_allocation();

CREATE TRIGGER lot_source_allocation_invariant
BEFORE INSERT ON bursar.credit_lot_source_allocations
FOR EACH ROW EXECUTE FUNCTION bursar.check_lot_source_allocation();

CREATE TRIGGER lot_restoration_invariant
BEFORE INSERT ON bursar.credit_lot_restorations
FOR EACH ROW EXECUTE FUNCTION bursar.check_lot_restoration();

CREATE TRIGGER lot_source_restoration_invariant
BEFORE INSERT ON bursar.credit_lot_source_restorations
FOR EACH ROW EXECUTE FUNCTION bursar.check_lot_source_restoration();

CREATE TRIGGER lots_internal_only
BEFORE UPDATE OR DELETE ON bursar.credit_lots
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER lot_sources_internal_only
BEFORE UPDATE OR DELETE ON bursar.credit_lot_sources
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER allocations_internal_only
BEFORE UPDATE OR DELETE ON bursar.credit_lot_allocations
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER source_allocations_internal_only
BEFORE UPDATE OR DELETE ON bursar.credit_lot_source_allocations
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER restorations_internal_only
BEFORE UPDATE OR DELETE ON bursar.credit_lot_restorations
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER source_restorations_internal_only
BEFORE UPDATE OR DELETE ON bursar.credit_lot_source_restorations
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER usage_charges_append_only
BEFORE UPDATE OR DELETE ON bursar.credit_usage_charges
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER usage_charge_projection
AFTER INSERT ON bursar.credit_usage_charges
FOR EACH ROW EXECUTE FUNCTION bursar.project_usage_charge();

CREATE TRIGGER quota_usage_events_append_only
BEFORE UPDATE OR DELETE ON bursar.quota_usage_events
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER quota_usage_event_invariant
BEFORE INSERT ON bursar.quota_usage_events
FOR EACH ROW EXECUTE FUNCTION bursar.check_quota_usage_event();

CREATE TRIGGER quota_usage_event_outbox
AFTER INSERT ON bursar.quota_usage_events
FOR EACH ROW EXECUTE FUNCTION bursar.enqueue_quota_measurement();

CREATE TRIGGER quota_notification_outbox
AFTER INSERT ON bursar.quota_events
FOR EACH ROW EXECUTE FUNCTION bursar.enqueue_quota_notification();

CREATE TRIGGER grant_events_append_only
BEFORE UPDATE OR DELETE ON bursar.grant_program_events
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER grant_award_executions_append_only
BEFORE UPDATE OR DELETE ON bursar.grant_award_executions
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER account_creation_grants_append_only
BEFORE UPDATE OR DELETE ON bursar.account_creation_grants
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER account_updated_at
BEFORE UPDATE ON bursar.credit_accounts
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER account_rearm_auto_recharge
AFTER UPDATE OF balance ON bursar.credit_accounts
FOR EACH ROW EXECUTE FUNCTION bursar.rearm_auto_recharge_profile();

CREATE TRIGGER plan_assignment_archive
BEFORE UPDATE OR DELETE ON bursar.account_plan_assignments
FOR EACH ROW EXECUTE FUNCTION bursar.archive_plan_assignment();

CREATE TRIGGER plan_assignment_updated_at
BEFORE UPDATE ON bursar.account_plan_assignments
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER lease_updated_at
BEFORE UPDATE ON bursar.credit_leases
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER plan_migration_updated_at
BEFORE UPDATE ON bursar.credit_plan_migrations
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_customer_updated_at
BEFORE UPDATE ON bursar.billing_customers
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_subscription_updated_at
BEFORE UPDATE ON bursar.billing_subscriptions
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_payment_updated_at
BEFORE UPDATE ON bursar.billing_payments
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_payment_status_transition
BEFORE UPDATE OF status ON bursar.billing_payments
FOR EACH ROW EXECUTE FUNCTION bursar.validate_billing_payment_transition();

CREATE TRIGGER billing_event_updated_at
BEFORE UPDATE ON bursar.billing_events
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_event_completed_outbox
AFTER UPDATE OF status ON bursar.billing_events
FOR EACH ROW EXECUTE FUNCTION bursar.enqueue_completed_billing_event();

CREATE TRIGGER storage_settings_updated_at
BEFORE UPDATE ON bursar.storage_settings
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER event_outbox_updated_at
BEFORE UPDATE ON bursar.event_outbox
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_refund_updated_at
BEFORE UPDATE ON bursar.billing_refunds
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_refund_status_transition
BEFORE UPDATE OF status ON bursar.billing_refunds
FOR EACH ROW EXECUTE FUNCTION bursar.validate_billing_refund_transition();

CREATE TRIGGER billing_subscription_change_updated_at
BEFORE UPDATE ON bursar.billing_subscription_changes
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER recharge_profile_updated_at
BEFORE UPDATE ON bursar.billing_auto_recharge_profiles
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER recharge_attempt_updated_at
BEFORE UPDATE ON bursar.billing_auto_recharge_attempts
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_checkout_intent_updated_at
BEFORE UPDATE ON bursar.billing_checkout_intents
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_invoice_updated_at
BEFORE UPDATE ON bursar.billing_invoices
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_dispute_updated_at
BEFORE UPDATE ON bursar.billing_disputes
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_preferences_updated_at
BEFORE UPDATE ON bursar.billing_preferences
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER refund_bounds
BEFORE INSERT OR UPDATE ON bursar.billing_refunds
FOR EACH ROW EXECUTE FUNCTION bursar.check_refund_bounds();

CREATE TRIGGER refund_grant_bounds
BEFORE INSERT OR UPDATE ON bursar.billing_refund_grants
FOR EACH ROW EXECUTE FUNCTION bursar.check_refund_bounds();
