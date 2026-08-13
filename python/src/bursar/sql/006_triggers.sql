-- Migration: 006_triggers.sql
-- Purpose: Attach lifecycle, accounting, projection, outbox, timestamp, and
--   refund invariant functions to their owning tables.
-- Depends on: 005_constraints.sql.
-- Security: Makes protected mutation paths and cross-row guards unavoidable.

-- Catalog lifecycle and immutable projections

-- Attach one ownership guard to every tenant-bearing parent table. Partition
-- children inherit the parent trigger when pg_partman creates them later.
DO $$
DECLARE
    v_table record;
BEGIN
    FOR v_table IN
        SELECT table_info.oid::regclass AS table_name
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
        EXECUTE format(
            'CREATE TRIGGER tenant_id_immutable '
            'BEFORE UPDATE OF tenant_id ON %s '
            'FOR EACH ROW EXECUTE FUNCTION bursar.reject_tenant_id_change()',
            v_table.table_name
        );
    END LOOP;
END
$$;

-- Govern revision state/delete operations at the row boundary so callers cannot
-- bypass activation serialization or erase published catalog history.

CREATE TRIGGER catalog_revision_state
BEFORE UPDATE ON bursar.catalog_revisions
FOR EACH ROW EXECUTE FUNCTION bursar.one_active_catalog_revision();

CREATE TRIGGER catalog_revision_delete
BEFORE DELETE ON bursar.catalog_revisions
FOR EACH ROW EXECUTE FUNCTION bursar.reject_revision_delete();

-- Treat the following catalog tables as revision snapshots: updates/deletes are
-- rejected, while insert validators keep typed columns aligned with source JSON
-- and provider lookups stable across publishes.
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

CREATE TRIGGER catalog_plan_allowed_operations_validate
BEFORE INSERT ON bursar.catalog_plans
FOR EACH ROW
EXECUTE FUNCTION bursar.validate_catalog_plan_allowed_operations();

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

CREATE TRIGGER catalog_auto_recharge_policy_validate
BEFORE INSERT ON bursar.catalog_auto_recharge_policies
FOR EACH ROW
EXECUTE FUNCTION bursar.validate_catalog_auto_recharge_policy();

CREATE TRIGGER catalog_provider_ref_immutable
BEFORE UPDATE OR DELETE ON bursar.catalog_provider_refs
FOR EACH ROW EXECUTE FUNCTION bursar.reject_catalog_projection_mutation();

CREATE TRIGGER catalog_provider_ref_validate
BEFORE INSERT ON bursar.catalog_provider_refs
FOR EACH ROW EXECUTE FUNCTION bursar.validate_catalog_provider_ref();

-- Calendar-policy time zones are validated as one cohesive trigger family so
-- every stored window can be evaluated identically by PostgreSQL and workers.
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

-- Ledger and lot provenance

-- Protect the financial ledger and lot provenance as internally authored
-- accounting state; insert guards serialize and cap every provenance edge.
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

-- Usage, quota, and grant facts

-- Keep durable usage facts immutable and project payload-backed PostgreSQL usage
-- after its dimensional payload row is inserted in the same transaction.
CREATE TRIGGER usage_charges_append_only
BEFORE UPDATE OR DELETE ON bursar.credit_usage_charges
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER usage_charge_projection
AFTER INSERT ON bursar.usage_charge_payloads
FOR EACH ROW EXECUTE FUNCTION bursar.project_usage_charge();

-- Validate immutable quota measurements before insert, then publish both
-- measurements and notification boundaries through the transactional outbox.
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

-- Grant qualification and award execution are financial receipts; prohibit
-- later mutation so catalog changes cannot rewrite already-issued credits.
CREATE TRIGGER grant_events_append_only
BEFORE UPDATE OR DELETE ON bursar.grant_program_events
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

CREATE TRIGGER grant_award_executions_append_only
BEFORE UPDATE OR DELETE ON bursar.grant_award_executions
FOR EACH ROW EXECUTE FUNCTION bursar.require_internal_mutation();

-- Mutable operational lifecycle

-- Maintain database-authored timestamps across mutable state tables, archive
-- replaced plan assignments, and rearm recharge only on qualifying balance rises.
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

-- Payment and webhook status hooks additionally reject invalid provider state
-- regressions and enqueue each newly completed PostgreSQL-backed webhook once.
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

-- Refund lifecycle validation is paired with the common timestamp family so a
-- provider outcome is terminal while idempotent same-state updates remain safe.
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

-- Refund accounting bounds

-- Apply the same locked monetary/proportional-credit bound function to refund
-- headers and their grant allocations, closing races at both write boundaries.
CREATE TRIGGER refund_bounds
BEFORE INSERT OR UPDATE ON bursar.billing_refunds
FOR EACH ROW EXECUTE FUNCTION bursar.check_refund_bounds();

CREATE TRIGGER refund_grant_bounds
BEFORE INSERT OR UPDATE ON bursar.billing_refund_grants
FOR EACH ROW EXECUTE FUNCTION bursar.check_refund_bounds();
