-- Migration: 003_financial_policy_tables.sql
-- Purpose: Define immutable credit provenance, usage accounting, policy
--   windows, reservations, team attribution, and grant execution state.
-- Depends on: 002_schema_types_tables.sql.
-- Security: Makes every financial fact tenant-owned and preserves exact audit lineage.

-- Contents
--   1. Immutable ledger and lot provenance
--   2. Usage payload contracts and charges
--   3. Usage projections and transactional outbox
--   4. Plan assignments and policy windows
--   5. Quota measurements and notifications
--   6. Credit leases and quota reservations
--   7. Team accounting
--   8. Grant execution and plan migration

-- 1. Immutable ledger and lot provenance

-- Record every exact balance movement with its resulting balance, request
-- digest, and account-scoped idempotency key as the financial audit source.
CREATE TABLE bursar.credit_ledger_entries (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    kind bursar.ledger_entry_kind NOT NULL,
    amount bursar.credit_numeric NOT NULL
    CHECK (amount <> 0),
    balance_after bursar.credit_numeric NOT NULL,
    reference_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    catalog_revision_id uuid REFERENCES bursar.catalog_revisions (id),
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(idempotency_key, 255)),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    operation text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(operation, 255)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
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

-- Track funded credit lots independently from the aggregate balance so priority,
-- expiry, consumption, provenance, and catalog-pinned policy remain reconcilable.
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
    granted bursar.credit_numeric NOT NULL
    CHECK (granted > 0),
    consumed bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (
        consumed >= 0
        AND consumed <= granted
    ),
    expires_at timestamptz CHECK (expires_at IS NULL OR bursar.is_finite_timestamptz(expires_at)),
    expiry_policy_snapshot jsonb NOT NULL DEFAULT '{"type":"never"}'::jsonb
    CHECK (
        bursar.is_bounded_json_object(expiry_policy_snapshot, 32768)
        AND bursar.matches_catalog_fragment(
            expiry_policy_snapshot,
            bursar.catalog_document_shape_schema()
            -> '$defs' -> 'BucketDefinition' -> 'properties' -> 'expiry'
        )
    ),
    source_type text NOT NULL DEFAULT 'ledger'
    CHECK (source_type IN (
        'ledger', 'grant_program', 'topup', 'subscription_cycle',
        'refund', 'adjustment'
    )),
    source_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    FOREIGN KEY (catalog_revision_id, bucket_key)
    REFERENCES bursar.catalog_buckets (catalog_revision_id, bucket_key)
);

-- Preserve each positive ledger contribution to a lot, including merged lots,
-- so later clawbacks can identify the credits' true business origin.
CREATE TABLE bursar.credit_lot_sources (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    lot_id uuid NOT NULL REFERENCES bursar.credit_lots (id) ON DELETE CASCADE,
    ledger_entry_id uuid NOT NULL UNIQUE
    REFERENCES bursar.credit_ledger_entries (id),
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    source_type text NOT NULL CHECK (bursar.is_nonempty_text(source_type)),
    source_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at))
);

-- Attribute each debit to the lots it consumes; positive allocation amounts
-- intentionally represent the magnitude of a negative ledger movement.
CREATE TABLE bursar.credit_lot_allocations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    debit_entry_id uuid NOT NULL
    REFERENCES bursar.credit_ledger_entries (id),
    lot_id uuid NOT NULL REFERENCES bursar.credit_lots (id),
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    allocation_kind text NOT NULL
    CHECK (allocation_kind IN ('spend', 'expiry', 'revocation', 'clawback')),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (debit_entry_id, lot_id)
);

-- Split a lot allocation across its contributing sources so merged balances do
-- not erase provider/grant provenance during refunds or revocations.
CREATE TABLE bursar.credit_lot_source_allocations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    lot_allocation_id uuid NOT NULL
    REFERENCES bursar.credit_lot_allocations (id),
    lot_source_id uuid NOT NULL REFERENCES bursar.credit_lot_sources (id),
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (lot_allocation_id, lot_source_id)
);

-- Link refund credits back to original lot allocations, preventing restoration
-- beyond the spend that was actually attributed to that lot.
CREATE TABLE bursar.credit_lot_restorations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    refund_entry_id uuid NOT NULL
    REFERENCES bursar.credit_ledger_entries (id),
    original_allocation_id uuid NOT NULL
    REFERENCES bursar.credit_lot_allocations (id),
    lot_id uuid NOT NULL REFERENCES bursar.credit_lots (id),
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (refund_entry_id, original_allocation_id)
);

-- Restore source-level provenance alongside each lot restoration so subsequent
-- clawback and reconciliation calculations remain exact.
CREATE TABLE bursar.credit_lot_source_restorations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    lot_restoration_id uuid NOT NULL
    REFERENCES bursar.credit_lot_restorations (id),
    source_allocation_id uuid NOT NULL
    REFERENCES bursar.credit_lot_source_allocations (id),
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (lot_restoration_id, source_allocation_id)
);

-- Represent credit-line usage or refund debt that cannot consume a funded lot;
-- these positive magnitudes explain the account's below-funded balance.
CREATE TABLE bursar.credit_unallocated_debits (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    ledger_entry_id uuid PRIMARY KEY
    REFERENCES bursar.credit_ledger_entries (id),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    reason text NOT NULL CHECK (reason IN ('credit_line', 'refund_debt')),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at))
);

-- Identify positive ledger movements applied to outstanding unallocated debt,
-- separating debt reduction from newly spendable credit issuance.
CREATE TABLE bursar.credit_debt_repayments (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    ledger_entry_id uuid PRIMARY KEY
    REFERENCES bursar.credit_ledger_entries (id),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at))
);

-- 2. Usage payload contracts and charges

-- Define operation measures as JSON numbers or exact decimal strings; the
-- validator below applies byte bounds, finiteness, and non-negativity.
CREATE FUNCTION bursar.measure_object_schema()
RETURNS json
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $function$
    SELECT $schema$
    {
      "type": "object",
      "propertyNames": {
        "type": "string",
        "minLength": 1,
        "maxLength": 255
      },
      "additionalProperties": {
        "anyOf": [
          {"type": "number"},
          {"type": "string"}
        ]
      }
    }
    $schema$::json
$function$;

-- Restrict dimension values to scalar JSON so grouping keys remain bounded and
-- predictable across PostgreSQL and external analytics backends.
CREATE FUNCTION bursar.dimension_object_schema()
RETURNS json
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $function$
    SELECT $schema$
    {
      "type": "object",
      "propertyNames": {
        "type": "string",
        "minLength": 1,
        "maxLength": 255
      },
      "additionalProperties": {
        "anyOf": [
          {"type": "string"},
          {"type": "number"},
          {"type": "boolean"}
        ]
      }
    }
    $schema$::json
$function$;

-- Freeze the minimum pricing result needed to prove how requested usage split
-- between allowance coverage, charged credits, and record-only telemetry.
CREATE FUNCTION bursar.usage_pricing_snapshot_schema()
RETURNS json
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $function$
    SELECT $schema$
    {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "requested": {
          "type": "string",
          "pattern": "^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?$"
        },
        "allowance_covered": {
          "type": "string",
          "pattern": "^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?$"
        },
        "charged": {
          "type": "string",
          "pattern": "^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?$"
        },
        "billing_disposition": {
          "enum": ["billable", "record_only"]
        }
      },
      "required": [
        "requested",
        "allowance_covered",
        "charged",
        "billing_disposition"
      ]
    }
    $schema$::json
$function$;

-- Validate every measure as an exact finite nonnegative number after structural
-- and byte-size checks, rejecting JSON numbers PostgreSQL numeric cannot hold.
CREATE FUNCTION bursar.valid_measure_object(
    p_value jsonb,
    p_max_bytes integer DEFAULT 65536
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path TO ''
AS $$
DECLARE
    v_value jsonb;
    v_number numeric;
BEGIN
    IF NOT bursar.is_bounded_json_object(
        COALESCE(p_value, '{}'::jsonb),
        p_max_bytes
    ) OR NOT extensions.jsonb_matches_schema(
        bursar.measure_object_schema(),
        COALESCE(p_value, '{}'::jsonb)
    ) THEN
        RETURN false;
    END IF;

    FOR v_value IN
        SELECT entry.value
        FROM jsonb_each(COALESCE(p_value, '{}'::jsonb)) AS entry
    LOOP
        BEGIN
            v_number := btrim(v_value #>> '{}')::numeric;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN false;
        END;

        -- Measures are exact source quantities, not stored credit amounts; they
        -- may legitimately exceed six fractional places or numeric(20,6).
        IF NOT bursar.is_finite_numeric(v_number) OR v_number < 0 THEN
            RETURN false;
        END IF;
    END LOOP;

    RETURN true;
END
$$;

-- Apply the shared scalar shape and byte ceiling to tenant-controlled dimensions
-- before they reach payload partitions or analytical grouping keys.
CREATE FUNCTION bursar.valid_dimension_object(
    p_value jsonb,
    p_max_bytes integer DEFAULT 65536
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT bursar.is_bounded_json_object(
        COALESCE(p_value, '{}'::jsonb),
        p_max_bytes
    ) AND extensions.jsonb_matches_schema(
        bursar.dimension_object_schema(),
        COALESCE(p_value, '{}'::jsonb)
    )
$$;

-- Store the durable, low-cardinality usage charge fact with exact accounting:
-- billable usage must split fully, while record-only usage moves no credits.
CREATE TABLE bursar.credit_usage_charges (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    operation text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(operation, 255)),
    event_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(event_at)),
    requested bursar.credit_numeric NOT NULL
    CHECK (requested >= 0),
    charged bursar.credit_numeric NOT NULL
    CHECK (charged >= 0),
    allowance_requested bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (
        allowance_requested >= 0
    ),
    allowance_covered bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (
        allowance_covered >= 0
    ),
    billing_disposition text NOT NULL DEFAULT 'billable'
    CHECK (billing_disposition IN ('billable', 'record_only')),
    catalog_revision_id uuid REFERENCES bursar.catalog_revisions (id),
    plan_id uuid,
    rate_card_key text CHECK (
        rate_card_key IS NULL OR bursar.is_nonempty_text(rate_card_key)
    ),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries (id),
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(idempotency_key, 255)),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (account_id, idempotency_key),
    UNIQUE (id, event_at),
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

-- Keep high-cardinality measures, dimensions, and metadata in a time-partitioned
-- payload store whose retention can differ from the permanent financial fact.
CREATE TABLE bursar.usage_charge_payloads (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    charge_id uuid NOT NULL,
    event_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(event_at)),
    measures jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.valid_measure_object(measures, 16384)),
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
    CHECK (bursar.valid_dimension_object(dimensions, 65536)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    -- Provider reconciliation receipts and other high-cardinality details are
    -- retained only here (or in ClickHouse), never in permanent ledger rows.
    CHECK (bursar.is_bounded_json_object(metadata, 1048576)),
    pricing_snapshot jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(pricing_snapshot, 32768)
        AND extensions.jsonb_matches_schema(
            bursar.usage_pricing_snapshot_schema(),
            pricing_snapshot
        )
    ),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    PRIMARY KEY (event_at, charge_id),
    FOREIGN KEY (charge_id, event_at)
    REFERENCES bursar.credit_usage_charges (id, event_at)
    ON DELETE CASCADE
) PARTITION BY RANGE (event_at);

-- 3. Usage projections and transactional outbox

-- Materialize daily reporting totals with deterministic 32-way sharding to
-- reduce contention on popular account/operation dimensions.
CREATE TABLE bursar.usage_daily_rollups (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    usage_day date NOT NULL CHECK (bursar.is_finite_date(usage_day)),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    operation text NOT NULL
    CHECK (bursar.is_bounded_text(operation, 255)),
    model_key text NOT NULL DEFAULT ''
    CHECK (bursar.is_bounded_text(model_key, 255)),
    region_key text NOT NULL DEFAULT ''
    CHECK (bursar.is_bounded_text(region_key, 255)),
    rollup_shard smallint NOT NULL
    CHECK (rollup_shard BETWEEN 0 AND 31),
    charged bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (charged >= 0),
    allowance_covered bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (
        allowance_covered >= 0
    ),
    charge_count bigint NOT NULL DEFAULT 0 CHECK (charge_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    PRIMARY KEY (
        usage_day,
        account_id,
        operation,
        model_key,
        region_key,
        rollup_shard
    )
);

-- Commit integration events in the same transaction as accounting changes;
-- lease fields make delivery retryable and tenant idempotency prevents repeats.
CREATE TABLE bursar.event_outbox (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(topic, 255)),
    aggregate_type text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(aggregate_type, 100)),
    aggregate_id uuid NOT NULL,
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(idempotency_key, 255)),
    payload_version smallint NOT NULL DEFAULT 1
    CHECK (payload_version > 0),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(payload, 2097152)),
    status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'delivered', 'dead_letter')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(available_at)),
    claim_token uuid,
    claim_expires_at timestamptz CHECK (claim_expires_at IS NULL OR bursar.is_finite_timestamptz(claim_expires_at)),
    last_error text CHECK (
        last_error IS NULL
        OR bursar.is_nonempty_bounded_text(last_error, 8192)
    ),
    delivered_at timestamptz CHECK (delivered_at IS NULL OR bursar.is_finite_timestamptz(delivered_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
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

-- 4. Plan assignments and policy windows

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
    catalog_revision_pinned boolean NOT NULL DEFAULT FALSE,
    source_type text NOT NULL DEFAULT 'manual'
    CHECK (source_type IN ('manual', 'subscription', 'migration', 'system')),
    source_id uuid,
    starts_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(starts_at)),
    ends_at timestamptz CHECK (ends_at IS NULL OR bursar.is_finite_timestamptz(ends_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    FOREIGN KEY (plan_id, catalog_revision_id, plan_key)
    REFERENCES bursar.catalog_plans (
        id,
        catalog_revision_id,
        plan_key
    ),
    CHECK (ends_at IS NULL OR ends_at > starts_at)
);

-- Preserve closed assignment intervals as effective-dated evidence, including
-- the catalog revision and replacement reason for historical policy decisions.
CREATE TABLE bursar.account_plan_assignment_history (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assignment_id uuid NOT NULL,
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    plan_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    plan_key text NOT NULL,
    catalog_revision_pinned boolean NOT NULL,
    source_type text NOT NULL,
    source_id uuid,
    starts_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(starts_at)),
    ends_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(ends_at)),
    replaced_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(replaced_at)),
    replacement_reason text NOT NULL DEFAULT 'reassigned'
    CHECK (bursar.is_nonempty_text(replacement_reason)),
    FOREIGN KEY (plan_id, catalog_revision_id, plan_key)
    REFERENCES bursar.catalog_plans (
        id,
        catalog_revision_id,
        plan_key
    ),
    CHECK (ends_at >= starts_at),
    UNIQUE (assignment_id, ends_at)
);

-- Queue immediate or renewal-bound plan transitions with explicit terminal
-- states, retaining failures instead of silently mutating current assignment.
CREATE TABLE bursar.plan_assignment_changes (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    from_plan_id uuid NOT NULL REFERENCES bursar.catalog_plans (id),
    to_plan_id uuid NOT NULL REFERENCES bursar.catalog_plans (id),
    change_kind text NOT NULL DEFAULT 'manual'
    CHECK (change_kind IN ('manual', 'catalog_revision')),
    pin_overridden boolean NOT NULL DEFAULT FALSE,
    strategy text NOT NULL CHECK (strategy IN ('immediate', 'next_renewal')),
    effective_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(effective_at)),
    state text NOT NULL DEFAULT 'scheduled'
    CHECK (state IN ('scheduled', 'applied', 'canceled', 'failed')),
    reason text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(reason, 2048)),
    error_message text CHECK (
        error_message IS NULL
        OR bursar.is_nonempty_bounded_text(error_message, 8192)
    ),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    applied_at timestamptz CHECK (applied_at IS NULL OR bursar.is_finite_timestamptz(applied_at)),
    CHECK (from_plan_id <> to_plan_id),
    CHECK (
        (state = 'applied' AND applied_at IS NOT NULL)
        OR (state <> 'applied' AND applied_at IS NULL)
    )
);

-- Materialize exact allowance reservation/consumption for a concrete time
-- interval and catalog policy snapshot, avoiding reinterpretation after publish.
CREATE TABLE bursar.allowance_windows (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    plan_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL,
    allowance_key text NOT NULL
    CHECK (bursar.is_nonempty_text(allowance_key)),
    window_start timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(window_start)),
    window_end timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(window_end)),
    period_unit text NOT NULL CHECK (
        period_unit IN (
            'second', 'minute', 'hour', 'day', 'week', 'month', 'year'
        )
    ),
    period_count integer NOT NULL CHECK (period_count > 0),
    period_anchor text NOT NULL
    CHECK (period_anchor IN ('calendar', 'plan_assignment', 'rolling')),
    period_timezone text NOT NULL,
    allowance bursar.credit_numeric NOT NULL
    CHECK (allowance >= 0),
    reserved bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (reserved >= 0),
    consumed bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (consumed >= 0),
    policy_snapshot jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(policy_snapshot, 32768)
        AND bursar.matches_catalog_definitions(
            policy_snapshot,
            'CreditAllowance'
        )
    ),
    CHECK (window_end > window_start),
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

-- Cache windowed quota state for admission while retaining exact limit,
-- enforcement, and revision-pinned policy used to make the decision.
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
    window_start timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(window_start)),
    window_end timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(window_end)),
    quota_limit bursar.credit_numeric NOT NULL
    CHECK (quota_limit >= 0),
    reserved bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (reserved >= 0),
    consumed bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (consumed >= 0),
    enforcement text NOT NULL CHECK (enforcement IN ('block', 'allow')),
    policy_snapshot jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(policy_snapshot, 32768)
        AND bursar.matches_catalog_definitions(
            policy_snapshot,
            'QuotaDefinition'
        )
    ),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
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

-- 5. Quota measurements and notifications

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
    amount bursar.credit_numeric NOT NULL
    CHECK (amount <> 0),
    event_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(event_at)),
    usage_charge_id uuid REFERENCES bursar.credit_usage_charges (id),
    correction_of_event_id uuid REFERENCES bursar.quota_usage_events (id),
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(idempotency_key, 255)),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
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

-- Deduplicate threshold and blocked notifications per quota window so retries
-- can enqueue observable events without emitting the same boundary twice.
CREATE TABLE bursar.quota_events (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    quota_window_id uuid NOT NULL REFERENCES bursar.quota_windows (id),
    usage_charge_id uuid REFERENCES bursar.credit_usage_charges (id),
    event_type text NOT NULL CHECK (event_type IN ('threshold', 'blocked')),
    threshold_percent integer,
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(idempotency_key, 255)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
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

-- 6. Credit leases and quota reservations

-- Reserve credits and allowance before work starts, then require one complete
-- settlement receipt or a non-settled row with no terminal accounting artifacts.
CREATE TABLE bursar.credit_leases (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts (id),
    operation text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(operation, 255)),
    feature text CHECK (
        feature IS NULL OR bursar.is_bounded_text(feature, 255)
    ),
    measures jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.valid_measure_object(measures, 16384)),
    dimensions jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.valid_dimension_object(dimensions, 65536)),
    policy_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(policy_snapshot, 32768)),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions (id),
    plan_id uuid,
    reserved_amount bursar.credit_numeric NOT NULL
    CHECK (reserved_amount >= 0),
    reserved_allowance bursar.credit_numeric NOT NULL DEFAULT 0
    CHECK (
        reserved_allowance >= 0
        AND reserved_allowance <= reserved_amount
    ),
    allowance_window_id uuid REFERENCES bursar.allowance_windows (id),
    minimum_balance bursar.credit_numeric NOT NULL DEFAULT 0,
    max_concurrent integer CHECK (max_concurrent IS NULL OR max_concurrent > 0),
    expires_at timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(expires_at)),
    status bursar.lease_status NOT NULL DEFAULT 'active',
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(idempotency_key, 255)),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    settled_amount bursar.credit_numeric,
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
    usage_charge_id uuid REFERENCES bursar.credit_usage_charges (id),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    UNIQUE (account_id, idempotency_key),
    FOREIGN KEY (plan_id, catalog_revision_id)
    REFERENCES bursar.catalog_plans (id, catalog_revision_id),
    CHECK (
        settled_amount IS NULL
        OR settled_amount >= 0
    ),
    CHECK (
        (
            status = 'settled'
            AND settled_amount IS NOT NULL
            AND settlement_idempotency_key IS NOT NULL
            AND settlement_request_digest IS NOT NULL
            AND usage_charge_id IS NOT NULL
        )
        OR (
            status <> 'settled'
            AND settled_amount IS NULL
            AND settlement_idempotency_key IS NULL
            AND settlement_request_digest IS NULL
            AND ledger_entry_id IS NULL
            AND usage_charge_id IS NULL
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
    amount bursar.credit_numeric NOT NULL
    CHECK (amount >= 0),
    window_start timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(window_start)),
    window_end timestamptz NOT NULL
    CHECK (bursar.is_finite_timestamptz(window_end)),
    released_at timestamptz CHECK (released_at IS NULL OR bursar.is_finite_timestamptz(released_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    PRIMARY KEY (lease_id, catalog_quota_id),
    CHECK (window_end > window_start),
    CHECK (released_at IS NULL OR released_at >= created_at)
);

-- 7. Team accounting

-- Give each tenant subject at most one team-owned accounting principal and make
-- creation replay-safe with a request digest and tenant idempotency key.
CREATE TABLE bursar.credit_teams (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
    creation_idempotency_key text NOT NULL
    CONSTRAINT credit_teams_creation_idempotency_key_check
    CHECK (bursar.is_nonempty_bounded_text(creation_idempotency_key, 255)),
    creation_request_digest bytea NOT NULL
    CONSTRAINT credit_teams_creation_request_digest_check
    CHECK (octet_length(creation_request_digest) = 32),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (tenant_id, subject_id),
    CONSTRAINT credit_teams_creation_idempotency_key_unique
    UNIQUE (tenant_id, creation_idempotency_key)
);

-- Record membership lifecycle, authorization role, and optional exact spend cap;
-- departed membership remains auditable through left_at.
CREATE TABLE bursar.credit_team_members (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    team_id uuid NOT NULL
    REFERENCES bursar.credit_teams (id) ON DELETE CASCADE,
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    spend_cap bursar.credit_numeric
    CHECK (
        spend_cap IS NULL
        OR spend_cap >= 0
    ),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    left_at timestamptz CHECK (left_at IS NULL OR bursar.is_finite_timestamptz(left_at)),
    PRIMARY KEY (team_id, subject_id),
    CHECK (left_at IS NULL OR left_at >= created_at)
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
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (bursar.is_bounded_json_object(metadata, 16384)),
    idempotency_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(idempotency_key, 255)),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (team_id, idempotency_key),
    FOREIGN KEY (team_id, subject_id)
    REFERENCES bursar.credit_team_members (team_id, subject_id)
);

-- 8. Grant execution and plan migration

-- Capture a qualifying program event against its catalog projection while
-- deduplicating on the stable program key across catalog revisions.
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
    occurred_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(occurred_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    -- Program keys are stable business identities across catalog revisions.
    -- This prevents a catalog publish from accidentally re-awarding a
    -- lifetime promotion whose projection received a new UUID.
    UNIQUE (tenant_id, program_key, subject_id, idempotency_key),
    FOREIGN KEY (grant_program_id, catalog_revision_id)
    REFERENCES bursar.catalog_grant_programs (id, catalog_revision_id)
);

-- Prove each configured award executed once for an event and link the resulting
-- positive ledger entry to its exact revision-local award definition.
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
    granted_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(granted_at)),
    UNIQUE (grant_event_id, catalog_grant_award_id),
    FOREIGN KEY (catalog_grant_award_id, catalog_revision_id)
    REFERENCES bursar.catalog_grant_awards (id, catalog_revision_id)
);

-- Track resumable bulk plan rollout by cursor and terminal state so operators
-- can continue after failure without reprocessing already migrated accounts.
CREATE TABLE bursar.credit_plan_migrations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    from_plan_id uuid REFERENCES bursar.catalog_plans (id),
    to_plan_id uuid NOT NULL REFERENCES bursar.catalog_plans (id),
    strategy text NOT NULL DEFAULT 'immediate'
    CHECK (strategy IN ('immediate', 'next_renewal')),
    effective_at timestamptz CHECK (effective_at IS NULL OR bursar.is_finite_timestamptz(effective_at)),
    cursor_account_id uuid,
    migrated_count integer NOT NULL DEFAULT 0 CHECK (migrated_count >= 0),
    status text NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'completed', 'failed', 'canceled')),
    last_error text CHECK (
        last_error IS NULL
        OR bursar.is_nonempty_bounded_text(last_error, 8192)
    ),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    CHECK (from_plan_id IS NULL OR from_plan_id <> to_plan_id)
);
