---
title: Database schema
sidebar_label: Database schema
description: Generated ERD for Bursar's PostgreSQL schema.
---

<!-- Generated from PostgreSQL by docs/scripts/generate_erd.py; do not edit manually. -->

# Database schema

This diagram is generated from the migrated PostgreSQL catalog. Each line is a
single foreign-key constraint; its label groups the child and parent columns in
key order. The symbols show optionality and whether the child key is unique.
Partition children are omitted because their parent defines the logical table.
Regenerate it with `BURSAR_ERD_DATABASE_URL` pointing to a disposable database
migrated with `bursar migrate`, then run `bun run generate:erd` in `docs/`.

```mermaid
erDiagram
    account_plan_assignment_history {
        uuid tenant_id FK
        bigint id PK
        uuid assignment_id
        uuid account_id FK
        uuid plan_id FK
        uuid catalog_revision_id FK
        text plan_key FK
        boolean catalog_revision_pinned
        text source_type
        uuid source_id
        timestamp_with_time_zone starts_at
        timestamp_with_time_zone ends_at
        timestamp_with_time_zone replaced_at
        text replacement_reason
    }
    account_plan_assignments {
        uuid tenant_id FK
        uuid account_id PK,FK
        uuid assignment_id
        uuid plan_id FK
        uuid catalog_revision_id FK
        text plan_key FK
        boolean catalog_revision_pinned
        text source_type
        uuid source_id
        timestamp_with_time_zone starts_at
        timestamp_with_time_zone ends_at
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    allowance_windows {
        uuid tenant_id FK
        uuid id PK
        uuid account_id FK
        uuid plan_id FK
        uuid catalog_revision_id FK
        text allowance_key
        timestamp_with_time_zone window_start
        timestamp_with_time_zone window_end
        text period_unit
        integer period_count
        text period_anchor
        text period_timezone
        bursar_credit_numeric allowance
        bursar_credit_numeric reserved
        bursar_credit_numeric consumed
        jsonb policy_snapshot
    }
    billing_auto_recharge_attempts {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider
        text provider_environment
        text idempotency_key
        bursar_recharge_attempt_status state
        uuid topup_id FK
        uuid catalog_revision_id FK
        integer quantity
        timestamp_with_time_zone window_start
        timestamp_with_time_zone window_end
        bigint quoted_amount_minor
        text currency
        text provider_attempt_id
        text failure_code
        text failure_message
        jsonb metadata
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_auto_recharge_profiles {
        uuid tenant_id PK,FK
        uuid subject_id PK,FK
        boolean enabled
        boolean armed
        text state
        text provider
        text provider_environment PK
        uuid catalog_revision_id FK
        uuid topup_id FK
        integer quantity
        bursar_credit_numeric threshold
        bursar_credit_numeric rearm_above
        integer max_charges_per_window
        bigint max_charge_minor
        integer cooldown_seconds
        integer max_consecutive_failures
        integer consecutive_failures
        text window_unit
        integer window_count
        text window_anchor
        text window_timezone
        timestamp_with_time_zone last_attempt_at
        timestamp_with_time_zone updated_at
    }
    billing_checkout_intents {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider
        text provider_environment
        text checkout_kind
        text product_key
        text region
        uuid catalog_revision_id FK
        text operation_key
        bytea request_digest
        text status
        text provider_session_id
        text checkout_url
        timestamp_with_time_zone expires_at
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_credit_grants {
        uuid tenant_id FK
        uuid id PK
        uuid payment_id FK
        uuid subject_id FK
        uuid topup_id FK
        uuid subscription_id FK
        uuid catalog_revision_id FK
        text grant_key
        bursar_credit_numeric configured_credits
        integer quantity
        jsonb expiry_policy_snapshot
        uuid ledger_entry_id FK
        uuid billing_event_id FK
        timestamp_with_time_zone created_at
    }
    billing_customers {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider
        text provider_environment
        text provider_customer_id
        text email
        jsonb metadata
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_disputes {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider FK
        text provider_environment FK
        text provider_dispute_id
        uuid payment_id FK
        text status
        text reason
        timestamp_with_time_zone provider_updated_at
        jsonb metadata
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_entitlement_sources {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider_environment FK
        uuid subscription_id FK
        boolean selected
        timestamp_with_time_zone selected_at
        timestamp_with_time_zone deselected_at
        timestamp_with_time_zone created_at
    }
    billing_event_payloads {
        uuid tenant_id FK
        uuid event_id PK,FK
        timestamp_with_time_zone received_at PK,FK
        jsonb envelope
        timestamp_with_time_zone created_at
    }
    billing_events {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider
        text provider_environment
        text provider_event_id
        text event_type
        bytea envelope_digest
        timestamp_with_time_zone payload_received_at
        timestamp_with_time_zone payload_archived_at
        text payload_object_key
        text payload_object_version
        bursar_billing_event_status status
        integer attempt_count
        uuid claim_token
        timestamp_with_time_zone claim_expires_at
        text last_error
        timestamp_with_time_zone completed_at
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_invoices {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider FK
        text provider_environment FK
        text provider_invoice_id
        uuid subscription_id FK
        text status
        bigint amount_due_minor
        bigint amount_paid_minor
        text currency
        timestamp_with_time_zone period_start
        timestamp_with_time_zone period_end
        timestamp_with_time_zone provider_updated_at
        jsonb metadata
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_payments {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        uuid topup_id FK
        text provider
        text provider_environment
        text provider_payment_id
        text provider_invoice_id
        bigint amount_minor
        bigint tax_minor
        text currency
        text purpose
        bursar_billing_payment_status status
        timestamp_with_time_zone provider_updated_at
        timestamp_with_time_zone status_changed_at
        jsonb metadata
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_preferences {
        uuid tenant_id PK,FK
        uuid subject_id PK,FK
        boolean auto_recharge
        boolean overage_protection
        boolean email_notifications
        boolean usage_alerts
        boolean invoice_reminders
        timestamp_with_time_zone updated_at
    }
    billing_refund_grants {
        uuid tenant_id FK
        uuid refund_id PK,FK
        uuid grant_id PK,FK
        bigint amount_minor
        bursar_credit_numeric credit_amount
        uuid ledger_entry_id FK
    }
    billing_refunds {
        uuid tenant_id FK
        uuid id PK
        uuid payment_id FK
        text provider
        text provider_environment
        text provider_refund_id
        bigint amount_minor
        text currency
        text status
        text reason
        jsonb metadata
        timestamp_with_time_zone provider_updated_at
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_subscription_changes {
        uuid tenant_id FK
        bigint id PK
        uuid subscription_id FK
        text state
        uuid from_offer_id FK
        uuid from_catalog_revision_id FK
        uuid to_offer_id FK
        uuid to_catalog_revision_id FK
        timestamp_with_time_zone effective_at
        text effective_behavior
        text proration_behavior
        text provider_operation_id
        text idempotency_key
        text region
        text error_message
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    billing_subscription_conflicts {
        uuid tenant_id FK
        bigint id PK
        uuid subject_id FK
        text provider
        text provider_environment
        text duplicate_provider_subscription_id
        uuid existing_subscription_id FK
        uuid billing_event_id FK
        jsonb metadata
        timestamp_with_time_zone created_at
    }
    billing_subscriptions {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider
        text provider_environment
        text provider_subscription_id
        text provider_customer_id
        uuid offer_id FK
        uuid catalog_revision_id FK
        bursar_billing_subscription_status status
        timestamp_with_time_zone current_period_start
        timestamp_with_time_zone current_period_end
        timestamp_with_time_zone trial_end
        timestamp_with_time_zone cancel_at
        boolean cancel_at_period_end
        timestamp_with_time_zone ended_at
        timestamp_with_time_zone grace_ends_at
        timestamp_with_time_zone grace_expired_at
        timestamp_with_time_zone provider_updated_at
        timestamp_with_time_zone entitlement_provider_updated_at
        uuid entitlement_billing_event_id FK
        text entitlement_reconciliation_outcome
        timestamp_with_time_zone status_changed_at
        jsonb metadata
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    catalog_activation_history {
        uuid tenant_id FK
        bigint id PK
        uuid catalog_revision_id FK
        timestamp_with_time_zone activated_at
        timestamp_with_time_zone deactivated_at
        text label
    }
    catalog_admission_operation_policies {
        uuid tenant_id FK
        uuid catalog_revision_id PK,FK
        text admission_policy_key PK,FK
        text operation_key PK,FK
        integer max_in_flight
    }
    catalog_admission_policies {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text policy_key
        integer max_in_flight
        jsonb definition
    }
    catalog_auto_recharge_policies {
        uuid tenant_id FK
        uuid catalog_revision_id PK,FK
        text eligible_topup_keys
        text default_topup_key FK
        integer quantity_min
        integer quantity_max
        integer quantity
        bursar_credit_numeric balance_min
        bursar_credit_numeric balance_max
        bursar_credit_numeric balance_below
        bursar_credit_numeric rearm_above
        integer max_purchases
        bigint max_charge_minor
        integer cooldown_seconds
        integer max_consecutive_failures
        text failure_action
        text period_unit
        integer period_count
        text period_anchor
        text period_timezone
        jsonb definition
    }
    catalog_buckets {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text bucket_key
        text label
        integer priority
        jsonb definition
        jsonb expiry_policy
        text expiry_type
        boolean expires
        text expires_after_unit
        integer expires_after_count
        text expires_after_anchor
        text expires_after_timezone
        timestamp_with_time_zone fixed_expires_at
        boolean allow_overdraft
        boolean is_default
    }
    catalog_credit_policies {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text policy_key
        text policy_type
        bursar_credit_numeric credit_limit
        jsonb definition
    }
    catalog_entitlement_features {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text feature_key
        text value_type
        jsonb default_value
        jsonb definition
    }
    catalog_grant_awards {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        uuid grant_program_id FK
        integer award_index
        text recipient
        bursar_credit_numeric amount
        text bucket_key FK
        jsonb expiry_policy
        jsonb definition
    }
    catalog_grant_programs {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text program_key
        text trigger_type
        jsonb availability
        jsonb eligibility
        integer max_awards_per_subject
        text idempotency_scope
        jsonb definition
    }
    catalog_offers {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text offer_key
        text display_name
        text description
        integer sort_order
        jsonb availability
        bigint amount_minor
        text currency
        text tax_behavior
        text plan_key FK
        text billing_unit
        integer billing_count
        jsonb trial_policy
        bursar_credit_numeric cycle_grant_amount
        text cycle_grant_bucket_key FK
        text cycle_grant_renewal
        jsonb cycle_grant_expiry_policy
        jsonb definition
    }
    catalog_operations {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text operation_key
        jsonb measures
        jsonb dimensions
        jsonb definition
    }
    catalog_plan_features {
        uuid tenant_id FK
        uuid catalog_revision_id PK,FK
        text plan_key PK,FK
        text feature_key PK,FK
        jsonb feature_value
    }
    catalog_plan_quotas {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text plan_key FK
        text quota_key
        text operation_key FK
        text measure_key
        bursar_credit_numeric quota_limit
        jsonb window_policy
        text enforcement
        integer emit_at_percent
        jsonb definition
    }
    catalog_plans {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text plan_key
        text display_name
        text description
        text rate_card FK
        text allowed_operations
        text credit_policy_key FK
        text admission_policy_key FK
        text default_rollout
        bursar_credit_numeric credit_allowance_amount
        integer credit_allowance_priority
        text credit_allowance_bucket FK
        text credit_allowance_reset_unit
        integer credit_allowance_reset_count
        text credit_allowance_reset_anchor
        text credit_allowance_reset_timezone
        jsonb definition
    }
    catalog_provider_refs {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text provider
        text provider_environment
        text lookup_type
        text lookup_value
        text object_type
        text object_key
    }
    catalog_rate_cards {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text rate_card_key
        text extends_key FK
        jsonb definition
    }
    catalog_revisions {
        uuid tenant_id FK
        uuid id PK
        bigint revision_no
        integer yaml_schema_version
        jsonb source_document
        bytea digest
        bursar_catalog_revision_status status
        text label
        timestamp_with_time_zone created_at
        timestamp_with_time_zone published_at
        timestamp_with_time_zone activated_at
        timestamp_with_time_zone retired_at
    }
    catalog_topups {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        text topup_key
        text display_name
        text description
        integer sort_order
        jsonb availability
        bigint amount_minor
        text currency
        text tax_behavior
        bursar_credit_numeric credits_per_unit
        text bucket_key FK
        integer min_quantity
        integer max_quantity
        integer default_quantity
        jsonb expiry_policy
        text lot_behavior
        jsonb definition
    }
    credit_accounts {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text account_kind
        bursar_credit_numeric balance
        bigint version
        timestamp_with_time_zone updated_at
        timestamp_with_time_zone created_at
    }
    credit_debt_repayments {
        uuid tenant_id FK
        uuid ledger_entry_id PK,FK
        uuid account_id FK
        bursar_credit_numeric amount
        timestamp_with_time_zone created_at
    }
    credit_lease_quota_reservations {
        uuid tenant_id FK
        uuid lease_id PK,FK
        uuid catalog_quota_id PK,FK
        uuid quota_window_id FK
        bursar_credit_numeric amount
        timestamp_with_time_zone window_start
        timestamp_with_time_zone window_end
        timestamp_with_time_zone released_at
        timestamp_with_time_zone created_at
    }
    credit_leases {
        uuid tenant_id FK
        uuid id PK
        uuid account_id FK
        text operation
        text feature
        jsonb measures
        jsonb dimensions
        jsonb policy_snapshot
        jsonb metadata
        uuid catalog_revision_id FK
        uuid plan_id FK
        bursar_credit_numeric reserved_amount
        bursar_credit_numeric reserved_allowance
        uuid allowance_window_id FK
        bursar_credit_numeric minimum_balance
        integer max_concurrent
        timestamp_with_time_zone expires_at
        bursar_lease_status status
        text idempotency_key
        bytea request_digest
        bursar_credit_numeric settled_amount
        text settlement_idempotency_key
        bytea settlement_request_digest
        uuid ledger_entry_id FK
        uuid usage_charge_id FK
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    credit_ledger_entries {
        uuid tenant_id FK
        uuid id PK
        uuid account_id FK
        bursar_ledger_entry_kind kind
        bursar_credit_numeric amount
        bursar_credit_numeric balance_after
        uuid reference_entry_id FK
        uuid catalog_revision_id FK
        text idempotency_key
        bytea request_digest
        text operation
        jsonb metadata
        timestamp_with_time_zone created_at
    }
    credit_lot_allocations {
        uuid tenant_id FK
        uuid id PK
        uuid debit_entry_id FK
        uuid lot_id FK
        bursar_credit_numeric amount
        text allocation_kind
        timestamp_with_time_zone created_at
    }
    credit_lot_restorations {
        uuid tenant_id FK
        uuid id PK
        uuid refund_entry_id FK
        uuid original_allocation_id FK
        uuid lot_id FK
        bursar_credit_numeric amount
        timestamp_with_time_zone created_at
    }
    credit_lot_source_allocations {
        uuid tenant_id FK
        uuid id PK
        uuid lot_allocation_id FK
        uuid lot_source_id FK
        bursar_credit_numeric amount
        timestamp_with_time_zone created_at
    }
    credit_lot_source_restorations {
        uuid tenant_id FK
        uuid id PK
        uuid lot_restoration_id FK
        uuid source_allocation_id FK
        bursar_credit_numeric amount
        timestamp_with_time_zone created_at
    }
    credit_lot_sources {
        uuid tenant_id FK
        uuid id PK
        uuid lot_id FK
        uuid ledger_entry_id FK
        bursar_credit_numeric amount
        text source_type
        uuid source_id
        timestamp_with_time_zone created_at
    }
    credit_lots {
        uuid tenant_id FK
        uuid id PK
        uuid account_id FK
        uuid source_entry_id FK
        uuid catalog_revision_id FK
        text bucket_key FK
        integer priority
        bursar_credit_numeric granted
        bursar_credit_numeric consumed
        timestamp_with_time_zone expires_at
        jsonb expiry_policy_snapshot
        text source_type
        uuid source_id
        timestamp_with_time_zone created_at
    }
    credit_plan_migrations {
        uuid tenant_id FK
        uuid id PK
        uuid from_plan_id FK
        uuid to_plan_id FK
        text strategy
        timestamp_with_time_zone effective_at
        uuid cursor_account_id
        integer migrated_count
        text status
        text last_error
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    credit_team_members {
        uuid tenant_id FK
        uuid team_id PK,FK
        uuid subject_id PK,FK
        text role
        bursar_credit_numeric spend_cap
        timestamp_with_time_zone created_at
        timestamp_with_time_zone left_at
    }
    credit_team_usage_charges {
        uuid tenant_id FK
        uuid id PK
        uuid team_id FK
        uuid subject_id FK
        uuid ledger_entry_id FK
        text operation
        bursar_credit_numeric amount
        jsonb metadata
        text idempotency_key
        bytea request_digest
        timestamp_with_time_zone created_at
    }
    credit_teams {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text name
        text creation_idempotency_key
        bytea creation_request_digest
        timestamp_with_time_zone created_at
    }
    credit_unallocated_debits {
        uuid tenant_id FK
        uuid ledger_entry_id PK,FK
        uuid account_id FK
        bursar_credit_numeric amount
        text reason
        timestamp_with_time_zone created_at
    }
    credit_usage_charges {
        uuid tenant_id FK
        uuid id PK
        uuid account_id FK
        text operation
        timestamp_with_time_zone event_at
        bursar_credit_numeric requested
        bursar_credit_numeric charged
        bursar_credit_numeric allowance_requested
        bursar_credit_numeric allowance_covered
        text billing_disposition
        uuid catalog_revision_id FK
        uuid plan_id FK
        text rate_card_key
        uuid ledger_entry_id FK
        uuid correction_of_charge_id
        text idempotency_key
        bytea request_digest
        timestamp_with_time_zone created_at
    }
    event_outbox {
        uuid tenant_id FK
        bigint id PK
        text topic
        text aggregate_type
        uuid aggregate_id
        text idempotency_key
        smallint payload_version
        jsonb payload
        text status
        integer attempt_count
        timestamp_with_time_zone available_at
        uuid claim_token
        timestamp_with_time_zone claim_expires_at
        text last_error
        timestamp_with_time_zone delivered_at
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    external_identities {
        uuid tenant_id FK
        uuid id PK
        uuid subject_id FK
        text provider
        text provider_environment
        text external_subject
        timestamp_with_time_zone created_at
    }
    grant_award_executions {
        uuid tenant_id FK
        uuid id PK
        uuid grant_event_id FK
        uuid catalog_grant_award_id FK
        uuid catalog_revision_id FK
        uuid recipient_subject_id FK
        uuid ledger_entry_id FK
        timestamp_with_time_zone granted_at
    }
    grant_program_events {
        uuid tenant_id FK
        uuid id PK
        uuid catalog_revision_id FK
        uuid grant_program_id FK
        text program_key
        uuid subject_id FK
        text event_key
        text idempotency_scope
        uuid referrer_subject_id FK
        jsonb metadata
        timestamp_with_time_zone occurred_at
        timestamp_with_time_zone created_at
    }
    plan_assignment_changes {
        uuid tenant_id FK
        bigint id PK
        uuid account_id FK
        uuid from_plan_id FK
        uuid to_plan_id FK
        text change_kind
        boolean pin_overridden
        text strategy
        timestamp_with_time_zone effective_at
        text state
        text reason
        text error_message
        timestamp_with_time_zone created_at
        timestamp_with_time_zone applied_at
    }
    quota_events {
        uuid tenant_id FK
        uuid id PK
        uuid quota_window_id FK
        uuid usage_charge_id FK
        text event_type
        integer threshold_percent
        text idempotency_key
        timestamp_with_time_zone created_at
    }
    quota_usage_events {
        uuid tenant_id FK
        uuid id PK
        uuid account_id FK
        uuid plan_id FK
        uuid catalog_revision_id FK
        uuid catalog_quota_id FK
        text quota_key
        text operation_key
        text measure_key
        bursar_credit_numeric amount
        timestamp_with_time_zone event_at
        uuid usage_charge_id FK
        uuid correction_of_event_id FK
        text idempotency_key
        bytea request_digest
        jsonb metadata
        timestamp_with_time_zone created_at
    }
    quota_windows {
        uuid tenant_id FK
        uuid id PK
        uuid account_id FK
        uuid plan_id FK
        uuid catalog_revision_id FK
        text quota_key
        text operation_key
        text measure_key
        timestamp_with_time_zone window_start
        timestamp_with_time_zone window_end
        bursar_credit_numeric quota_limit
        bursar_credit_numeric reserved
        bursar_credit_numeric consumed
        text enforcement
        jsonb policy_snapshot
        timestamp_with_time_zone created_at
    }
    schema_migrations {
        text version PK
        text checksum
        timestamp_with_time_zone applied_at
    }
    storage_settings {
        boolean singleton PK
        integer usage_payload_retention_days
        integer quota_event_retention_days
        integer quota_max_lateness_seconds
        integer quota_correction_window_days
        integer quota_retention_safety_days
        integer billing_payload_retention_days
        integer quota_notification_retention_days
        integer terminal_lease_payload_retention_days
        integer usage_rollup_retention_days
        integer outbox_delivered_retention_days
        integer outbox_max_retention_days
        integer maintenance_interval_seconds
        integer maintenance_batch_size
        integer maintenance_lock_timeout_ms
        timestamp_with_time_zone last_maintenance_at
        timestamp_with_time_zone updated_at
    }
    subjects {
        uuid tenant_id PK,FK
        uuid id PK
        timestamp_with_time_zone pseudonymized_at
        timestamp_with_time_zone created_at
    }
    tenant_catalog_counters {
        uuid tenant_id PK,FK
        bigint next_revision_no
    }
    tenants {
        uuid id PK
        text slug
        text display_name
        text provider_environment
        text usage_backend
        text usage_cutover_target
        timestamp_with_time_zone usage_cutover_cutoff
        text status
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
    }
    usage_backend_cutover_items {
        uuid tenant_id PK,FK
        uuid charge_id PK
        timestamp_with_time_zone event_at PK
        text digest
    }
    usage_charge_payloads {
        uuid tenant_id FK
        uuid charge_id PK,FK
        timestamp_with_time_zone event_at PK,FK
        jsonb measures
        text feature
        text model
        text region
        jsonb dimensions
        jsonb metadata
        jsonb pricing_snapshot
        timestamp_with_time_zone created_at
    }
    usage_daily_rollups {
        uuid tenant_id FK
        date usage_day PK
        uuid account_id PK,FK
        text operation PK
        text model_key PK
        text region_key PK
        smallint rollup_shard PK
        bursar_credit_numeric charged
        bursar_credit_numeric allowance_covered
        bigint charge_count
        timestamp_with_time_zone updated_at
    }
    usage_rollup_contributions {
        uuid tenant_id PK,FK
        uuid charge_id PK,FK
        timestamp_with_time_zone event_at
    }
    credit_accounts ||--o{ account_plan_assignment_history : "tenant_id, account_id to tenant_id, id"
    tenants ||--o{ account_plan_assignment_history : "tenant_id to id"
    catalog_plans ||--o{ account_plan_assignment_history : "tenant_id, plan_id, catalog_revision_id, plan_key to tenant_id, id, catalog_revision_id, plan_key"
    credit_accounts ||--o| account_plan_assignments : "tenant_id, account_id to tenant_id, id"
    tenants ||--o{ account_plan_assignments : "tenant_id to id"
    catalog_plans ||--o{ account_plan_assignments : "tenant_id, plan_id, catalog_revision_id, plan_key to tenant_id, id, catalog_revision_id, plan_key"
    credit_accounts ||--o{ allowance_windows : "tenant_id, account_id to tenant_id, id"
    tenants ||--o{ allowance_windows : "tenant_id to id"
    catalog_plans ||--o{ allowance_windows : "tenant_id, plan_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    tenants ||--o{ billing_auto_recharge_attempts : "tenant_id to id"
    subjects ||--o{ billing_auto_recharge_attempts : "tenant_id, subject_id to tenant_id, id"
    catalog_topups ||--o{ billing_auto_recharge_attempts : "tenant_id, topup_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    catalog_revisions |o--o{ billing_auto_recharge_profiles : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ billing_auto_recharge_profiles : "tenant_id to id"
    subjects ||--o{ billing_auto_recharge_profiles : "tenant_id, subject_id to tenant_id, id"
    catalog_topups |o--o{ billing_auto_recharge_profiles : "tenant_id, topup_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    catalog_revisions ||--o{ billing_checkout_intents : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ billing_checkout_intents : "tenant_id to id"
    subjects ||--o{ billing_checkout_intents : "tenant_id, subject_id to tenant_id, id"
    billing_events |o--o{ billing_credit_grants : "tenant_id, billing_event_id to tenant_id, id"
    tenants ||--o{ billing_credit_grants : "tenant_id to id"
    credit_ledger_entries |o--o{ billing_credit_grants : "tenant_id, ledger_entry_id to tenant_id, id"
    billing_payments |o--o{ billing_credit_grants : "tenant_id, payment_id, subject_id to tenant_id, id, subject_id"
    subjects ||--o{ billing_credit_grants : "tenant_id, subject_id to tenant_id, id"
    billing_subscriptions |o--o{ billing_credit_grants : "tenant_id, subscription_id, subject_id to tenant_id, id, subject_id"
    catalog_topups |o--o{ billing_credit_grants : "tenant_id, topup_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    tenants ||--o{ billing_customers : "tenant_id to id"
    subjects ||--o{ billing_customers : "tenant_id, subject_id to tenant_id, id"
    tenants ||--o{ billing_disputes : "tenant_id to id"
    billing_payments |o--o{ billing_disputes : "tenant_id, payment_id, subject_id, provider, provider_environment to tenant_id, id, subject_id, provider, provider_environment"
    subjects |o--o{ billing_disputes : "tenant_id, subject_id to tenant_id, id"
    tenants ||--o{ billing_entitlement_sources : "tenant_id to id"
    subjects ||--o{ billing_entitlement_sources : "tenant_id, subject_id to tenant_id, id"
    billing_subscriptions ||--o| billing_entitlement_sources : "tenant_id, subscription_id, subject_id, provider_environment to tenant_id, id, subject_id, provider_environment"
    billing_events ||--o| billing_event_payloads : "tenant_id, event_id, received_at to tenant_id, id, payload_received_at"
    tenants ||--o{ billing_event_payloads : "tenant_id to id"
    tenants ||--o{ billing_events : "tenant_id to id"
    subjects |o--o{ billing_events : "tenant_id, subject_id to tenant_id, id"
    tenants ||--o{ billing_invoices : "tenant_id to id"
    subjects ||--o{ billing_invoices : "tenant_id, subject_id to tenant_id, id"
    billing_subscriptions |o--o{ billing_invoices : "tenant_id, subscription_id, subject_id, provider, provider_environment to tenant_id, id, subject_id, provider, provider_environment"
    tenants ||--o{ billing_payments : "tenant_id to id"
    subjects ||--o{ billing_payments : "tenant_id, subject_id to tenant_id, id"
    catalog_topups |o--o{ billing_payments : "tenant_id, topup_id to tenant_id, id"
    tenants ||--o{ billing_preferences : "tenant_id to id"
    subjects ||--o| billing_preferences : "tenant_id, subject_id to tenant_id, id"
    tenants ||--o{ billing_refund_grants : "tenant_id to id"
    billing_credit_grants ||--o{ billing_refund_grants : "tenant_id, grant_id to tenant_id, id"
    credit_ledger_entries |o--o{ billing_refund_grants : "tenant_id, ledger_entry_id to tenant_id, id"
    billing_refunds ||--o{ billing_refund_grants : "tenant_id, refund_id to tenant_id, id"
    tenants ||--o{ billing_refunds : "tenant_id to id"
    billing_payments ||--o{ billing_refunds : "tenant_id, payment_id to tenant_id, id"
    tenants ||--o{ billing_subscription_changes : "tenant_id to id"
    catalog_offers ||--o{ billing_subscription_changes : "tenant_id, from_offer_id, from_catalog_revision_id to tenant_id, id, catalog_revision_id"
    billing_subscriptions ||--o{ billing_subscription_changes : "tenant_id, subscription_id to tenant_id, id"
    catalog_offers ||--o{ billing_subscription_changes : "tenant_id, to_offer_id, to_catalog_revision_id to tenant_id, id, catalog_revision_id"
    billing_events |o--o{ billing_subscription_conflicts : "tenant_id, billing_event_id to tenant_id, id"
    billing_subscriptions |o--o{ billing_subscription_conflicts : "tenant_id, existing_subscription_id to tenant_id, id"
    tenants ||--o{ billing_subscription_conflicts : "tenant_id to id"
    subjects |o--o{ billing_subscription_conflicts : "tenant_id, subject_id to tenant_id, id"
    billing_events |o--o{ billing_subscriptions : "tenant_id, entitlement_billing_event_id to tenant_id, id"
    tenants ||--o{ billing_subscriptions : "tenant_id to id"
    catalog_offers ||--o{ billing_subscriptions : "tenant_id, offer_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    subjects ||--o{ billing_subscriptions : "tenant_id, subject_id to tenant_id, id"
    catalog_revisions ||--o{ catalog_activation_history : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_activation_history : "tenant_id to id"
    catalog_admission_policies ||--o{ catalog_admission_operation_policies : "tenant_id, catalog_revision_id, admission_policy_key to tenant_id, catalog_revision_id, policy_key"
    catalog_operations ||--o{ catalog_admission_operation_policies : "tenant_id, catalog_revision_id, operation_key to tenant_id, catalog_revision_id, operation_key"
    tenants ||--o{ catalog_admission_operation_policies : "tenant_id to id"
    catalog_revisions ||--o{ catalog_admission_policies : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_admission_policies : "tenant_id to id"
    catalog_topups ||--o| catalog_auto_recharge_policies : "tenant_id, catalog_revision_id, default_topup_key to tenant_id, catalog_revision_id, topup_key"
    catalog_revisions ||--o| catalog_auto_recharge_policies : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_auto_recharge_policies : "tenant_id to id"
    catalog_revisions ||--o{ catalog_buckets : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_buckets : "tenant_id to id"
    catalog_revisions ||--o{ catalog_credit_policies : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_credit_policies : "tenant_id to id"
    catalog_revisions ||--o{ catalog_entitlement_features : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_entitlement_features : "tenant_id to id"
    catalog_buckets ||--o{ catalog_grant_awards : "tenant_id, catalog_revision_id, bucket_key to tenant_id, catalog_revision_id, bucket_key"
    tenants ||--o{ catalog_grant_awards : "tenant_id to id"
    catalog_grant_programs ||--o{ catalog_grant_awards : "tenant_id, grant_program_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    catalog_revisions ||--o{ catalog_grant_programs : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_grant_programs : "tenant_id to id"
    catalog_revisions ||--o{ catalog_offers : "tenant_id, catalog_revision_id to tenant_id, id"
    catalog_buckets |o--o{ catalog_offers : "tenant_id, catalog_revision_id, cycle_grant_bucket_key to tenant_id, catalog_revision_id, bucket_key"
    catalog_plans ||--o{ catalog_offers : "tenant_id, catalog_revision_id, plan_key to tenant_id, catalog_revision_id, plan_key"
    tenants ||--o{ catalog_offers : "tenant_id to id"
    catalog_revisions ||--o{ catalog_operations : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_operations : "tenant_id to id"
    catalog_entitlement_features ||--o{ catalog_plan_features : "tenant_id, catalog_revision_id, feature_key to tenant_id, catalog_revision_id, feature_key"
    catalog_plans ||--o{ catalog_plan_features : "tenant_id, catalog_revision_id, plan_key to tenant_id, catalog_revision_id, plan_key"
    tenants ||--o{ catalog_plan_features : "tenant_id to id"
    catalog_operations ||--o{ catalog_plan_quotas : "tenant_id, catalog_revision_id, operation_key to tenant_id, catalog_revision_id, operation_key"
    catalog_plans ||--o{ catalog_plan_quotas : "tenant_id, catalog_revision_id, plan_key to tenant_id, catalog_revision_id, plan_key"
    tenants ||--o{ catalog_plan_quotas : "tenant_id to id"
    catalog_admission_policies |o--o{ catalog_plans : "tenant_id, catalog_revision_id, admission_policy_key to tenant_id, catalog_revision_id, policy_key"
    catalog_revisions ||--o{ catalog_plans : "tenant_id, catalog_revision_id to tenant_id, id"
    catalog_buckets |o--o{ catalog_plans : "tenant_id, catalog_revision_id, credit_allowance_bucket to tenant_id, catalog_revision_id, bucket_key"
    catalog_credit_policies |o--o{ catalog_plans : "tenant_id, catalog_revision_id, credit_policy_key to tenant_id, catalog_revision_id, policy_key"
    catalog_rate_cards |o--o{ catalog_plans : "tenant_id, catalog_revision_id, rate_card to tenant_id, catalog_revision_id, rate_card_key"
    tenants ||--o{ catalog_plans : "tenant_id to id"
    catalog_revisions ||--o{ catalog_provider_refs : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_provider_refs : "tenant_id to id"
    catalog_revisions ||--o{ catalog_rate_cards : "tenant_id, catalog_revision_id to tenant_id, id"
    catalog_rate_cards |o--o{ catalog_rate_cards : "tenant_id, catalog_revision_id, extends_key to tenant_id, catalog_revision_id, rate_card_key"
    tenants ||--o{ catalog_rate_cards : "tenant_id to id"
    tenants ||--o{ catalog_revisions : "tenant_id to id"
    catalog_buckets ||--o{ catalog_topups : "tenant_id, catalog_revision_id, bucket_key to tenant_id, catalog_revision_id, bucket_key"
    catalog_revisions ||--o{ catalog_topups : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ catalog_topups : "tenant_id to id"
    tenants ||--o{ credit_accounts : "tenant_id to id"
    subjects ||--o{ credit_accounts : "tenant_id, subject_id to tenant_id, id"
    credit_accounts ||--o{ credit_debt_repayments : "tenant_id, account_id to tenant_id, id"
    tenants ||--o{ credit_debt_repayments : "tenant_id to id"
    credit_ledger_entries ||--o| credit_debt_repayments : "tenant_id, ledger_entry_id to tenant_id, id"
    catalog_plan_quotas ||--o{ credit_lease_quota_reservations : "tenant_id, catalog_quota_id to tenant_id, id"
    tenants ||--o{ credit_lease_quota_reservations : "tenant_id to id"
    credit_leases ||--o{ credit_lease_quota_reservations : "tenant_id, lease_id to tenant_id, id"
    quota_windows |o--o{ credit_lease_quota_reservations : "tenant_id, quota_window_id to tenant_id, id"
    credit_accounts ||--o{ credit_leases : "tenant_id, account_id to tenant_id, id"
    allowance_windows |o--o{ credit_leases : "tenant_id, allowance_window_id to tenant_id, id"
    catalog_revisions ||--o{ credit_leases : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ credit_leases : "tenant_id to id"
    credit_ledger_entries |o--o{ credit_leases : "tenant_id, ledger_entry_id to tenant_id, id"
    catalog_plans |o--o{ credit_leases : "tenant_id, plan_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    credit_usage_charges |o--o{ credit_leases : "tenant_id, usage_charge_id to tenant_id, id"
    credit_accounts ||--o{ credit_ledger_entries : "tenant_id, account_id to tenant_id, id"
    catalog_revisions |o--o{ credit_ledger_entries : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ credit_ledger_entries : "tenant_id to id"
    credit_ledger_entries |o--o{ credit_ledger_entries : "tenant_id, reference_entry_id to tenant_id, id"
    credit_ledger_entries ||--o{ credit_lot_allocations : "tenant_id, debit_entry_id to tenant_id, id"
    tenants ||--o{ credit_lot_allocations : "tenant_id to id"
    credit_lots ||--o{ credit_lot_allocations : "tenant_id, lot_id to tenant_id, id"
    tenants ||--o{ credit_lot_restorations : "tenant_id to id"
    credit_lots ||--o{ credit_lot_restorations : "tenant_id, lot_id to tenant_id, id"
    credit_lot_allocations ||--o{ credit_lot_restorations : "tenant_id, original_allocation_id to tenant_id, id"
    credit_ledger_entries ||--o{ credit_lot_restorations : "tenant_id, refund_entry_id to tenant_id, id"
    tenants ||--o{ credit_lot_source_allocations : "tenant_id to id"
    credit_lot_allocations ||--o{ credit_lot_source_allocations : "tenant_id, lot_allocation_id to tenant_id, id"
    credit_lot_sources ||--o{ credit_lot_source_allocations : "tenant_id, lot_source_id to tenant_id, id"
    tenants ||--o{ credit_lot_source_restorations : "tenant_id to id"
    credit_lot_restorations ||--o{ credit_lot_source_restorations : "tenant_id, lot_restoration_id to tenant_id, id"
    credit_lot_source_allocations ||--o{ credit_lot_source_restorations : "tenant_id, source_allocation_id to tenant_id, id"
    tenants ||--o{ credit_lot_sources : "tenant_id to id"
    credit_ledger_entries ||--o| credit_lot_sources : "tenant_id, ledger_entry_id to tenant_id, id"
    credit_lots ||--o{ credit_lot_sources : "tenant_id, lot_id to tenant_id, id"
    credit_accounts ||--o{ credit_lots : "tenant_id, account_id to tenant_id, id"
    catalog_buckets ||--o{ credit_lots : "tenant_id, catalog_revision_id, bucket_key to tenant_id, catalog_revision_id, bucket_key"
    tenants ||--o{ credit_lots : "tenant_id to id"
    credit_ledger_entries ||--o| credit_lots : "tenant_id, source_entry_id to tenant_id, id"
    tenants ||--o{ credit_plan_migrations : "tenant_id to id"
    catalog_plans |o--o{ credit_plan_migrations : "tenant_id, from_plan_id to tenant_id, id"
    catalog_plans ||--o{ credit_plan_migrations : "tenant_id, to_plan_id to tenant_id, id"
    tenants ||--o{ credit_team_members : "tenant_id to id"
    subjects ||--o{ credit_team_members : "tenant_id, subject_id to tenant_id, id"
    credit_teams ||--o{ credit_team_members : "tenant_id, team_id to tenant_id, id"
    tenants ||--o{ credit_team_usage_charges : "tenant_id to id"
    credit_ledger_entries ||--o| credit_team_usage_charges : "tenant_id, ledger_entry_id to tenant_id, id"
    subjects ||--o{ credit_team_usage_charges : "tenant_id, subject_id to tenant_id, id"
    credit_teams ||--o{ credit_team_usage_charges : "tenant_id, team_id to tenant_id, id"
    credit_team_members ||--o{ credit_team_usage_charges : "tenant_id, team_id, subject_id to tenant_id, team_id, subject_id"
    tenants ||--o{ credit_teams : "tenant_id to id"
    subjects ||--o| credit_teams : "tenant_id, subject_id to tenant_id, id"
    credit_accounts ||--o{ credit_unallocated_debits : "tenant_id, account_id to tenant_id, id"
    tenants ||--o{ credit_unallocated_debits : "tenant_id to id"
    credit_ledger_entries ||--o| credit_unallocated_debits : "tenant_id, ledger_entry_id to tenant_id, id"
    credit_accounts ||--o{ credit_usage_charges : "tenant_id, account_id to tenant_id, id"
    catalog_revisions |o--o{ credit_usage_charges : "tenant_id, catalog_revision_id to tenant_id, id"
    tenants ||--o{ credit_usage_charges : "tenant_id to id"
    credit_ledger_entries |o--o{ credit_usage_charges : "tenant_id, ledger_entry_id to tenant_id, id"
    catalog_plans |o--o{ credit_usage_charges : "tenant_id, plan_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    tenants ||--o{ event_outbox : "tenant_id to id"
    tenants ||--o{ external_identities : "tenant_id to id"
    subjects ||--o{ external_identities : "tenant_id, subject_id to tenant_id, id"
    catalog_grant_awards ||--o{ grant_award_executions : "tenant_id, catalog_grant_award_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    tenants ||--o{ grant_award_executions : "tenant_id to id"
    grant_program_events ||--o{ grant_award_executions : "tenant_id, grant_event_id to tenant_id, id"
    credit_ledger_entries ||--o| grant_award_executions : "tenant_id, ledger_entry_id to tenant_id, id"
    subjects ||--o{ grant_award_executions : "tenant_id, recipient_subject_id to tenant_id, id"
    tenants ||--o{ grant_program_events : "tenant_id to id"
    catalog_grant_programs ||--o{ grant_program_events : "tenant_id, grant_program_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    subjects |o--o{ grant_program_events : "tenant_id, referrer_subject_id to tenant_id, id"
    subjects ||--o{ grant_program_events : "tenant_id, subject_id to tenant_id, id"
    credit_accounts ||--o{ plan_assignment_changes : "tenant_id, account_id to tenant_id, id"
    tenants ||--o{ plan_assignment_changes : "tenant_id to id"
    catalog_plans ||--o{ plan_assignment_changes : "tenant_id, from_plan_id to tenant_id, id"
    catalog_plans ||--o{ plan_assignment_changes : "tenant_id, to_plan_id to tenant_id, id"
    tenants ||--o{ quota_events : "tenant_id to id"
    quota_windows ||--o{ quota_events : "tenant_id, quota_window_id to tenant_id, id"
    credit_usage_charges |o--o{ quota_events : "tenant_id, usage_charge_id to tenant_id, id"
    credit_accounts ||--o{ quota_usage_events : "tenant_id, account_id to tenant_id, id"
    catalog_plan_quotas ||--o{ quota_usage_events : "tenant_id, catalog_quota_id to tenant_id, id"
    quota_usage_events |o--o{ quota_usage_events : "tenant_id, correction_of_event_id to tenant_id, id"
    tenants ||--o{ quota_usage_events : "tenant_id to id"
    catalog_plans ||--o{ quota_usage_events : "tenant_id, plan_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    credit_usage_charges |o--o{ quota_usage_events : "tenant_id, usage_charge_id to tenant_id, id"
    credit_accounts ||--o{ quota_windows : "tenant_id, account_id to tenant_id, id"
    tenants ||--o{ quota_windows : "tenant_id to id"
    catalog_plans ||--o{ quota_windows : "tenant_id, plan_id, catalog_revision_id to tenant_id, id, catalog_revision_id"
    tenants ||--o{ subjects : "tenant_id to id"
    tenants ||--o| tenant_catalog_counters : "tenant_id to id"
    tenants ||--o{ usage_backend_cutover_items : "tenant_id to id"
    credit_usage_charges ||--o| usage_charge_payloads : "tenant_id, charge_id, event_at to tenant_id, id, event_at"
    tenants ||--o{ usage_charge_payloads : "tenant_id to id"
    credit_accounts ||--o{ usage_daily_rollups : "tenant_id, account_id to tenant_id, id"
    tenants ||--o{ usage_daily_rollups : "tenant_id to id"
    credit_usage_charges ||--o| usage_rollup_contributions : "tenant_id, charge_id to tenant_id, id"
    tenants ||--o{ usage_rollup_contributions : "tenant_id to id"
```
