CREATE TABLE bursar.billing_customers (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_customer_id text NOT NULL
    CHECK (bursar.is_nonempty_text(provider_customer_id)),
    email text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
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

CREATE TABLE bursar.billing_subscriptions (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_subscription_id text NOT NULL
    CHECK (bursar.is_nonempty_text(provider_subscription_id)),
    provider_customer_id text,
    offer_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    status bursar.billing_subscription_status NOT NULL,
    current_period_start timestamptz,
    current_period_end timestamptz,
    trial_end timestamptz,
    cancel_at timestamptz,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    ended_at timestamptz,
    grace_ends_at timestamptz,
    grace_expired_at timestamptz,
    provider_updated_at timestamptz NOT NULL DEFAULT now(),
    status_changed_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_subscription_id
    ),
    UNIQUE (id, subject_id),
    UNIQUE (id, subject_id, provider_environment),
    FOREIGN KEY (offer_id, catalog_revision_id)
    REFERENCES bursar.catalog_offers (id, catalog_revision_id),
    CHECK (
        current_period_end IS null
        OR current_period_start IS null
        OR current_period_end > current_period_start
    ),
    CHECK (
        ended_at IS null
        OR status IN ('incomplete_expired', 'canceled', 'expired')
    ),
    CHECK (
        (grace_ends_at IS null AND grace_expired_at IS null)
        OR
        (status = 'past_due' AND grace_ends_at IS NOT null)
    ),
    CHECK (
        grace_expired_at IS null
        OR grace_expired_at >= grace_ends_at
    )
);

CREATE TABLE bursar.billing_entitlement_sources (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    subscription_id uuid NOT NULL,
    selected boolean NOT NULL DEFAULT false,
    selected_at timestamptz,
    deselected_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
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
        (selected AND selected_at IS NOT null AND deselected_at IS null)
        OR (NOT selected)
    ),
    CHECK (
        deselected_at IS null
        OR (selected_at IS NOT null AND deselected_at >= selected_at)
    )
);

CREATE TABLE bursar.billing_payments (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_payment_id text NOT NULL
    CHECK (bursar.is_nonempty_text(provider_payment_id)),
    provider_invoice_id text
    CHECK (provider_invoice_id IS null OR bursar.is_nonempty_text(provider_invoice_id)),
    amount_minor bigint NOT NULL CHECK (amount_minor >= 0),
    tax_minor bigint NOT NULL DEFAULT 0 CHECK (tax_minor >= 0),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    purpose text NOT NULL CHECK (purpose IN ('subscription', 'credit_topup')),
    status bursar.billing_payment_status NOT NULL DEFAULT 'pending',
    provider_updated_at timestamptz NOT NULL DEFAULT now(),
    status_changed_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_payment_id
    ),
    UNIQUE (id, subject_id)
);

CREATE TABLE bursar.billing_events (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    provider text NOT NULL
    CHECK (
        bursar.is_nonempty_text(provider)
        AND bursar.is_bounded_text(provider, 100)
    ),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_event_id text NOT NULL
    CHECK (
        bursar.is_nonempty_text(provider_event_id)
        AND bursar.is_bounded_text(provider_event_id, 255)
    ),
    event_type text NOT NULL
    CHECK (
        bursar.is_nonempty_text(event_type)
        AND bursar.is_bounded_text(event_type, 255)
    ),
    envelope_digest bytea NOT NULL CHECK (octet_length(envelope_digest) = 32),
    payload_received_at timestamptz NOT NULL DEFAULT now(),
    payload_archived_at timestamptz,
    payload_object_key text CHECK (
        payload_object_key IS null
        OR bursar.is_bounded_text(payload_object_key, 2048)
    ),
    payload_object_version text CHECK (
        payload_object_version IS null
        OR bursar.is_bounded_text(payload_object_version, 255)
    ),
    status bursar.billing_event_status NOT NULL DEFAULT 'processing',
    attempt_count integer NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
    claim_token uuid,
    claim_expires_at timestamptz,
    last_error text CHECK (
        last_error IS null OR bursar.is_bounded_text(last_error, 8192)
    ),
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_event_id
    ),
    CHECK (
        (
            status = 'processing'
            AND claim_token IS NOT null
            AND claim_expires_at IS NOT null
        )
        OR
        (
            status <> 'processing'
            AND claim_token IS null
            AND claim_expires_at IS null
        )
    ),
    CHECK (
        (status IN ('completed', 'ignored') AND completed_at IS NOT null)
        OR
        (status NOT IN ('completed', 'ignored') AND completed_at IS null)
    ),
    CHECK (
        (
            payload_archived_at IS null
            AND payload_object_key IS null
            AND payload_object_version IS null
        )
        OR
        (payload_archived_at IS NOT null AND payload_object_key IS NOT null)
    )
);

CREATE TABLE bursar.billing_event_payloads (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    event_id uuid NOT NULL
    REFERENCES bursar.billing_events (id) ON DELETE CASCADE,
    received_at timestamptz NOT NULL,
    envelope jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(envelope, 1048576)),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (received_at, event_id)
) PARTITION BY RANGE (received_at);

CREATE TABLE bursar.billing_subscription_conflicts (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id uuid REFERENCES bursar.subjects (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    duplicate_provider_subscription_id text NOT NULL
    CHECK (bursar.is_nonempty_text(duplicate_provider_subscription_id)),
    existing_subscription_id uuid REFERENCES bursar.billing_subscriptions (id),
    billing_event_id uuid REFERENCES bursar.billing_events (id),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        duplicate_provider_subscription_id
    )
);

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
    CHECK (bursar.is_nonempty_text(grant_key)),
    configured_credits numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(configured_credits) AND configured_credits > 0),
    quantity integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
    expiry_policy_snapshot jsonb CHECK (
        expiry_policy_snapshot IS null
        OR octet_length(expiry_policy_snapshot::text) <= 32768
    ),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    billing_event_id uuid REFERENCES bursar.billing_events (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (payment_id, subject_id)
    REFERENCES bursar.billing_payments (id, subject_id),
    FOREIGN KEY (subscription_id, subject_id)
    REFERENCES bursar.billing_subscriptions (id, subject_id),
    FOREIGN KEY (topup_id, catalog_revision_id)
    REFERENCES bursar.catalog_topups (id, catalog_revision_id),
    CHECK (
        (payment_id IS NOT null AND topup_id IS NOT null AND subscription_id IS null)
        OR
        (topup_id IS null AND subscription_id IS NOT null)
    ),
    UNIQUE NULLS NOT DISTINCT (payment_id, topup_id, billing_event_id, grant_key)
);

CREATE TABLE bursar.billing_refunds (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    payment_id uuid NOT NULL REFERENCES bursar.billing_payments (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_refund_id text NOT NULL
    CHECK (bursar.is_nonempty_text(provider_refund_id)),
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'succeeded', 'failed', 'canceled')),
    reason text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    provider_updated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_refund_id
    )
);

CREATE TABLE bursar.billing_refund_grants (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    refund_id uuid NOT NULL
    REFERENCES bursar.billing_refunds (id) ON DELETE CASCADE,
    grant_id uuid NOT NULL REFERENCES bursar.billing_credit_grants (id),
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    credit_amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(credit_amount) AND credit_amount > 0),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    PRIMARY KEY (refund_id, grant_id)
);

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
    effective_at timestamptz,
    proration_behavior text NOT NULL DEFAULT 'provider_default'
    CHECK (proration_behavior IN (
        'provider_default', 'invoice_immediately', 'none'
    )),
    provider_operation_id text,
    idempotency_key text NOT NULL CHECK (bursar.is_nonempty_text(idempotency_key)),
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (subscription_id, idempotency_key),
    FOREIGN KEY (from_offer_id, from_catalog_revision_id)
    REFERENCES bursar.catalog_offers (id, catalog_revision_id),
    FOREIGN KEY (to_offer_id, to_catalog_revision_id)
    REFERENCES bursar.catalog_offers (id, catalog_revision_id),
    CHECK (from_offer_id <> to_offer_id),
    CHECK (effective_at IS NOT null OR state = 'awaiting_payment')
);

CREATE TABLE bursar.billing_auto_recharge_profiles (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    enabled boolean NOT NULL DEFAULT false,
    armed boolean NOT NULL DEFAULT true,
    state text NOT NULL DEFAULT 'active'
    CHECK (state IN ('active', 'paused', 'disabled')),
    provider text,
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    catalog_revision_id uuid REFERENCES bursar.catalog_revisions (id),
    topup_id uuid,
    quantity integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
    threshold numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (bursar.is_finite_numeric(threshold) AND threshold >= 0),
    rearm_above numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (bursar.is_finite_numeric(rearm_above) AND rearm_above >= 0),
    max_charges_per_window integer
    CHECK (max_charges_per_window IS null OR max_charges_per_window > 0),
    max_charge_minor bigint CHECK (max_charge_minor IS null OR max_charge_minor > 0),
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
    last_attempt_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject_id, provider_environment),
    FOREIGN KEY (topup_id, catalog_revision_id)
    REFERENCES bursar.catalog_topups (id, catalog_revision_id),
    CHECK (NOT enabled OR (provider IS NOT null AND topup_id IS NOT null)),
    CHECK (rearm_above > threshold OR NOT enabled)
);

CREATE TABLE bursar.billing_auto_recharge_attempts (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_text(idempotency_key)
        AND bursar.is_bounded_text(idempotency_key, 255)
    ),
    state bursar.recharge_attempt_status NOT NULL DEFAULT 'claimed',
    topup_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    quoted_amount_minor bigint CHECK (quoted_amount_minor IS null OR quoted_amount_minor >= 0),
    currency text,
    provider_attempt_id text CHECK (
        provider_attempt_id IS null
        OR bursar.is_bounded_text(provider_attempt_id, 255)
    ),
    failure_code text CHECK (
        failure_code IS null OR bursar.is_bounded_text(failure_code, 255)
    ),
    failure_message text CHECK (
        failure_message IS null
        OR bursar.is_bounded_text(failure_message, 8192)
    ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        subject_id,
        provider_environment,
        idempotency_key
    ),
    FOREIGN KEY (topup_id, catalog_revision_id)
    REFERENCES bursar.catalog_topups (id, catalog_revision_id),
    CHECK (window_end > window_start),
    CHECK ((quoted_amount_minor IS null) = (currency IS null)),
    CHECK (currency IS null OR currency ~ '^[A-Z]{3}$')
);

CREATE TABLE bursar.catalog_auto_recharge_policies (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    catalog_revision_id uuid PRIMARY KEY
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    eligible_topup_keys text [] NOT NULL CHECK (cardinality(eligible_topup_keys) > 0),
    default_topup_key text NOT NULL,
    quantity_min integer NOT NULL CHECK (quantity_min > 0),
    quantity_max integer NOT NULL CHECK (quantity_max >= quantity_min),
    quantity integer NOT NULL CHECK (quantity BETWEEN quantity_min AND quantity_max),
    balance_min numeric(20, 6) NOT NULL CHECK (balance_min >= 0),
    balance_max numeric(20, 6) NOT NULL CHECK (balance_max >= balance_min),
    balance_below numeric(20, 6) NOT NULL
    CHECK (balance_below BETWEEN balance_min AND balance_max),
    rearm_above numeric(20, 6) NOT NULL CHECK (rearm_above > balance_below),
    max_purchases integer NOT NULL CHECK (max_purchases > 0),
    max_charge_minor bigint NOT NULL CHECK (max_charge_minor > 0),
    cooldown_seconds integer NOT NULL CHECK (cooldown_seconds > 0),
    max_consecutive_failures integer NOT NULL CHECK (max_consecutive_failures > 0),
    failure_action text NOT NULL CHECK (failure_action = 'pause'),
    period_unit text NOT NULL
    CHECK (period_unit IN ('second', 'minute', 'hour', 'day', 'week', 'month', 'year')),
    period_count integer NOT NULL CHECK (period_count > 0),
    period_anchor text NOT NULL CHECK (period_anchor IN ('calendar', 'rolling')),
    period_timezone text NOT NULL,
    definition jsonb NOT NULL
    CHECK (bursar.is_bounded_json_object(definition, 262144)),
    FOREIGN KEY (catalog_revision_id, default_topup_key)
    REFERENCES bursar.catalog_topups (catalog_revision_id, topup_key)
);

CREATE TABLE bursar.billing_checkout_intents (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    checkout_kind text NOT NULL
    CHECK (checkout_kind IN ('subscription', 'credit_topup')),
    product_key text NOT NULL CHECK (bursar.is_nonempty_text(product_key)),
    region text CHECK (region IS null OR region ~ '^[A-Z]{2,3}$'),
    catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions (id),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    status text NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'completed', 'failed', 'expired')),
    provider_session_id text,
    checkout_url text,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        subject_id,
        provider,
        provider_environment,
        checkout_kind,
        product_key,
        catalog_revision_id,
        request_digest
    ),
    CHECK (expires_at > created_at)
);

CREATE TABLE bursar.billing_invoices (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_invoice_id text NOT NULL
    CHECK (bursar.is_nonempty_text(provider_invoice_id)),
    subscription_id uuid REFERENCES bursar.billing_subscriptions (id),
    status text NOT NULL
    CHECK (status IN ('draft', 'open', 'paid', 'void', 'uncollectible')),
    amount_due_minor bigint NOT NULL CHECK (amount_due_minor >= 0),
    amount_paid_minor bigint NOT NULL DEFAULT 0 CHECK (amount_paid_minor >= 0),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    period_start timestamptz,
    period_end timestamptz,
    provider_updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_invoice_id
    ),
    CHECK (
        period_end IS null
        OR period_start IS null
        OR period_end > period_start
    )
);

CREATE TABLE bursar.billing_disputes (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid REFERENCES bursar.subjects (id),
    provider text NOT NULL CHECK (bursar.is_nonempty_text(provider)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    provider_dispute_id text NOT NULL
    CHECK (bursar.is_nonempty_text(provider_dispute_id)),
    payment_id uuid REFERENCES bursar.billing_payments (id),
    status text NOT NULL
    CHECK (status IN (
        'needs_response', 'under_review', 'won', 'lost', 'closed'
    )),
    reason text,
    provider_updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        provider_dispute_id
    )
);

CREATE TABLE bursar.billing_preferences (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    auto_recharge boolean NOT NULL DEFAULT false,
    overage_protection boolean NOT NULL DEFAULT true,
    email_notifications boolean NOT NULL DEFAULT true,
    usage_alerts boolean NOT NULL DEFAULT true,
    invoice_reminders boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject_id)
);

CREATE UNIQUE INDEX billing_auto_recharge_one_active
ON bursar.billing_auto_recharge_attempts (
    tenant_id,
    subject_id,
    provider_environment
)
WHERE state IN (
    'claimed', 'submitted', 'processing', 'unknown', 'action_required'
);

CREATE UNIQUE INDEX billing_auto_recharge_provider_attempt_unique
ON bursar.billing_auto_recharge_attempts (
    tenant_id,
    provider,
    provider_environment,
    provider_attempt_id
)
WHERE provider_attempt_id IS NOT null;
