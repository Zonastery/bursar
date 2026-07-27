-- Trigger declarations and mutation guards.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE TRIGGER catalog_revision_state
BEFORE UPDATE ON bursar.catalog_revisions
FOR EACH ROW EXECUTE FUNCTION bursar.one_active_catalog_revision();

CREATE TRIGGER catalog_revision_delete
BEFORE DELETE ON bursar.catalog_revisions
FOR EACH ROW EXECUTE FUNCTION bursar.reject_revision_delete();

CREATE TRIGGER catalog_bucket_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_buckets
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_signup_grant_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_signup_grants
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_plan_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_plans
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

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
FOR EACH ROW EXECUTE FUNCTION bursar.require_valid_timezone('expires_after_timezone');

CREATE TRIGGER catalog_plan_timezone
BEFORE INSERT OR UPDATE ON bursar.catalog_plans
FOR EACH ROW EXECUTE FUNCTION bursar.require_valid_timezone('included_credits_reset_timezone');

CREATE TRIGGER catalog_offer_timezone
BEFORE INSERT OR UPDATE ON bursar.catalog_offers
FOR EACH ROW EXECUTE FUNCTION bursar.require_valid_timezone('billing_timezone');

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

CREATE TRIGGER lot_allocation_invariant
BEFORE INSERT ON bursar.credit_lot_allocations
FOR EACH ROW EXECUTE FUNCTION bursar.check_lot_allocation();

CREATE TRIGGER lots_internal_only
BEFORE UPDATE OR DELETE ON bursar.credit_lots
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER allocations_internal_only
BEFORE UPDATE OR DELETE ON bursar.credit_lot_allocations
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER usage_charges_append_only
BEFORE UPDATE OR DELETE ON bursar.credit_usage_charges
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER account_updated_at BEFORE UPDATE ON bursar.credit_accounts
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER account_rearm_auto_recharge
AFTER UPDATE OF balance ON bursar.credit_accounts
FOR EACH ROW EXECUTE FUNCTION bursar.rearm_auto_recharge_profile();

CREATE TRIGGER plan_assignment_updated_at BEFORE UPDATE ON bursar.account_plan_assignments
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER lease_updated_at BEFORE UPDATE ON bursar.credit_leases
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER plan_migration_updated_at BEFORE UPDATE ON bursar.credit_plan_migrations
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_customer_updated_at BEFORE UPDATE ON bursar.billing_customers
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_subscription_updated_at BEFORE UPDATE ON bursar.billing_subscriptions
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_event_updated_at BEFORE UPDATE ON bursar.billing_events
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_subscription_change_updated_at BEFORE UPDATE ON bursar.billing_subscription_changes
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER recharge_profile_updated_at BEFORE UPDATE ON bursar.billing_auto_recharge_profiles
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER recharge_attempt_updated_at BEFORE UPDATE ON bursar.billing_auto_recharge_attempts
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_checkout_intent_updated_at BEFORE UPDATE ON bursar.billing_checkout_intents
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER billing_preferences_updated_at BEFORE UPDATE ON bursar.billing_preferences
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE TRIGGER refund_bounds
BEFORE INSERT OR UPDATE ON bursar.billing_refunds
FOR EACH ROW EXECUTE FUNCTION bursar.check_refund_bounds();

CREATE TRIGGER refund_grant_bounds
BEFORE INSERT OR UPDATE ON bursar.billing_refund_grants
FOR EACH ROW EXECUTE FUNCTION bursar.check_refund_bounds();
