CREATE TABLE bursar.credit_ledger_entries (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    kind bursar.ledger_entry_kind NOT NULL,
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount <> 0),
    balance_after numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(balance_after)),
    reference_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    catalog_revision_id uuid REFERENCES bursar.catalog_revisions (id),
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_text(idempotency_key)
        AND bursar.is_bounded_text(idempotency_key, 255)
    ),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    operation text NOT NULL
    CHECK (
        bursar.is_nonempty_text(operation)
        AND bursar.is_bounded_text(operation, 255)
    ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, idempotency_key),
    CHECK (
        (kind IN ('grant', 'purchase', 'refund', 'release') AND amount > 0)
        OR
        (kind IN (
            'usage', 'expiry', 'revocation', 'refund_clawback', 'reservation'
        ) AND amount < 0)
        OR kind = 'adjustment'
    ),
    CHECK (reference_entry_id IS NULL OR reference_entry_id <> id)
);

CREATE TABLE bursar.credit_lots (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    source_entry_id uuid NOT NULL UNIQUE
    REFERENCES bursar.credit_ledger_entries (id),
    catalog_revision_id uuid NOT NULL,
    bucket_key text NOT NULL,
    priority integer NOT NULL CHECK (priority >= 0),
    granted numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(granted) AND granted > 0),
    consumed numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (
        bursar.is_finite_numeric(consumed)
        AND consumed >= 0
        AND consumed <= granted
    ),
    expires_at timestamptz,
    expiry_policy_snapshot jsonb NOT NULL DEFAULT '{"type":"never"}'::jsonb
    CHECK (
        bursar.is_bounded_json_object(expiry_policy_snapshot, 32768)
    ),
    source_type text NOT NULL DEFAULT 'ledger'
    CHECK (source_type IN (
        'ledger', 'grant_program', 'topup', 'subscription_cycle',
        'refund', 'adjustment'
    )),
    source_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (catalog_revision_id, bucket_key)
    REFERENCES bursar.catalog_buckets (catalog_revision_id, bucket_key)
);

CREATE TABLE bursar.credit_lot_sources (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    lot_id uuid NOT NULL REFERENCES bursar.credit_lots (id) ON DELETE CASCADE,
    ledger_entry_id uuid NOT NULL UNIQUE
    REFERENCES bursar.credit_ledger_entries (id),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount > 0),
    source_type text NOT NULL,
    source_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bursar.credit_lot_allocations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    debit_entry_id uuid NOT NULL
    REFERENCES bursar.credit_ledger_entries (id),
    lot_id uuid NOT NULL REFERENCES bursar.credit_lots (id),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount > 0),
    allocation_kind text NOT NULL
    CHECK (allocation_kind IN ('spend', 'expiry', 'revocation', 'clawback')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (debit_entry_id, lot_id)
);

CREATE TABLE bursar.credit_lot_source_allocations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    lot_allocation_id uuid NOT NULL
    REFERENCES bursar.credit_lot_allocations (id),
    lot_source_id uuid NOT NULL REFERENCES bursar.credit_lot_sources (id),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (lot_allocation_id, lot_source_id)
);

CREATE TABLE bursar.credit_lot_restorations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    refund_entry_id uuid NOT NULL
    REFERENCES bursar.credit_ledger_entries (id),
    original_allocation_id uuid NOT NULL
    REFERENCES bursar.credit_lot_allocations (id),
    lot_id uuid NOT NULL REFERENCES bursar.credit_lots (id),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (refund_entry_id, original_allocation_id)
);

CREATE TABLE bursar.credit_lot_source_restorations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    lot_restoration_id uuid NOT NULL
    REFERENCES bursar.credit_lot_restorations (id),
    source_allocation_id uuid NOT NULL
    REFERENCES bursar.credit_lot_source_allocations (id),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (lot_restoration_id, source_allocation_id)
);

CREATE TABLE bursar.credit_unallocated_debits (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    ledger_entry_id uuid PRIMARY KEY
    REFERENCES bursar.credit_ledger_entries (id),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount > 0),
    reason text NOT NULL CHECK (reason IN ('credit_line', 'refund_debt')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bursar.credit_debt_repayments (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    ledger_entry_id uuid PRIMARY KEY
    REFERENCES bursar.credit_ledger_entries (id),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bursar.credit_usage_charges (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    operation text NOT NULL
    CHECK (
        bursar.is_nonempty_text(operation)
        AND bursar.is_bounded_text(operation, 255)
    ),
    event_at timestamptz NOT NULL DEFAULT now(),
    requested numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(requested) AND requested >= 0),
    charged numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(charged) AND charged >= 0),
    allowance_requested numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (
        bursar.is_finite_numeric(allowance_requested)
        AND allowance_requested >= 0
    ),
    allowance_covered numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (
        bursar.is_finite_numeric(allowance_covered)
        AND allowance_covered >= 0
    ),
    billing_disposition text NOT NULL DEFAULT 'billable'
    CHECK (billing_disposition IN ('billable', 'record_only')),
    catalog_revision_id uuid REFERENCES bursar.catalog_revisions (id),
    plan_id uuid,
    rate_card_key text CHECK (
        rate_card_key IS NULL OR bursar.is_bounded_text(rate_card_key, 255)
    ),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_text(idempotency_key)
        AND bursar.is_bounded_text(idempotency_key, 255)
    ),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, idempotency_key),
    FOREIGN KEY (plan_id, catalog_revision_id)
    REFERENCES bursar.catalog_plans (id, catalog_revision_id),
    CONSTRAINT credit_usage_charges_accounting_by_disposition_check
    CHECK (
        (
            billing_disposition = 'billable'
            AND charged + allowance_covered = requested
        )
        OR
        (
            billing_disposition = 'record_only'
            AND charged = 0
            AND allowance_requested = 0
            AND allowance_covered = 0
        )
    ),
    CHECK (allowance_covered <= allowance_requested)
);

CREATE TABLE bursar.usage_charge_payloads (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    charge_id uuid NOT NULL
    REFERENCES bursar.credit_usage_charges (id) ON DELETE CASCADE,
    event_at timestamptz NOT NULL,
    measures jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(measures, 16384)),
    feature text CHECK (
        feature IS NULL OR bursar.is_bounded_text(feature, 255)
    ),
    model text CHECK (
        model IS NULL OR bursar.is_bounded_text(model, 255)
    ),
    region text CHECK (
        region IS NULL OR bursar.is_bounded_text(region, 255)
    ),
    dimensions jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(dimensions, 65536)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    pricing_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(pricing_snapshot, 32768)),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_at, charge_id)
) PARTITION BY RANGE (event_at);

CREATE TABLE bursar.usage_daily_rollups (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    usage_day date NOT NULL,
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    operation text NOT NULL
    CHECK (bursar.is_bounded_text(operation, 255)),
    model_key text NOT NULL DEFAULT ''
    CHECK (bursar.is_bounded_text(model_key, 255)),
    region_key text NOT NULL DEFAULT ''
    CHECK (bursar.is_bounded_text(region_key, 255)),
    rollup_shard smallint NOT NULL
    CHECK (rollup_shard BETWEEN 0 AND 31),
    charged numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (bursar.is_finite_numeric(charged) AND charged >= 0),
    allowance_covered numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (
        bursar.is_finite_numeric(allowance_covered)
        AND allowance_covered >= 0
    ),
    charge_count bigint NOT NULL DEFAULT 0 CHECK (charge_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        usage_day,
        account_id,
        operation,
        model_key,
        region_key,
        rollup_shard
    )
);

CREATE TABLE bursar.event_outbox (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic text NOT NULL
    CHECK (
        bursar.is_nonempty_text(topic)
        AND bursar.is_bounded_text(topic, 255)
    ),
    aggregate_type text NOT NULL
    CHECK (
        bursar.is_nonempty_text(aggregate_type)
        AND bursar.is_bounded_text(aggregate_type, 100)
    ),
    aggregate_id uuid NOT NULL,
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_text(idempotency_key)
        AND bursar.is_bounded_text(idempotency_key, 255)
    ),
    payload_version smallint NOT NULL DEFAULT 1
    CHECK (payload_version > 0),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(payload, 2097152)),
    status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'delivered', 'dead_letter')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    claim_token uuid,
    claim_expires_at timestamptz,
    last_error text CHECK (
        last_error IS NULL OR bursar.is_bounded_text(last_error, 8192)
    ),
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key),
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
        (status = 'delivered' AND delivered_at IS NOT NULL)
        OR
        (status <> 'delivered' AND delivered_at IS NULL)
    )
);

-- This is the hot current-state row. Replaced assignments are copied to the
-- append-only history table by a trigger before every update or delete.
CREATE TABLE bursar.account_plan_assignments (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    account_id uuid PRIMARY KEY REFERENCES bursar.credit_accounts (id),
    assignment_id uuid NOT NULL DEFAULT bursar.uuid_v7() UNIQUE,
    plan_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    plan_key text NOT NULL,
    revision_policy text NOT NULL
    CHECK (revision_policy IN ('immediate', 'next_renewal', 'pinned')),
    source_type text NOT NULL DEFAULT 'manual'
    CHECK (source_type IN ('manual', 'subscription', 'migration', 'system')),
    source_id uuid,
    starts_at timestamptz NOT NULL DEFAULT now(),
    ends_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (plan_id, catalog_revision_id)
    REFERENCES bursar.catalog_plans (id, catalog_revision_id),
    FOREIGN KEY (catalog_revision_id, plan_key)
    REFERENCES bursar.catalog_plans (catalog_revision_id, plan_key),
    CHECK (ends_at IS NULL OR ends_at > starts_at)
);

CREATE TABLE bursar.account_plan_assignment_history (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assignment_id uuid NOT NULL,
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    plan_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    plan_key text NOT NULL,
    revision_policy text NOT NULL
    CHECK (revision_policy IN ('immediate', 'next_renewal', 'pinned')),
    source_type text NOT NULL,
    source_id uuid,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    replaced_at timestamptz NOT NULL DEFAULT now(),
    replacement_reason text NOT NULL DEFAULT 'reassigned',
    FOREIGN KEY (plan_id, catalog_revision_id)
    REFERENCES bursar.catalog_plans (id, catalog_revision_id),
    CHECK (ends_at > starts_at),
    UNIQUE (assignment_id, ends_at)
);

CREATE TABLE bursar.plan_assignment_changes (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    from_plan_id uuid NOT NULL REFERENCES bursar.catalog_plans (id),
    to_plan_id uuid NOT NULL REFERENCES bursar.catalog_plans (id),
    strategy text NOT NULL CHECK (strategy IN ('immediate', 'next_renewal')),
    effective_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'scheduled'
    CHECK (state IN ('scheduled', 'applied', 'canceled', 'failed')),
    reason text NOT NULL,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz,
    UNIQUE (account_id, to_plan_id, effective_at),
    CHECK (from_plan_id <> to_plan_id),
    CHECK (
        (state = 'applied' AND applied_at IS NOT NULL)
        OR (state <> 'applied' AND applied_at IS NULL)
    )
);

CREATE TABLE bursar.allowance_windows (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    plan_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    allowance_key text NOT NULL
    CHECK (bursar.is_nonempty_text(allowance_key)),
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    period_unit text NOT NULL CHECK (
        period_unit IN (
            'second', 'minute', 'hour', 'day', 'week', 'month', 'year'
        )
    ),
    period_count integer NOT NULL CHECK (period_count > 0),
    period_anchor text NOT NULL
    CHECK (period_anchor IN ('calendar', 'plan_assignment', 'rolling')),
    period_timezone text NOT NULL,
    allowance numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(allowance) AND allowance >= 0),
    reserved numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (bursar.is_finite_numeric(reserved) AND reserved >= 0),
    consumed numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (bursar.is_finite_numeric(consumed) AND consumed >= 0),
    policy_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(policy_snapshot, 32768)),
    CHECK (window_end > window_start),
    CHECK (reserved + consumed <= allowance),
    UNIQUE (
        account_id,
        plan_id,
        catalog_revision_id,
        allowance_key,
        window_start,
        window_end
    ),
    FOREIGN KEY (plan_id, catalog_revision_id)
    REFERENCES bursar.catalog_plans (id, catalog_revision_id)
);

CREATE TABLE bursar.quota_windows (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    plan_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    quota_key text NOT NULL,
    operation_key text NOT NULL,
    measure_key text NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    quota_limit numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(quota_limit) AND quota_limit >= 0),
    reserved numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (bursar.is_finite_numeric(reserved) AND reserved >= 0),
    consumed numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (bursar.is_finite_numeric(consumed) AND consumed >= 0),
    enforcement text NOT NULL CHECK (enforcement IN ('block', 'allow')),
    policy_snapshot jsonb NOT NULL
    CHECK (bursar.is_bounded_json_object(policy_snapshot, 32768)),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (window_end > window_start),
    UNIQUE (
        account_id,
        plan_id,
        catalog_revision_id,
        quota_key,
        window_start,
        window_end
    ),
    FOREIGN KEY (plan_id, catalog_revision_id)
    REFERENCES bursar.catalog_plans (id, catalog_revision_id)
);

-- Immutable measurements are the source of truth for rolling-window quotas
-- and corrections. quota_windows remains a materialized cache for admission.
CREATE TABLE bursar.quota_usage_events (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    plan_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    catalog_quota_id uuid NOT NULL REFERENCES bursar.catalog_plan_quotas (id),
    quota_key text NOT NULL,
    operation_key text NOT NULL,
    measure_key text NOT NULL,
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount <> 0),
    event_at timestamptz NOT NULL,
    usage_charge_id uuid REFERENCES bursar.credit_usage_charges (id),
    correction_of_event_id uuid REFERENCES bursar.quota_usage_events (id),
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_text(idempotency_key)
        AND bursar.is_bounded_text(idempotency_key, 255)
    ),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, catalog_quota_id, idempotency_key),
    FOREIGN KEY (plan_id, catalog_revision_id)
    REFERENCES bursar.catalog_plans (id, catalog_revision_id),
    CHECK (
        correction_of_event_id IS NULL
        OR correction_of_event_id <> id
    ),
    CHECK (
        (amount > 0 AND correction_of_event_id IS NULL)
        OR (amount < 0 AND correction_of_event_id IS NOT NULL)
    )
);

CREATE TABLE bursar.quota_events (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    quota_window_id uuid NOT NULL REFERENCES bursar.quota_windows (id),
    usage_charge_id uuid REFERENCES bursar.credit_usage_charges (id),
    event_type text NOT NULL CHECK (event_type IN ('threshold', 'blocked')),
    threshold_percent integer,
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_text(idempotency_key)
        AND bursar.is_bounded_text(idempotency_key, 255)
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (
        quota_window_id,
        idempotency_key,
        event_type,
        threshold_percent
    ),
    CHECK (
        (
            event_type = 'threshold'
            AND threshold_percent BETWEEN 1 AND 100
        )
        OR
        (event_type = 'blocked' AND threshold_percent IS NULL)
    )
);

CREATE TABLE bursar.credit_leases (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    operation text NOT NULL
    CHECK (
        bursar.is_nonempty_text(operation)
        AND bursar.is_bounded_text(operation, 255)
    ),
    feature text CHECK (
        feature IS NULL OR bursar.is_bounded_text(feature, 255)
    ),
    measures jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(measures, 16384)),
    dimensions jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(dimensions, 65536)),
    policy_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(policy_snapshot, 32768)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions (id),
    plan_id uuid,
    reserved_amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(reserved_amount) AND reserved_amount >= 0),
    reserved_allowance numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (
        bursar.is_finite_numeric(reserved_allowance)
        AND reserved_allowance >= 0
        AND reserved_allowance <= reserved_amount
    ),
    allowance_window_id uuid REFERENCES bursar.allowance_windows (id),
    minimum_balance numeric(20, 6) NOT NULL DEFAULT 0
    CHECK (bursar.is_finite_numeric(minimum_balance)),
    max_concurrent integer CHECK (max_concurrent IS NULL OR max_concurrent > 0),
    expires_at timestamptz NOT NULL,
    status bursar.lease_status NOT NULL DEFAULT 'active',
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_text(idempotency_key)
        AND bursar.is_bounded_text(idempotency_key, 255)
    ),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    settled_amount numeric(20, 6),
    settlement_idempotency_key text CHECK (
        settlement_idempotency_key IS NULL
        OR bursar.is_bounded_text(settlement_idempotency_key, 255)
    ),
    settlement_request_digest bytea
    CHECK (
        settlement_request_digest IS NULL
        OR octet_length(settlement_request_digest) = 32
    ),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, idempotency_key),
    FOREIGN KEY (plan_id, catalog_revision_id)
    REFERENCES bursar.catalog_plans (id, catalog_revision_id),
    CHECK (
        settled_amount IS NULL
        OR (bursar.is_finite_numeric(settled_amount) AND settled_amount >= 0)
    ),
    CHECK (
        (
            status = 'settled'
            AND settled_amount IS NOT NULL
            AND settlement_idempotency_key IS NOT NULL
            AND settlement_request_digest IS NOT NULL
        )
        OR (
            status <> 'settled'
            AND settled_amount IS NULL
            AND settlement_idempotency_key IS NULL
            AND settlement_request_digest IS NULL
            AND ledger_entry_id IS NULL
        )
    ),
    CHECK (
        reserved_allowance = 0
        OR plan_id IS NOT NULL
    ),
    CHECK (
        allowance_window_id IS NULL
        OR plan_id IS NOT NULL
    ),
    CHECK (expires_at > created_at)
);

-- A lease can reserve several independently metered plan quotas. Fixed
-- windows point at their materialized quota window; rolling windows retain
-- their admission-time bounds and are recomputed from immutable usage events.
CREATE TABLE bursar.credit_lease_quota_reservations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    lease_id uuid NOT NULL REFERENCES bursar.credit_leases (id),
    catalog_quota_id uuid NOT NULL REFERENCES bursar.catalog_plan_quotas (id),
    quota_window_id uuid REFERENCES bursar.quota_windows (id),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount >= 0),
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    released_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lease_id, catalog_quota_id),
    CHECK (window_end > window_start),
    CHECK (released_at IS NULL OR released_at >= created_at)
);

CREATE TABLE bursar.credit_teams (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, subject_id)
);

CREATE TABLE bursar.credit_team_members (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    team_id uuid NOT NULL
    REFERENCES bursar.credit_teams (id) ON DELETE CASCADE,
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    spend_cap numeric(20, 6)
    CHECK (
        spend_cap IS NULL
        OR (
            bursar.is_finite_numeric(spend_cap)
            AND spend_cap >= 0
        )
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, subject_id)
);

-- Team usage is attributed to the member that initiated it. This keeps the
-- monetary ledger account-centric while making member spend caps auditable
-- and atomically enforceable.
CREATE TABLE bursar.credit_team_usage_charges (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    team_id uuid NOT NULL REFERENCES bursar.credit_teams (id),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    ledger_entry_id uuid NOT NULL UNIQUE
    REFERENCES bursar.credit_ledger_entries (id),
    operation text NOT NULL CHECK (bursar.is_nonempty_text(operation)),
    amount numeric(20, 6) NOT NULL
    CHECK (bursar.is_finite_numeric(amount) AND amount > 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    idempotency_key text NOT NULL
    CHECK (
        bursar.is_nonempty_text(idempotency_key)
        AND bursar.is_bounded_text(idempotency_key, 255)
    ),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (team_id, idempotency_key),
    FOREIGN KEY (team_id, subject_id)
    REFERENCES bursar.credit_team_members (team_id, subject_id)
);

CREATE TABLE bursar.grant_program_events (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL,
    grant_program_id uuid NOT NULL,
    program_key text NOT NULL CHECK (bursar.is_nonempty_text(program_key)),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    event_key text NOT NULL CHECK (bursar.is_nonempty_text(event_key)),
    idempotency_scope text NOT NULL
    CHECK (idempotency_scope IN ('subject', 'event')),
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_text(idempotency_key)),
    referrer_subject_id uuid REFERENCES bursar.subjects (id),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Program keys are stable business identities across catalog revisions.
    -- This prevents a catalog publish from accidentally re-awarding a
    -- lifetime promotion whose projection received a new UUID.
    UNIQUE (tenant_id, program_key, subject_id, idempotency_key),
    FOREIGN KEY (grant_program_id, catalog_revision_id)
    REFERENCES bursar.catalog_grant_programs (id, catalog_revision_id)
);

CREATE TABLE bursar.grant_award_executions (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    grant_event_id uuid NOT NULL REFERENCES bursar.grant_program_events (id),
    catalog_grant_award_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    recipient_subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    ledger_entry_id uuid NOT NULL UNIQUE
    REFERENCES bursar.credit_ledger_entries (id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (grant_event_id, catalog_grant_award_id),
    FOREIGN KEY (catalog_grant_award_id, catalog_revision_id)
    REFERENCES bursar.catalog_grant_awards (id, catalog_revision_id)
);

CREATE TABLE bursar.credit_plan_migrations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    from_plan_id uuid REFERENCES bursar.catalog_plans (id),
    to_plan_id uuid NOT NULL REFERENCES bursar.catalog_plans (id),
    strategy text NOT NULL DEFAULT 'immediate'
    CHECK (strategy IN ('immediate', 'next_renewal')),
    effective_at timestamptz,
    cursor_account_id uuid,
    migrated_count integer NOT NULL DEFAULT 0 CHECK (migrated_count >= 0),
    status text NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'completed', 'failed', 'canceled')),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (from_plan_id IS NULL OR from_plan_id <> to_plan_id)
);
