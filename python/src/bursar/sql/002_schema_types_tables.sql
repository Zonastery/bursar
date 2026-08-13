-- Migration: 002_schema_types_tables.sql
-- Purpose: Define lifecycle types and the tenant-owned catalog, identity, and
--   credit-account foundations used by later financial and billing tables.
-- Depends on: 001_schema_and_types.sql.
-- Security: Defaults ownership to required tenant context and namespaces provider IDs.

-- Contents
--   1. Lifecycle and accounting types
--   2. Storage retention configuration
--   3. Tenant subjects and provider identities
--   4. Catalog revision ledger
--   5. Catalog pricing and policy projections
--   6. Plan and commerce projections
--   7. Credit accounts

-- 1. Lifecycle and accounting types

-- Catalog states support guarded publication, activation, retirement, and
-- reactivation; later triggers serialize activation and preserve published content.
CREATE TYPE bursar.catalog_revision_status AS ENUM (
    'draft',
    'published',
    'active',
    'retired'
);

-- Ledger kinds encode why exact credits moved so downstream reconciliation can
-- distinguish issuance, consumption, reservation, and corrective entries.
CREATE TYPE bursar.ledger_entry_kind AS ENUM (
    'grant',
    'purchase',
    'usage',
    'expiry',
    'revocation',
    'refund',
    'refund_clawback',
    'adjustment',
    'reservation',
    'release'
);

-- Lease states separate spend authorization from terminal settlement/release,
-- supporting retry-safe ownership of reserved credits.
CREATE TYPE bursar.lease_status AS ENUM (
    'active',
    'settling',
    'settled',
    'released',
    'expired'
);

-- Payment states retain provider lifecycle detail needed to validate monotonic
-- transitions and distinguish reversible success from terminal failure.
CREATE TYPE bursar.billing_payment_status AS ENUM (
    'pending',
    'requires_action',
    'succeeded',
    'failed',
    'canceled',
    'refunded',
    'partially_refunded',
    'disputed'
);

-- Subscription states preserve provider distinctions that affect entitlement
-- eligibility and stale-event conflict resolution.
CREATE TYPE bursar.billing_subscription_status AS ENUM (
    'incomplete',
    'incomplete_expired',
    'trialing',
    'active',
    'past_due',
    'paused',
    'canceled',
    'unpaid',
    'expired'
);

-- Webhook processing states support leases, replay, ignore, and explicit dead
-- lettering without treating receipt as successful application.
CREATE TYPE bursar.billing_event_status AS ENUM (
    'processing',
    'completed',
    'failed',
    'ignored',
    'dead_letter'
);

-- Recharge attempts model asynchronous provider uncertainty explicitly so an
-- unknown charge is reconciled instead of being blindly retried.
CREATE TYPE bursar.recharge_attempt_status AS ENUM (
    'claimed',
    'submitted',
    'processing',
    'succeeded',
    'failed',
    'unknown',
    'action_required',
    'canceled'
);

-- 2. Storage retention configuration

-- Keep one operator-controlled retention contract whose cross-field checks
-- ensure payloads outlive outbox replay and quota correction horizons.
CREATE TABLE bursar.storage_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    usage_payload_retention_days integer NOT NULL DEFAULT 90
    CHECK (usage_payload_retention_days BETWEEN 1 AND 36500),
    quota_event_retention_days integer NOT NULL DEFAULT 45
    CHECK (quota_event_retention_days BETWEEN 1 AND 36500),
    quota_max_lateness_seconds integer NOT NULL DEFAULT 604800
    CHECK (quota_max_lateness_seconds BETWEEN 0 AND 31536000),
    quota_correction_window_days integer NOT NULL DEFAULT 7
    CHECK (quota_correction_window_days BETWEEN 0 AND 3650),
    quota_retention_safety_days integer NOT NULL DEFAULT 1
    CHECK (quota_retention_safety_days BETWEEN 1 AND 365),
    billing_payload_retention_days integer NOT NULL DEFAULT 30
    CHECK (billing_payload_retention_days BETWEEN 1 AND 36500),
    quota_notification_retention_days integer NOT NULL DEFAULT 90
    CHECK (quota_notification_retention_days BETWEEN 1 AND 36500),
    terminal_lease_payload_retention_days integer NOT NULL DEFAULT 30
    CHECK (terminal_lease_payload_retention_days BETWEEN 1 AND 36500),
    usage_rollup_retention_days integer NOT NULL DEFAULT 730
    CHECK (usage_rollup_retention_days BETWEEN 1 AND 36500),
    outbox_delivered_retention_days integer NOT NULL DEFAULT 7
    CHECK (outbox_delivered_retention_days BETWEEN 1 AND 36500),
    outbox_max_retention_days integer NOT NULL DEFAULT 30
    CHECK (outbox_max_retention_days BETWEEN 1 AND 36500),
    maintenance_interval_seconds integer NOT NULL DEFAULT 60
    CHECK (maintenance_interval_seconds BETWEEN 1 AND 86400),
    maintenance_batch_size integer NOT NULL DEFAULT 500
    CHECK (maintenance_batch_size BETWEEN 1 AND 5000),
    maintenance_lock_timeout_ms integer NOT NULL DEFAULT 100
    CHECK (maintenance_lock_timeout_ms BETWEEN 1 AND 5000),
    last_maintenance_at timestamptz CHECK (
        last_maintenance_at IS null OR bursar.is_finite_timestamptz(last_maintenance_at)
    ),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    CHECK (
        quota_event_retention_days * 86400::bigint
        >= quota_max_lateness_seconds
        + quota_correction_window_days * 86400::bigint
        + quota_retention_safety_days * 86400::bigint
    ),
    CHECK (outbox_max_retention_days >= outbox_delivered_retention_days),
    CHECK (usage_payload_retention_days >= outbox_max_retention_days),
    CHECK (billing_payload_retention_days >= outbox_max_retention_days)
);

INSERT INTO bursar.storage_settings (singleton) VALUES (true);

-- 3. Tenant subjects and provider identities

-- Subjects are deliberately provider-neutral accounting principals; lifecycle
-- deletion is represented by pseudonymization rather than erasing history.
CREATE TABLE bursar.subjects (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    pseudonymized_at timestamptz CHECK (pseudonymized_at IS null OR bursar.is_finite_timestamptz(pseudonymized_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at))
);

-- Map a provider/environment identity to its tenant-local subject, preventing
-- live, test, and sandbox identifiers from colliding or crossing tenants.
CREATE TABLE bursar.external_identities (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL
    REFERENCES bursar.subjects (id) ON DELETE CASCADE,
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    external_subject text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(external_subject, 255)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (
        tenant_id,
        provider,
        provider_environment,
        external_subject
    )
);

-- 4. Catalog revision ledger

-- Allocate revision numbers independently per tenant without serializing
-- unrelated tenants on a global sequence.
CREATE TABLE bursar.tenant_catalog_counters (
    tenant_id uuid PRIMARY KEY DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE CASCADE,
    next_revision_no bigint NOT NULL DEFAULT 1
    CHECK (next_revision_no > 0)
);

COMMENT ON TABLE bursar.tenant_catalog_counters IS
'Per-tenant catalog revision allocator.';

-- Atomically reserve the next tenant-local revision number; gaps are acceptable
-- because the number is an ordering key, not a financial sequence.
CREATE FUNCTION bursar.next_catalog_revision_no()
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
SET search_path TO ''
AS $$
DECLARE
    v_tenant uuid := bursar.require_tenant_id();
    v_revision bigint;
BEGIN
    INSERT INTO bursar.tenant_catalog_counters(
        tenant_id,
        next_revision_no
    )
    VALUES (v_tenant, 2)
    ON CONFLICT (tenant_id) DO UPDATE
    SET next_revision_no
        = bursar.tenant_catalog_counters.next_revision_no + 1
    RETURNING next_revision_no - 1 INTO v_revision;

    RETURN v_revision;
END
$$;

REVOKE ALL
ON FUNCTION bursar.next_catalog_revision_no()
FROM PUBLIC;

-- Preserve source documents and digests as revision evidence; content becomes
-- immutable once a revision leaves draft, and timestamps audit its lifecycle.
CREATE TABLE bursar.catalog_revisions (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    revision_no bigint NOT NULL DEFAULT bursar.next_catalog_revision_no(),
    yaml_schema_version integer NOT NULL CHECK (yaml_schema_version > 0),
    source_document jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(source_document, 4194304)
        AND extensions.jsonb_matches_schema(
            bursar.catalog_document_shape_schema()::json,
            source_document
        )
    ),
    digest bytea NOT NULL CHECK (octet_length(digest) = 32),
    status bursar.catalog_revision_status NOT NULL DEFAULT 'draft',
    label text CHECK (
        label IS null OR bursar.is_nonempty_bounded_text(label, 255)
    ),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    published_at timestamptz CHECK (published_at IS null OR bursar.is_finite_timestamptz(published_at)),
    activated_at timestamptz CHECK (activated_at IS null OR bursar.is_finite_timestamptz(activated_at)),
    retired_at timestamptz CHECK (retired_at IS null OR bursar.is_finite_timestamptz(retired_at)),
    UNIQUE (tenant_id, revision_no),
    UNIQUE (tenant_id, yaml_schema_version, digest),
    CHECK (published_at IS null OR published_at >= created_at),
    CHECK (activated_at IS null OR published_at IS NOT null),
    CHECK (retired_at IS null OR activated_at IS NOT null)
);

-- Record activation intervals separately from revision state so historical
-- pricing and entitlement resolution can answer which catalog was active.
CREATE TABLE bursar.catalog_activation_history (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id),
    activated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(activated_at)),
    deactivated_at timestamptz CHECK (deactivated_at IS null OR bursar.is_finite_timestamptz(deactivated_at)),
    label text CHECK (
        label IS null OR bursar.is_nonempty_bounded_text(label, 255)
    ),
    CHECK (deactivated_at IS null OR deactivated_at >= activated_at)
);

-- 5. Catalog pricing and policy projections

-- Project bucket priority and expiry semantics from each immutable revision;
-- the all-or-none expiry fields prevent partially materialized policies.
CREATE TABLE bursar.catalog_buckets (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    bucket_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(bucket_key, 255)),
    label text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(label, 255)),
    priority integer NOT NULL CHECK (priority >= 0),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'BucketDefinition'
        )
    ),
    expiry_policy jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(expiry_policy, 32768)
        AND bursar.matches_catalog_fragment(
            expiry_policy,
            bursar.catalog_document_shape_schema()
            -> '$defs' -> 'BucketDefinition' -> 'properties' -> 'expiry'
        )
    ),
    expiry_type text NOT NULL
    CHECK (expiry_type IN (
        'never', 'after_grant', 'end_of_window', 'fixed_at',
        'subscription_end'
    )),
    expires boolean GENERATED ALWAYS AS (expiry_type <> 'never') STORED,
    expires_after_unit text
    CHECK (expires_after_unit IN ('day', 'week', 'month', 'year')),
    expires_after_count integer
    CHECK (expires_after_count IS null OR expires_after_count > 0),
    expires_after_anchor text
    CHECK (expires_after_anchor IN ('calendar', 'plan_assignment', 'rolling')),
    expires_after_timezone text,
    fixed_expires_at timestamptz CHECK (fixed_expires_at IS null OR bursar.is_finite_timestamptz(fixed_expires_at)),
    allow_overdraft boolean NOT NULL DEFAULT false,
    is_default boolean NOT NULL DEFAULT false,
    UNIQUE (catalog_revision_id, bucket_key),
    UNIQUE (id, catalog_revision_id),
    UNIQUE (catalog_revision_id, priority),
    CHECK (
        (
            expiry_type IN ('never', 'subscription_end')
            AND expires_after_unit IS null
            AND expires_after_count IS null
            AND expires_after_anchor IS null
            AND expires_after_timezone IS null
            AND fixed_expires_at IS null
        )
        OR
        (
            expiry_type IN ('after_grant', 'end_of_window')
            AND expires_after_unit IS NOT null
            AND expires_after_count IS NOT null
            AND expires_after_anchor IS NOT null
            AND expires_after_timezone IS NOT null
            AND fixed_expires_at IS null
        )
        OR
        (
            expiry_type = 'fixed_at'
            AND expires_after_unit IS null
            AND expires_after_count IS null
            AND expires_after_anchor IS null
            AND expires_after_timezone IS null
            AND fixed_expires_at IS NOT null
        )
    )
);

-- Materialize operation measure/dimension contracts beside their source JSON
-- so usage validation is revision-pinned and queryable without reparsing YAML.
CREATE TABLE bursar.catalog_operations (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    operation_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(operation_key, 255)),
    measures jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(measures, 65536)
        AND bursar.matches_catalog_fragment(
            measures,
            bursar.catalog_document_shape_schema()
            -> '$defs' -> 'OperationDefinition' -> 'properties' -> 'measures'
        )
    ),
    dimensions jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (
        bursar.is_bounded_json_object(dimensions, 65536)
        AND bursar.matches_catalog_fragment(
            dimensions,
            bursar.catalog_document_shape_schema()
            -> '$defs' -> 'OperationDefinition' -> 'properties' -> 'dimensions'
        )
    ),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'OperationDefinition'
        )
    ),
    UNIQUE (catalog_revision_id, operation_key)
);

-- Rate cards remain revision-scoped; the deferred self-reference permits a
-- child to be loaded before its parent during one atomic catalog publication.
CREATE TABLE bursar.catalog_rate_cards (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    rate_card_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(rate_card_key, 255)),
    extends_key text CHECK (
        extends_key IS null
        OR bursar.is_nonempty_bounded_text(extends_key, 255)
    ),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(definition, 'RateCard')
    ),
    UNIQUE (catalog_revision_id, rate_card_key),
    FOREIGN KEY (catalog_revision_id, extends_key)
    REFERENCES bursar.catalog_rate_cards (catalog_revision_id, rate_card_key)
    DEFERRABLE INITIALLY DEFERRED
);

-- Keep prepaid and bounded credit-line behavior revision-pinned, with exact
-- positive limits only where overdraft policy actually permits debt.
CREATE TABLE bursar.catalog_credit_policies (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    policy_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(policy_key, 255)),
    policy_type text NOT NULL CHECK (policy_type IN ('prepaid', 'credit_line')),
    credit_limit bursar.credit_numeric,
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'PrepaidCreditPolicy',
            'CreditLinePolicy'
        )
    ),
    UNIQUE (catalog_revision_id, policy_key),
    CHECK (
        (policy_type = 'prepaid' AND credit_limit IS null)
        OR
        (
            policy_type = 'credit_line'
            AND credit_limit > 0
        )
    )
);

-- Store catalog-wide concurrency ceilings independently of runtime counters so
-- admission decisions can resolve the exact published policy.
CREATE TABLE bursar.catalog_admission_policies (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    policy_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(policy_key, 255)),
    max_in_flight integer CHECK (max_in_flight IS null OR max_in_flight > 0),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'AdmissionPolicy'
        )
    ),
    UNIQUE (catalog_revision_id, policy_key)
);

-- Normalize per-operation concurrency overrides and bind both sides to the
-- same catalog revision, preventing cross-revision policy composition.
CREATE TABLE bursar.catalog_admission_operation_policies (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    catalog_revision_id uuid NOT NULL,
    admission_policy_key text NOT NULL,
    operation_key text NOT NULL,
    max_in_flight integer NOT NULL CHECK (max_in_flight > 0),
    PRIMARY KEY (
        catalog_revision_id,
        admission_policy_key,
        operation_key
    ),
    FOREIGN KEY (catalog_revision_id, admission_policy_key)
    REFERENCES bursar.catalog_admission_policies (
        catalog_revision_id,
        policy_key
    ) ON DELETE CASCADE,
    FOREIGN KEY (catalog_revision_id, operation_key)
    REFERENCES bursar.catalog_operations (
        catalog_revision_id,
        operation_key
    ) ON DELETE CASCADE
);

-- Project feature type/default metadata for revision-aware entitlement reads;
-- later validation checks plan values against the declared feature contract.
CREATE TABLE bursar.catalog_entitlement_features (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    feature_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(feature_key, 255)),
    value_type text NOT NULL CHECK (value_type IN ('boolean', 'integer', 'string', 'enum')),
    default_value jsonb NOT NULL
    CHECK (octet_length(default_value::text) <= 65536),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'BooleanFeature',
            'IntegerFeature',
            'StringFeature',
            'EnumFeature'
        )
    ),
    UNIQUE (catalog_revision_id, feature_key)
);

-- 6. Plan and commerce projections

-- Gather a plan's revision-local pricing, credit, admission, and allowance
-- references; allowance fields are all present or all absent as one policy.
CREATE TABLE bursar.catalog_plans (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    plan_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(plan_key, 255)),
    display_name text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(display_name, 255)),
    description text CHECK (
        description IS null
        OR bursar.is_nonempty_bounded_text(description, 8192)
    ),
    rate_card text CHECK (
        rate_card IS null
        OR bursar.is_nonempty_bounded_text(rate_card, 255)
    ),
    allowed_operations text [] NOT NULL DEFAULT ARRAY[]::text []
    CHECK (bursar.is_canonical_identifier_array(
        allowed_operations,
        1000,
        255
    )),
    credit_policy_key text CHECK (
        credit_policy_key IS null
        OR bursar.is_nonempty_bounded_text(credit_policy_key, 255)
    ),
    admission_policy_key text CHECK (
        admission_policy_key IS null
        OR bursar.is_nonempty_bounded_text(admission_policy_key, 255)
    ),
    default_rollout text NOT NULL DEFAULT 'immediate'
    CHECK (default_rollout IN (
        'immediate', 'next_renewal', 'new_assignments_only'
    )),
    credit_allowance_amount bursar.credit_numeric
    CHECK (
        credit_allowance_amount IS null
        OR (
            credit_allowance_amount >= 0
        )
    ),
    credit_allowance_priority integer
    CHECK (
        credit_allowance_priority IS null
        OR credit_allowance_priority >= 0
    ),
    credit_allowance_bucket text,
    credit_allowance_reset_unit text
    CHECK (credit_allowance_reset_unit IN (
        'second', 'minute', 'hour', 'day', 'week', 'month', 'year'
    )),
    credit_allowance_reset_count integer
    CHECK (
        credit_allowance_reset_count IS null
        OR credit_allowance_reset_count > 0
    ),
    credit_allowance_reset_anchor text
    CHECK (
        credit_allowance_reset_anchor
        IN ('calendar', 'plan_assignment', 'rolling')
    ),
    credit_allowance_reset_timezone text,
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'PlanDefinition'
        )
    ),
    UNIQUE (catalog_revision_id, plan_key),
    UNIQUE (id, catalog_revision_id),
    UNIQUE (id, catalog_revision_id, plan_key),
    FOREIGN KEY (catalog_revision_id, rate_card)
    REFERENCES bursar.catalog_rate_cards (
        catalog_revision_id,
        rate_card_key
    ),
    FOREIGN KEY (catalog_revision_id, credit_policy_key)
    REFERENCES bursar.catalog_credit_policies (
        catalog_revision_id,
        policy_key
    ),
    FOREIGN KEY (catalog_revision_id, admission_policy_key)
    REFERENCES bursar.catalog_admission_policies (
        catalog_revision_id,
        policy_key
    ),
    FOREIGN KEY (catalog_revision_id, credit_allowance_bucket)
    REFERENCES bursar.catalog_buckets (catalog_revision_id, bucket_key),
    CHECK (
        (
            credit_allowance_amount IS null
            AND credit_allowance_priority IS null
            AND credit_allowance_bucket IS null
            AND credit_allowance_reset_unit IS null
            AND credit_allowance_reset_count IS null
            AND credit_allowance_reset_anchor IS null
            AND credit_allowance_reset_timezone IS null
        )
        OR
        (
            credit_allowance_amount IS NOT null
            AND credit_allowance_priority IS NOT null
            AND credit_allowance_bucket IS NOT null
            AND credit_allowance_reset_unit IS NOT null
            AND credit_allowance_reset_count IS NOT null
            AND credit_allowance_reset_anchor IS NOT null
            AND credit_allowance_reset_timezone IS NOT null
        )
    )
);

-- Store typed plan feature values as a revision-local join so plan resolution
-- cannot accidentally combine definitions from different catalogs.
CREATE TABLE bursar.catalog_plan_features (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    catalog_revision_id uuid NOT NULL,
    plan_key text NOT NULL,
    feature_key text NOT NULL,
    feature_value jsonb NOT NULL
    CHECK (octet_length(feature_value::text) <= 65536),
    PRIMARY KEY (catalog_revision_id, plan_key, feature_key),
    FOREIGN KEY (catalog_revision_id, plan_key)
    REFERENCES bursar.catalog_plans (catalog_revision_id, plan_key)
    ON DELETE CASCADE,
    FOREIGN KEY (catalog_revision_id, feature_key)
    REFERENCES bursar.catalog_entitlement_features (
        catalog_revision_id,
        feature_key
    ) ON DELETE CASCADE
);

-- Materialize exact quota limits and window policies per plan/operation while
-- preserving whether exceedance blocks work or only emits telemetry.
CREATE TABLE bursar.catalog_plan_quotas (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL,
    plan_key text NOT NULL,
    quota_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(quota_key, 255)),
    operation_key text NOT NULL,
    measure_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(measure_key, 255)),
    quota_limit bursar.credit_numeric NOT NULL
    CHECK (quota_limit >= 0),
    window_policy jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(window_policy, 32768)
        AND bursar.matches_catalog_fragment(
            window_policy,
            bursar.catalog_document_shape_schema()
            -> '$defs' -> 'QuotaDefinition' -> 'properties' -> 'window'
        )
    ),
    enforcement text NOT NULL CHECK (enforcement IN ('block', 'allow')),
    emit_at_percent integer [] NOT NULL DEFAULT ARRAY[]::integer []
    CHECK (bursar.is_canonical_threshold_array(emit_at_percent)),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'QuotaDefinition'
        )
    ),
    UNIQUE (catalog_revision_id, plan_key, quota_key),
    FOREIGN KEY (catalog_revision_id, plan_key)
    REFERENCES bursar.catalog_plans (catalog_revision_id, plan_key)
    ON DELETE CASCADE,
    FOREIGN KEY (catalog_revision_id, operation_key)
    REFERENCES bursar.catalog_operations (
        catalog_revision_id,
        operation_key
    ) ON DELETE CASCADE
);

-- Define revision-pinned award programs with explicit eligibility and
-- idempotency scope so repeated business events cannot multiply grants.
CREATE TABLE bursar.catalog_grant_programs (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    program_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(program_key, 255)),
    trigger_type text NOT NULL
    CHECK (trigger_type IN (
        'account_created',
        'referral_completed',
        'promo_code_redeemed',
        'manual'
    )),
    availability jsonb CHECK (
        availability IS null
        OR (
            octet_length(availability::text) <= 65536
            AND bursar.matches_catalog_fragment(
                availability,
                bursar.catalog_document_shape_schema()
                -> '$defs' -> 'Availability'
            )
        )
    ),
    eligibility jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (
        bursar.is_bounded_json_object(eligibility, 32768)
        AND bursar.matches_catalog_fragment(
            eligibility,
            bursar.catalog_document_shape_schema()
            -> '$defs' -> 'GrantEligibility'
        )
    ),
    max_awards_per_subject integer NOT NULL DEFAULT 1
    CHECK (max_awards_per_subject > 0),
    idempotency_scope text NOT NULL DEFAULT 'subject'
    CHECK (idempotency_scope IN ('subject', 'event')),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'GrantProgram'
        )
    ),
    UNIQUE (catalog_revision_id, program_key),
    UNIQUE (id, catalog_revision_id)
);

-- Expand ordered awards into exact, bucket-bound rows; subscription-end expiry
-- is excluded because these grants need a standalone lifetime anchor.
CREATE TABLE bursar.catalog_grant_awards (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL,
    grant_program_id uuid NOT NULL,
    award_index integer NOT NULL CHECK (award_index >= 0),
    recipient text NOT NULL CHECK (recipient IN ('subject', 'referrer')),
    amount bursar.credit_numeric NOT NULL
    CHECK (amount > 0),
    bucket_key text NOT NULL,
    expiry_policy jsonb CHECK (
        expiry_policy IS null
        OR (
            octet_length(expiry_policy::text) <= 32768
            AND bursar.matches_catalog_fragment(
                expiry_policy,
                bursar.catalog_document_shape_schema()
                -> '$defs' -> 'GrantAward' -> 'properties' -> 'expiry'
            )
        )
    ),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(definition, 'GrantAward')
    ),
    UNIQUE (grant_program_id, award_index),
    UNIQUE (id, catalog_revision_id),
    FOREIGN KEY (grant_program_id, catalog_revision_id)
    REFERENCES bursar.catalog_grant_programs (id, catalog_revision_id)
    ON DELETE CASCADE,
    FOREIGN KEY (catalog_revision_id, bucket_key)
    REFERENCES bursar.catalog_buckets (catalog_revision_id, bucket_key),
    CHECK (
        expiry_policy IS null
        OR expiry_policy ->> 'type' <> 'subscription_end'
    )
);

-- Project subscription offers with exact minor-unit prices and an all-or-none
-- cycle grant, pinned to the plan and bucket in the same catalog revision.
CREATE TABLE bursar.catalog_offers (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    offer_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(offer_key, 255)),
    display_name text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(display_name, 255)),
    description text CHECK (
        description IS null
        OR bursar.is_nonempty_bounded_text(description, 8192)
    ),
    sort_order integer NOT NULL DEFAULT 0,
    availability jsonb CHECK (
        availability IS null
        OR (
            octet_length(availability::text) <= 65536
            AND bursar.matches_catalog_fragment(
                availability,
                bursar.catalog_document_shape_schema()
                -> '$defs' -> 'Availability'
            )
        )
    ),
    amount_minor bigint NOT NULL
    CHECK (bursar.is_nonnegative_safe_integer(amount_minor)),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    tax_behavior text NOT NULL
    CHECK (tax_behavior IN ('inclusive', 'exclusive', 'unspecified')),
    plan_key text NOT NULL,
    billing_unit text NOT NULL
    CHECK (billing_unit IN ('day', 'week', 'month', 'year')),
    billing_count integer NOT NULL CHECK (billing_count > 0),
    trial_policy jsonb CHECK (
        trial_policy IS null
        OR (
            octet_length(trial_policy::text) <= 32768
            AND bursar.matches_catalog_fragment(
                trial_policy,
                bursar.catalog_document_shape_schema()
                -> '$defs' -> 'BillingInterval'
            )
        )
    ),
    cycle_grant_amount bursar.credit_numeric
    CHECK (
        cycle_grant_amount IS null
        OR (
            cycle_grant_amount > 0
        )
    ),
    cycle_grant_bucket_key text,
    cycle_grant_renewal text
    CHECK (cycle_grant_renewal IN ('replace_previous', 'accumulate')),
    cycle_grant_expiry_policy jsonb CHECK (
        cycle_grant_expiry_policy IS null
        OR (
            octet_length(cycle_grant_expiry_policy::text) <= 32768
            AND bursar.matches_catalog_fragment(
                cycle_grant_expiry_policy,
                bursar.catalog_document_shape_schema()
                -> '$defs' -> 'CycleGrant' -> 'properties' -> 'expiry'
            )
        )
    ),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(
            definition,
            'SubscriptionOffer'
        )
    ),
    UNIQUE (catalog_revision_id, offer_key),
    UNIQUE (id, catalog_revision_id),
    FOREIGN KEY (catalog_revision_id, plan_key)
    REFERENCES bursar.catalog_plans (catalog_revision_id, plan_key),
    FOREIGN KEY (catalog_revision_id, cycle_grant_bucket_key)
    REFERENCES bursar.catalog_buckets (catalog_revision_id, bucket_key),
    CHECK (
        (
            cycle_grant_amount IS null
            AND cycle_grant_bucket_key IS null
            AND cycle_grant_renewal IS null
            AND cycle_grant_expiry_policy IS null
        )
        OR
        (
            cycle_grant_amount IS NOT null
            AND cycle_grant_bucket_key IS NOT null
            AND cycle_grant_renewal IS NOT null
            AND cycle_grant_expiry_policy IS NOT null
        )
    )
);

-- Project one-time purchases as exact credits-per-unit with bounded quantity
-- and bucket/expiry rules that remain stable for historical fulfillment.
CREATE TABLE bursar.catalog_topups (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    topup_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(topup_key, 255)),
    display_name text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(display_name, 255)),
    description text CHECK (
        description IS null
        OR bursar.is_nonempty_bounded_text(description, 8192)
    ),
    sort_order integer NOT NULL DEFAULT 0,
    availability jsonb CHECK (
        availability IS null
        OR (
            octet_length(availability::text) <= 65536
            AND bursar.matches_catalog_fragment(
                availability,
                bursar.catalog_document_shape_schema()
                -> '$defs' -> 'Availability'
            )
        )
    ),
    amount_minor bigint NOT NULL
    CHECK (bursar.is_nonnegative_safe_integer(amount_minor)),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    tax_behavior text NOT NULL
    CHECK (tax_behavior IN ('inclusive', 'exclusive', 'unspecified')),
    credits_per_unit bursar.credit_numeric NOT NULL
    CHECK (credits_per_unit > 0),
    bucket_key text NOT NULL,
    min_quantity integer NOT NULL DEFAULT 1 CHECK (min_quantity > 0),
    max_quantity integer NOT NULL DEFAULT 1 CHECK (max_quantity >= min_quantity),
    default_quantity integer NOT NULL DEFAULT 1
    CHECK (
        default_quantity >= min_quantity
        AND default_quantity <= max_quantity
    ),
    expiry_policy jsonb CHECK (
        expiry_policy IS null
        OR (
            octet_length(expiry_policy::text) <= 32768
            AND bursar.matches_catalog_fragment(
                expiry_policy,
                bursar.catalog_document_shape_schema()
                -> '$defs' -> 'TopupOffer' -> 'properties' -> 'expiry'
            )
        )
    ),
    lot_behavior text NOT NULL DEFAULT 'separate_lots'
    CHECK (lot_behavior IN ('separate_lots', 'merge_and_refresh')),
    definition jsonb NOT NULL
    CHECK (
        bursar.is_bounded_json_object(definition, 262144)
        AND bursar.matches_catalog_definitions(definition, 'TopupOffer')
    ),
    UNIQUE (catalog_revision_id, topup_key),
    UNIQUE (id, catalog_revision_id),
    FOREIGN KEY (catalog_revision_id, bucket_key)
    REFERENCES bursar.catalog_buckets (catalog_revision_id, bucket_key),
    CHECK (
        expiry_policy IS null
        OR expiry_policy ->> 'type' <> 'subscription_end'
    )
);

-- Resolve external provider objects to catalog offers/topups using both
-- environment and revision, avoiding live/test aliasing or mutable lookups.
CREATE TABLE bursar.catalog_provider_refs (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    catalog_revision_id uuid NOT NULL
    REFERENCES bursar.catalog_revisions (id) ON DELETE CASCADE,
    provider text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(provider, 100)),
    provider_environment text NOT NULL
    DEFAULT bursar.current_provider_environment()
    CHECK (provider_environment IN ('live', 'test', 'sandbox')),
    lookup_type text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(lookup_type, 100)),
    lookup_value text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(lookup_value, 255)),
    object_type text NOT NULL CHECK (object_type IN ('offer', 'topup')),
    object_key text NOT NULL
    CHECK (bursar.is_nonempty_bounded_text(object_key, 255)),
    UNIQUE (
        catalog_revision_id,
        provider,
        provider_environment,
        lookup_type,
        lookup_value
    )
);

-- 7. Credit accounts

-- Hold the current exact balance and optimistic-lock version per tenant subject
-- and account kind; the immutable ledger added next remains the audit source.
CREATE TABLE bursar.credit_accounts (
    tenant_id uuid NOT NULL DEFAULT bursar.require_tenant_id()
    REFERENCES bursar.tenants (id) ON DELETE RESTRICT,
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    subject_id uuid NOT NULL REFERENCES bursar.subjects (id),
    account_kind text NOT NULL CHECK (account_kind IN ('personal', 'team')),
    balance bursar.credit_numeric NOT NULL DEFAULT 0,
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    UNIQUE (tenant_id, subject_id, account_kind)
);
