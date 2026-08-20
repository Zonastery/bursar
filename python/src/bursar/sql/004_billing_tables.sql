-- Migration: 004_billing_tables.sql
-- Purpose: Define provider-facing customer, subscription, payment, webhook,
--   grant, refund, recharge, checkout, invoice, and dispute state.
-- Depends on: 003_financial_policy_tables.sql.
-- Security: Scopes every provider identifier by tenant and provider environment.

-- Contents
--   1. Provider customers and subscriptions
--   2. Payments and webhook ingestion
--   3. Credit grants and refunds
--   4. Subscription changes
--   5. Auto-recharge policy and execution
--   6. Checkout, invoices, disputes, and preferences

-- 1. Provider customers and subscriptions

-- Map each tenant subject to at most one customer per provider/environment,
-- while preserving the provider's externally stable customer identifier.
CREATE TABLE bursar.billing_customers (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_customer_id text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider_customer_id, 255)),
    email text CHECK (
        email IS NULL
        OR bursar.is_nonempty_bounded_text(email, 320)
    ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_customer_id
    ),
    UNIQUE (
        tenant_id,
        subject_id,
        provider,
        provider_environment
    )
);

-- Keep subscription state pinned to its purchased catalog offer and order
-- provider events by provider_updated_at rather than delivery time.
CREATE TABLE bursar.billing_subscriptions (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_subscription_id text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider_subscription_id, 255)),
    provider_customer_id text CHECK (
        provider_customer_id IS NULL
        OR bursar.is_nonempty_bounded_text(provider_customer_id, 255)
    ),
    offer_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    status bursar.billing_subscription_status NOT NULL,
    current_period_start timestamptz CHECK (
        current_period_start IS NULL OR bursar.is_finite_timestamptz(current_period_start)
    ),
    current_period_end timestamptz CHECK (
        current_period_end IS NULL OR bursar.is_finite_timestamptz(current_period_end)
    ),
    trial_end timestamptz CHECK (trial_end IS NULL OR bursar.is_finite_timestamptz(trial_end)),
    cancel_at timestamptz CHECK (cancel_at IS NULL OR bursar.is_finite_timestamptz(cancel_at)),
    cancel_at_period_end boolean NOT NULL DEFAULT FALSE,
    ended_at timestamptz CHECK (ended_at IS NULL OR bursar.is_finite_timestamptz(ended_at)),
    grace_ends_at timestamptz CHECK (grace_ends_at IS NULL OR bursar.is_finite_timestamptz(grace_ends_at)),
    grace_expired_at timestamptz CHECK (grace_expired_at IS NULL OR bursar.is_finite_timestamptz(grace_expired_at)),
    provider_updated_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(provider_updated_at)),
    entitlement_provider_updated_at timestamptz CHECK (
        entitlement_provider_updated_at IS NULL
        OR bursar.is_finite_timestamptz(entitlement_provider_updated_at)
    ),
    entitlement_billing_event_id uuid,
    entitlement_reconciliation_outcome text CHECK (
        entitlement_reconciliation_outcome IS NULL
        OR entitlement_reconciliation_outcome IN (
            'applied', 'revoked', 'preserved'
        )
    ),
    status_changed_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(status_changed_at)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_subscription_id
    ),
    UNIQUE (id, subject_id),
    UNIQUE (id, subject_id, provider_environment),
    UNIQUE (id, subject_id, provider, provider_environment),
    FOREIGN KEY (offer_id, catalog_revision_id)
    REFERENCES bursar.catalog_offers (id, catalog_revision_id),
    CHECK (
        current_period_end IS NULL
        OR current_period_start IS NULL
        OR current_period_end > current_period_start
    ),
    CHECK (
        ended_at IS NULL
        OR status IN ('incomplete_expired', 'canceled', 'expired')
    ),
    CHECK (
        (grace_ends_at IS NULL AND grace_expired_at IS NULL)
        OR
        (status = 'past_due' AND grace_ends_at IS NOT NULL)
    ),
    CHECK (
        grace_expired_at IS NULL
        OR grace_expired_at >= grace_ends_at
    ),
    CHECK (
        (entitlement_provider_updated_at IS NULL)::integer
        + (entitlement_billing_event_id IS NULL)::integer
        + (entitlement_reconciliation_outcome IS NULL)::integer
        IN (0, 3)
    )
);

-- Track which subscription supplies entitlements for a subject/environment;
-- timestamps record each link's latest selection state and later uniqueness picks one.
CREATE TABLE bursar.billing_entitlement_sources (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    subscription_id uuid NOT NULL,
    selected boolean NOT NULL DEFAULT FALSE,
    selected_at timestamptz CHECK (selected_at IS NULL OR bursar.is_finite_timestamptz(selected_at)),
    deselected_at timestamptz CHECK (deselected_at IS NULL OR bursar.is_finite_timestamptz(deselected_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (
        tenant_id,
        subject_id,
        provider_environment,
        subscription_id
    ),
    FOREIGN KEY (subscription_id, subject_id, provider_environment)
    REFERENCES bursar.billing_subscriptions (
        id,
        subject_id,
        provider_environment
    ),
    CHECK (
        (
            selected
            AND selected_at IS NOT NULL
            AND deselected_at IS NULL
        )
        OR (
            NOT selected
            AND (
                (selected_at IS NULL AND deselected_at IS NULL)
                OR (
                    selected_at IS NOT NULL
                    AND deselected_at IS NOT NULL
                )
            )
        )
    ),
    CHECK (
        deselected_at IS NULL
        OR (selected_at IS NOT NULL AND deselected_at >= selected_at)
    )
);

-- 2. Payments and webhook ingestion

-- Persist exact minor-unit payment facts and provider lifecycle timestamps;
-- environment-qualified identity prevents test charges matching live records.
CREATE TABLE bursar.billing_payments (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_payment_id text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider_payment_id, 255)),
    provider_invoice_id text
    CHECK (
        provider_invoice_id IS NULL
        OR bursar.is_nonempty_bounded_text(provider_invoice_id, 255)
    ),
    amount_minor bigint NOT NULL
    CHECK (bursar.is_nonnegative_safe_integer(amount_minor)),
    tax_minor bigint NOT NULL DEFAULT 0
    CHECK (bursar.is_nonnegative_safe_integer(tax_minor)),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    purpose text NOT NULL CHECK (purpose IN ('subscription', 'credit_topup')),
    status bursar.billing_payment_status NOT NULL DEFAULT 'pending',
    provider_updated_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(provider_updated_at)),
    status_changed_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(status_changed_at)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_payment_id
    ),
    UNIQUE (id, subject_id),
    UNIQUE (id, subject_id, provider, provider_environment)
);

-- Deduplicate webhook envelopes by provider identity and retain explicit lease,
-- retry, terminal, and payload-archive state for crash-safe processing.
CREATE TABLE bursar.billing_events (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid REFERENCES bursar.subjects (id) ON DELETE RESTRICT,
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_event_id text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider_event_id, 255)),
    event_type text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(event_type, 255)),
    envelope_digest bytea NOT NULL CHECK (octet_length(envelope_digest) = 32),
    payload_received_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(payload_received_at)),
    payload_archived_at timestamptz CHECK (
        payload_archived_at IS NULL OR bursar.is_finite_timestamptz(payload_archived_at)
    ),
    payload_object_key text CHECK (
        payload_object_key IS NULL
        OR bursar.is_nonempty_bounded_text(payload_object_key, 2048)
    ),
    payload_object_version text CHECK (
        payload_object_version IS NULL
        OR bursar.is_nonempty_bounded_text(payload_object_version, 1024)
    ),
    status bursar.billing_event_status NOT NULL DEFAULT 'processing',
    attempt_count integer NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
    claim_token uuid,
    claim_expires_at timestamptz CHECK (claim_expires_at IS NULL OR bursar.is_finite_timestamptz(claim_expires_at)),
    last_error text CHECK (
        last_error IS NULL
        OR bursar.is_nonempty_bounded_text(last_error, 8192)
    ),
    completed_at timestamptz CHECK (completed_at IS NULL OR bursar.is_finite_timestamptz(completed_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_event_id
    ),
    UNIQUE (id, payload_received_at),
    CHECK (
        (
            status = 'processing'
            AND claim_token IS NOT NULL
            AND claim_expires_at IS NOT NULL
        )
        OR
        (
            status <> 'processing'
            AND claim_token IS NULL
            AND claim_expires_at IS NULL
        )
    ),
    CHECK (
        (status IN ('completed', 'ignored') AND completed_at IS NOT NULL)
        OR
        (status NOT IN ('completed', 'ignored') AND completed_at IS NULL)
    ),
    CHECK (
        (
            payload_archived_at IS NULL
            AND payload_object_key IS NULL
            AND payload_object_version IS NULL
        )
        OR
        (payload_archived_at IS NOT NULL AND payload_object_key IS NOT NULL)
    )
);

ALTER TABLE bursar.billing_subscriptions
ADD CONSTRAINT billing_subscriptions_entitlement_event_fkey
FOREIGN KEY (entitlement_billing_event_id)
REFERENCES bursar.billing_events (id)
ON DELETE RESTRICT;

-- Keep full provider envelopes in a time-partitioned retention tier, separate
-- from the durable event digest and processing outcome.
CREATE TABLE bursar.billing_event_payloads (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    event_id uuid NOT NULL,
    received_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(received_at)),
    envelope jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(envelope, 1048576)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    PRIMARY KEY (received_at, event_id),
    FOREIGN KEY (event_id, received_at)
    REFERENCES bursar.billing_events (id, payload_received_at)
    ON DELETE CASCADE
) PARTITION BY RANGE (received_at);

-- Preserve duplicate-subscription conflicts as operator-visible evidence rather
-- than silently choosing an entitlement source or discarding the event.
CREATE TABLE bursar.billing_subscription_conflicts (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id uuid REFERENCES bursar.subjects (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    duplicate_provider_subscription_id text NOT NULL
    CHECK (
        bursar.is_nonempty_bounded_text(
            duplicate_provider_subscription_id,
            255
        )
    ),
    existing_subscription_id uuid REFERENCES bursar.billing_subscriptions (id),
    billing_event_id uuid REFERENCES bursar.billing_events (id),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        duplicate_provider_subscription_id
    )
);

-- 3. Credit grants and refunds

-- Tie each purchased or subscription-cycle credit grant to its exact catalog
-- configuration and deduplicate nullable source combinations as one receipt.
CREATE TABLE bursar.billing_credit_grants (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    payment_id uuid,
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    topup_id uuid,
    subscription_id uuid,
    catalog_revision_id uuid NOT NULL,
    grant_key text NOT NULL DEFAULT 'default'
    CHECK (bursar.is_nonempty_bounded_text(grant_key, 255)),
    configured_credits bursar.credit_numeric NOT NULL
    CHECK (configured_credits > 0),
    quantity integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
    expiry_policy_snapshot jsonb CHECK (
        expiry_policy_snapshot IS NULL
        OR (
            octet_length(expiry_policy_snapshot::text) <= 32768
            AND bursar.matches_catalog_fragment(
                expiry_policy_snapshot,
                bursar.catalog_document_shape_schema()
                -> '$defs' -> 'BucketDefinition' -> 'properties' -> 'expiry'
            )
        )
    ),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    billing_event_id uuid REFERENCES bursar.billing_events (id),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    FOREIGN KEY (payment_id, subject_id)
    REFERENCES bursar.billing_payments (id, subject_id),
    FOREIGN KEY (subscription_id, subject_id)
    REFERENCES bursar.billing_subscriptions (id, subject_id),
    FOREIGN KEY (topup_id, catalog_revision_id)
    REFERENCES bursar.catalog_topups (id, catalog_revision_id),
    CHECK (
        (payment_id IS NOT NULL AND topup_id IS NOT NULL AND subscription_id IS NULL)
        OR
        (topup_id IS NULL AND subscription_id IS NOT NULL)
    ),
    UNIQUE NULLS NOT DISTINCT (payment_id, topup_id, billing_event_id, grant_key)
);

-- Store provider refund state in exact minor units with provider ordering data;
-- later bounds ensure succeeded refunds never exceed their payment.
CREATE TABLE bursar.billing_refunds (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    payment_id uuid NOT NULL REFERENCES bursar.billing_payments (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_refund_id text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider_refund_id, 255)),
    amount_minor bigint NOT NULL
    CHECK (bursar.is_positive_safe_integer(amount_minor)),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'succeeded', 'failed', 'canceled')),
    reason text CHECK (
        reason IS NULL
        OR bursar.is_nonempty_bounded_text(reason, 2048)
    ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    provider_updated_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(provider_updated_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_refund_id
    )
);

-- Allocate each refund across original credit grants, coupling money returned
-- to exact credit clawbacks without losing many-grant payment provenance.
CREATE TABLE bursar.billing_refund_grants (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    refund_id uuid NOT NULL
    REFERENCES bursar.billing_refunds (id) ON DELETE CASCADE,
    grant_id uuid NOT NULL REFERENCES bursar.billing_credit_grants (id),
    amount_minor bigint NOT NULL
    CHECK (bursar.is_positive_safe_integer(amount_minor)),
    credit_amount bursar.credit_numeric NOT NULL
    CHECK (credit_amount > 0),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    PRIMARY KEY (refund_id, grant_id)
);

-- 4. Subscription changes

-- Model a subscription transition as a retryable operation between exact offer
-- revisions, retaining effective/proration behavior and provider failures.
CREATE TABLE bursar.billing_subscription_changes (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subscription_id uuid NOT NULL REFERENCES bursar.billing_subscriptions (id),
    state text NOT NULL
    CHECK (state IN (
        'awaiting_payment', 'scheduled', 'applied', 'canceled', 'failed'
    )),
    from_offer_id uuid NOT NULL,
    from_catalog_revision_id uuid NOT NULL,
    to_offer_id uuid NOT NULL,
    to_catalog_revision_id uuid NOT NULL,
    effective_at timestamptz CHECK (effective_at IS NULL OR bursar.is_finite_timestamptz(effective_at)),
    effective_behavior text NOT NULL
    CONSTRAINT billing_subscription_changes_effective_behavior_check
    CHECK (effective_behavior IN ('immediate', 'renewal')),
    proration_behavior text NOT NULL DEFAULT 'provider_default'
    CHECK (proration_behavior IN (
        'provider_default', 'invoice_immediately', 'none'
    )),
    provider_operation_id text CHECK (
        provider_operation_id IS NULL
        OR bursar.is_nonempty_bounded_text(provider_operation_id, 255)
    ),
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(idempotency_key, 255)),
    error_message text CHECK (
        error_message IS NULL
        OR bursar.is_nonempty_bounded_text(error_message, 8192)
    ),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (subscription_id, idempotency_key),
    FOREIGN KEY (from_offer_id, from_catalog_revision_id)
    REFERENCES bursar.catalog_offers (id, catalog_revision_id),
    FOREIGN KEY (to_offer_id, to_catalog_revision_id)
    REFERENCES bursar.catalog_offers (id, catalog_revision_id),
    CHECK (from_offer_id <> to_offer_id),
    CHECK (effective_at IS NOT NULL OR state = 'awaiting_payment')
);

-- 5. Auto-recharge policy and execution

-- Store a subject's environment-specific recharge choice with threshold/rearm
-- hysteresis, cooldown, rate limits, and a pause-on-failure circuit breaker.
CREATE TABLE bursar.billing_auto_recharge_profiles (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    enabled boolean NOT NULL DEFAULT FALSE,
    armed boolean NOT NULL DEFAULT TRUE,
    state text NOT NULL DEFAULT 'disabled'
    CHECK (state IN ('active', 'paused', 'disabled')),
    provider text CHECK (
        provider IS NULL OR bursar.is_nonempty_bounded_text(provider, 100)
    ),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    catalog_revision_id uuid REFERENCES bursar.catalog_revisions (id),
    topup_id uuid,
    quantity integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
    threshold bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (threshold >= 0),
    rearm_above bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (rearm_above >= 0),
    max_charges_per_window integer
    CHECK (max_charges_per_window IS NULL OR max_charges_per_window > 0),
    max_charge_minor bigint CHECK (
        max_charge_minor IS NULL
        OR bursar.is_positive_safe_integer(max_charge_minor)
    ),
    cooldown_seconds integer NOT NULL DEFAULT 0 CHECK (cooldown_seconds >= 0),
    max_consecutive_failures integer NOT NULL DEFAULT 3
    CHECK (max_consecutive_failures > 0),
    consecutive_failures integer NOT NULL DEFAULT 0
    CHECK (consecutive_failures >= 0),
    window_unit text NOT NULL DEFAULT 'day'
    CHECK (window_unit IN ('second', 'minute', 'hour', 'day', 'week', 'month', 'year')),
    window_count integer NOT NULL DEFAULT 1 CHECK (window_count > 0),
    window_anchor text NOT NULL DEFAULT 'calendar'
    CHECK (window_anchor IN ('calendar', 'rolling')),
    window_timezone text NOT NULL DEFAULT 'UTC',
    last_attempt_at timestamptz CHECK (last_attempt_at IS NULL OR bursar.is_finite_timestamptz(last_attempt_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    PRIMARY KEY (tenant_id, subject_id, provider_environment),
    FOREIGN KEY (topup_id, catalog_revision_id)
    REFERENCES bursar.catalog_topups (id, catalog_revision_id),
    CHECK (NOT enabled OR (provider IS NOT NULL AND topup_id IS NOT NULL)),
    CHECK (enabled = (state <> 'disabled')),
    CHECK (enabled OR armed),
    CHECK (rearm_above > threshold OR NOT enabled)
);

-- Record each asynchronous recharge attempt with a tenant idempotency key and
-- explicit unknown/action-required states so uncertain charges are not repeated.
CREATE TABLE bursar.billing_auto_recharge_attempts (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_bounded_text(idempotency_key, 255)
    ),
    state bursar.recharge_attempt_status NOT NULL DEFAULT 'claimed',
    topup_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    window_start timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(window_start)),
    window_end timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(window_end)),
    quoted_amount_minor bigint CHECK (
        quoted_amount_minor IS NULL
        OR bursar.is_nonnegative_safe_integer(quoted_amount_minor)
    ),
    currency text,
    provider_attempt_id text CHECK (
        provider_attempt_id IS NULL
        OR bursar.is_nonempty_bounded_text(provider_attempt_id, 255)
    ),
    failure_code text CHECK (
        failure_code IS NULL
        OR bursar.is_nonempty_bounded_text(failure_code, 255)
    ),
    failure_message text CHECK (
        failure_message IS NULL
        OR bursar.is_nonempty_bounded_text(failure_message, 8192)
    ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (
        tenant_id,
        subject_id,
        provider_environment,
        idempotency_key
    ),
    FOREIGN KEY (topup_id, catalog_revision_id)
    REFERENCES bursar.catalog_topups (id, catalog_revision_id),
    CHECK (window_end > window_start),
    CHECK ((quoted_amount_minor IS NULL) = (currency IS NULL)),
    CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$')
);

-- Project revision-pinned recharge guardrails, constraining selectable topups,
-- quantity, balance hysteresis, spend caps, cooldown, and failure action.
CREATE TABLE bursar.catalog_auto_recharge_policies (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    catalog_revision_id uuid PRIMARY KEY
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    eligible_topup_keys text [] NOT NULL CHECK (
        cardinality(eligible_topup_keys) > 0
        AND bursar.is_canonical_identifier_array(
            eligible_topup_keys,
            1000,
            255
        )
    ),
    default_topup_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(default_topup_key, 255)),
    quantity_min integer NOT NULL CHECK (quantity_min > 0),
    quantity_max integer NOT NULL CHECK (quantity_max >= quantity_min),
    quantity integer NOT NULL CHECK (quantity BETWEEN quantity_min AND quantity_max),
    balance_min bursar.credit_numeric NOT NULL
    CHECK (balance_min >= 0),
    balance_max bursar.credit_numeric NOT NULL
    CHECK (
        balance_max >= balance_min
    ),
    balance_below bursar.credit_numeric NOT NULL
    CHECK (
        balance_below BETWEEN balance_min AND balance_max
    ),
    rearm_above bursar.credit_numeric NOT NULL
    CHECK (
        rearm_above > balance_below
    ),
    max_purchases integer NOT NULL CHECK (max_purchases > 0),
    max_charge_minor bigint NOT NULL
    CHECK (bursar.is_positive_safe_integer(max_charge_minor)),
    cooldown_seconds integer NOT NULL CHECK (cooldown_seconds > 0),
    max_consecutive_failures integer NOT NULL CHECK (max_consecutive_failures > 0),
    failure_action text NOT NULL CHECK (failure_action = 'pause'),
    period_unit text NOT NULL
    CHECK (period_unit IN ('second', 'minute', 'hour', 'day', 'week', 'month', 'year')),
    period_count integer NOT NULL CHECK (period_count > 0),
    period_anchor text NOT NULL CHECK (period_anchor IN ('calendar', 'rolling')),
    period_timezone text NOT NULL,
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'AutoRechargeGuardrails'
        )
    ),
    FOREIGN KEY (catalog_revision_id, default_topup_key)
    REFERENCES bursar.catalog_topups (catalog_revision_id, topup_key)
);

-- 6. Checkout, invoices, disputes, and preferences

-- Make checkout creation replay-safe per subject/provider/environment while
-- pinning the requested product to the catalog revision used for quoting.
CREATE TABLE bursar.billing_checkout_intents (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    checkout_kind text NOT NULL
    CHECK (checkout_kind IN ('subscription', 'credit_topup')),
    product_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(product_key, 255)),
    region text CHECK (region IS NULL OR region ~ '^[A-Z]{2,3}$'),
    catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions (id),
    operation_key text NOT NULL
    CONSTRAINT billing_checkout_intents_operation_key_check
    CHECK (bursar.is_nonempty_bounded_text(operation_key, 255)),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    status text NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'completed', 'failed', 'expired')),
    provider_session_id text CHECK (
        provider_session_id IS NULL
        OR bursar.is_nonempty_bounded_text(provider_session_id, 255)
    ),
    checkout_url text CHECK (
        checkout_url IS NULL
        OR bursar.is_nonempty_bounded_text(checkout_url, 8192)
    ),
    expires_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(expires_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    CONSTRAINT billing_checkout_intents_operation_key_unique
    UNIQUE (
        tenant_id,
        subject_id,
        provider,
        provider_environment,
        operation_key
    ),
    CHECK (expires_at > created_at)
);

-- Mirror provider invoices in exact minor units and bind any subscription to the
-- same subject, provider, and environment for safe reconciliation.
CREATE TABLE bursar.billing_invoices (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_invoice_id text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider_invoice_id, 255)),
    subscription_id uuid,
    status text NOT NULL
    CHECK (status IN ('draft', 'open', 'paid', 'void', 'uncollectible')),
    amount_due_minor bigint NOT NULL
    CHECK (bursar.is_nonnegative_safe_integer(amount_due_minor)),
    amount_paid_minor bigint NOT NULL DEFAULT 0
    CHECK (bursar.is_nonnegative_safe_integer(amount_paid_minor)),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    period_start timestamptz CHECK (period_start IS NULL OR bursar.is_finite_timestamptz(period_start)),
    period_end timestamptz CHECK (period_end IS NULL OR bursar.is_finite_timestamptz(period_end)),
    provider_updated_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(provider_updated_at)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_invoice_id
    ),
    FOREIGN KEY (
        subscription_id,
        subject_id,
        provider,
        provider_environment
    ) REFERENCES bursar.billing_subscriptions (
        id,
        subject_id,
        provider,
        provider_environment
    ),
    CHECK (
        period_end IS NULL
        OR period_start IS NULL
        OR period_end > period_start
    )
);

-- Preserve dispute lifecycle independently of payment status while ensuring a
-- linked payment shares subject, provider, and environment identity.
CREATE TABLE bursar.billing_disputes (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid REFERENCES bursar.subjects (id),
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_dispute_id text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider_dispute_id, 255)),
    payment_id uuid,
    status text NOT NULL
    CHECK (status IN (
        'needs_response', 'under_review', 'won', 'lost', 'closed'
    )),
    reason text CHECK (
        reason IS NULL
        OR bursar.is_nonempty_bounded_text(reason, 2048)
    ),
    provider_updated_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(provider_updated_at)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_dispute_id
    ),
    FOREIGN KEY (
        payment_id,
        subject_id,
        provider,
        provider_environment
    ) REFERENCES bursar.billing_payments (
        id,
        subject_id,
        provider,
        provider_environment
    ),
    CHECK (payment_id IS NULL OR subject_id IS NOT NULL)
);

-- Keep one tenant-subject notification and protection preference row; billing
-- automation remains opt-in while overage protection defaults fail-safe.
CREATE TABLE bursar.billing_preferences (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    auto_recharge boolean NOT NULL DEFAULT FALSE,
    overage_protection boolean NOT NULL DEFAULT TRUE,
    email_notifications boolean NOT NULL DEFAULT TRUE,
    usage_alerts boolean NOT NULL DEFAULT TRUE,
    invoice_reminders boolean NOT NULL DEFAULT FALSE,
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    PRIMARY KEY (tenant_id, subject_id)
);

-- Permit only one nonterminal/uncertain recharge per subject and environment,
-- closing the concurrency gap before an attempt reaches provider settlement.
CREATE UNIQUE INDEX billing_auto_recharge_one_active
ON bursar.billing_auto_recharge_attempts (
    tenant_id,
    subject_id,
    provider_environment
)
WHERE state IN (
    'claimed', 'submitted', 'processing', 'unknown', 'action_required'
);

-- Deduplicate provider attempts once an external identifier is known, while
-- excluding pre-submission rows that legitimately have no identifier yet.
CREATE UNIQUE INDEX billing_auto_recharge_provider_attempt_unique
ON bursar.billing_auto_recharge_attempts (
    tenant_id,
    provider,
    provider_environment,
    provider_attempt_id
)
WHERE provider_attempt_id IS NOT NULL;
