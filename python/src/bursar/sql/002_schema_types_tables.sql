CREATE SCHEMA IF NOT EXISTS bursar;
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

CREATE TYPE bursar.catalog_revision_status AS ENUM ('draft', 'published', 'active', 'retired');
CREATE TYPE bursar.ledger_entry_kind AS ENUM ('grant', 'purchase', 'usage', 'expiry', 'revocation', 'refund', 'refund_clawback', 'adjustment', 'reservation', 'release');
CREATE TYPE bursar.lease_status AS ENUM ('active', 'settled', 'released', 'expired');
CREATE TYPE bursar.billing_payment_status AS ENUM ('pending', 'succeeded', 'failed', 'refunded', 'partially_refunded');
CREATE TYPE bursar.billing_subscription_status AS ENUM ('incomplete', 'incomplete_expired', 'trialing', 'active', 'past_due', 'paused', 'canceled', 'unpaid', 'expired');
CREATE TYPE bursar.billing_event_status AS ENUM ('processing', 'completed', 'failed');
CREATE TYPE bursar.recharge_attempt_status AS ENUM ('claimed', 'submitted', 'processing', 'succeeded', 'failed', 'unknown', 'action_required');

CREATE TABLE bursar.subjects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bursar.external_identities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id) ON DELETE CASCADE,
    provider text NOT NULL, external_subject text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, external_subject)
);
CREATE TABLE bursar.catalog_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), revision_no bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    yaml_schema_version integer NOT NULL CHECK (yaml_schema_version > 0), source_document jsonb NOT NULL,
    digest bytea NOT NULL CHECK (octet_length(digest) = 32), status bursar.catalog_revision_status NOT NULL DEFAULT 'draft',
    label text, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz, activated_at timestamptz,
    retired_at timestamptz
);
CREATE TABLE bursar.catalog_buckets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions(id),
    bucket_key text NOT NULL, label text NOT NULL, priority integer NOT NULL CHECK (priority >= 0),
    expires boolean NOT NULL DEFAULT false,
    expires_after_unit text CHECK (expires_after_unit IN ('day','week','month','year')),
    expires_after_count integer CHECK (expires_after_count IS NULL OR expires_after_count > 0),
    expires_after_anchor text CHECK (expires_after_anchor IN ('calendar','plan_assignment','rolling')),
    expires_after_timezone text,
    CHECK ((expires = false AND expires_after_unit IS NULL AND expires_after_count IS NULL AND expires_after_anchor IS NULL AND expires_after_timezone IS NULL)
        OR (expires = true AND expires_after_unit IS NOT NULL AND expires_after_count IS NOT NULL AND expires_after_anchor IS NOT NULL AND expires_after_timezone IS NOT NULL)),
    allow_overdraft boolean NOT NULL DEFAULT false, is_default boolean NOT NULL DEFAULT false,
    UNIQUE (catalog_revision_id, bucket_key)
);
CREATE TABLE bursar.catalog_plans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions(id),
 plan_key text NOT NULL, display_name text NOT NULL, rate_card text,
 features jsonb NOT NULL DEFAULT '{}'::jsonb,
    limits jsonb NOT NULL DEFAULT '{}'::jsonb, spending jsonb NOT NULL DEFAULT '{}'::jsonb,
    included_credits numeric(20,6) CHECK (included_credits IS NULL OR included_credits >= 0),
    included_credits_bucket text, included_credits_reset_unit text CHECK (included_credits_reset_unit IN ('day','week','month','year')),
    included_credits_reset_count integer CHECK (included_credits_reset_count IS NULL OR included_credits_reset_count > 0),
    included_credits_reset_anchor text CHECK (included_credits_reset_anchor IN ('calendar','plan_assignment','subscription_start','rolling')),
    included_credits_reset_timezone text,
    UNIQUE (catalog_revision_id, plan_key),
    UNIQUE (id, catalog_revision_id),
    FOREIGN KEY (catalog_revision_id, included_credits_bucket)
        REFERENCES bursar.catalog_buckets(catalog_revision_id, bucket_key),
    CHECK (
        (included_credits IS NULL
         AND included_credits_bucket IS NULL
         AND included_credits_reset_unit IS NULL
         AND included_credits_reset_count IS NULL
         AND included_credits_reset_anchor IS NULL
         AND included_credits_reset_timezone IS NULL)
        OR
        (included_credits IS NOT NULL
         AND included_credits_bucket IS NOT NULL
         AND included_credits_reset_unit IS NOT NULL
         AND included_credits_reset_count IS NOT NULL
         AND included_credits_reset_anchor IS NOT NULL
         AND included_credits_reset_timezone IS NOT NULL)
    )
);
CREATE TABLE bursar.catalog_signup_grants (
    catalog_revision_id uuid PRIMARY KEY REFERENCES bursar.catalog_revisions(id) ON DELETE CASCADE,
    amount numeric(20,6) NOT NULL CHECK (amount > 0),
    bucket_key text NOT NULL,
    FOREIGN KEY (catalog_revision_id, bucket_key)
        REFERENCES bursar.catalog_buckets(catalog_revision_id, bucket_key)
);
CREATE TABLE bursar.catalog_offers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions(id),
    offer_key text NOT NULL, plan_key text NOT NULL, billing_unit text NOT NULL CHECK (billing_unit IN ('day','week','month','year')),
    billing_count integer NOT NULL CHECK (billing_count > 0), billing_anchor text NOT NULL CHECK (billing_anchor IN ('calendar','plan_assignment','subscription_start','rolling')),
    billing_timezone text NOT NULL DEFAULT 'UTC', grant_mode text NOT NULL CHECK (grant_mode IN ('allowance','credits','none')),
    grant_credits numeric(20,6) NOT NULL DEFAULT 0 CHECK (grant_credits >= 0), grant_bucket_key text,
    renewal_replacement text NOT NULL CHECK (renewal_replacement IN ('replace','accumulate')),
    subscription_end_behavior text NOT NULL CHECK (subscription_end_behavior IN ('expire','keep')),
    credit_stacking text NOT NULL CHECK (credit_stacking IN ('stack','replace')),
    UNIQUE (catalog_revision_id, offer_key),
    UNIQUE (id, catalog_revision_id),
    FOREIGN KEY (catalog_revision_id, plan_key)
        REFERENCES bursar.catalog_plans(catalog_revision_id, plan_key),
    FOREIGN KEY (catalog_revision_id, grant_bucket_key)
        REFERENCES bursar.catalog_buckets(catalog_revision_id, bucket_key),
    CHECK (
        (grant_mode = 'credits' AND grant_credits > 0 AND grant_bucket_key IS NOT NULL)
        OR (grant_mode <> 'credits' AND grant_credits = 0 AND grant_bucket_key IS NULL)
    )
);
CREATE TABLE bursar.catalog_topups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions(id),
    topup_key text NOT NULL, credits_per_unit numeric(20,6) NOT NULL CHECK (credits_per_unit > 0), bucket_key text NOT NULL,
    min_quantity integer NOT NULL DEFAULT 1 CHECK (min_quantity > 0), max_quantity integer CHECK (max_quantity IS NULL OR max_quantity >= min_quantity),
    UNIQUE (catalog_revision_id, topup_key),
    UNIQUE (id, catalog_revision_id),
    FOREIGN KEY (catalog_revision_id, bucket_key)
        REFERENCES bursar.catalog_buckets(catalog_revision_id, bucket_key)
);
CREATE TABLE bursar.catalog_provider_refs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions(id),
    provider text NOT NULL, lookup_type text NOT NULL CHECK (length(trim(lookup_type)) > 0),
    lookup_value text NOT NULL, object_type text NOT NULL CHECK (object_type IN ('offer','topup','plan')), object_key text NOT NULL,
    UNIQUE (catalog_revision_id, provider, lookup_type, lookup_value)
);
CREATE TABLE bursar.credit_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id), account_kind text NOT NULL CHECK (account_kind IN ('personal','team')),
    balance numeric(20,6) NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (subject_id, account_kind)
);
