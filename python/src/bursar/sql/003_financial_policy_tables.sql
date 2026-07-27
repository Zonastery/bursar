CREATE TABLE bursar.credit_ledger_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id),
    kind bursar.ledger_entry_kind NOT NULL, amount numeric(20,6) NOT NULL CHECK (amount <> 0), balance_after numeric(20,6) NOT NULL,
    reference_entry_id uuid REFERENCES bursar.credit_ledger_entries(id), idempotency_key text NOT NULL, request_digest bytea NOT NULL CHECK (octet_length(request_digest)=32),
    operation text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, idempotency_key),
    CHECK (
        (kind IN ('grant','purchase','refund','release') AND amount > 0)
        OR (kind IN ('usage','expiry','revocation','refund_clawback','reservation') AND amount < 0)
        OR kind = 'adjustment'
    )
);
CREATE TABLE bursar.credit_lots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id), source_entry_id uuid NOT NULL UNIQUE REFERENCES bursar.credit_ledger_entries(id),
    catalog_revision_id uuid NOT NULL, bucket_key text NOT NULL, priority integer NOT NULL CHECK (priority >= 0),
    granted numeric(20,6) NOT NULL CHECK (granted > 0), consumed numeric(20,6) NOT NULL DEFAULT 0 CHECK (consumed >= 0 AND consumed <= granted),
    expires_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (catalog_revision_id, bucket_key)
        REFERENCES bursar.catalog_buckets(catalog_revision_id, bucket_key)
);
CREATE TABLE bursar.credit_lot_allocations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), debit_entry_id uuid NOT NULL REFERENCES bursar.credit_ledger_entries(id), lot_id uuid NOT NULL REFERENCES bursar.credit_lots(id),
    amount numeric(20,6) NOT NULL CHECK (amount > 0), allocation_kind text NOT NULL CHECK (allocation_kind IN ('spend','expiry','revocation','clawback')), created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (debit_entry_id, lot_id)
);
CREATE TABLE bursar.credit_usage_charges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id), operation text NOT NULL, feature text,
    model text, region text, requested numeric(20,6) NOT NULL CHECK (requested >= 0), charged numeric(20,6) NOT NULL CHECK (charged >= 0),
    allowance_requested numeric(20,6) NOT NULL DEFAULT 0 CHECK (allowance_requested >= 0),
    allowance_covered numeric(20,6) NOT NULL DEFAULT 0 CHECK (allowance_covered >= 0),
 ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries(id), metadata jsonb NOT NULL DEFAULT '{}'::jsonb, idempotency_key text NOT NULL, request_digest bytea NOT NULL CHECK (octet_length(request_digest)=32), created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, idempotency_key),
    CHECK (charged + allowance_covered = requested),
    CHECK (allowance_covered <= allowance_requested)
);
CREATE TABLE bursar.account_plan_assignments (
    account_id uuid PRIMARY KEY REFERENCES bursar.credit_accounts(id), plan_id uuid NOT NULL, catalog_revision_id uuid NOT NULL,
    starts_at timestamptz NOT NULL DEFAULT now(), ends_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (plan_id, catalog_revision_id)
        REFERENCES bursar.catalog_plans(id, catalog_revision_id),
    CHECK (ends_at IS NULL OR ends_at > starts_at)
);
CREATE TABLE bursar.allowance_windows (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id), plan_id uuid NOT NULL,
    catalog_revision_id uuid NOT NULL, feature text NOT NULL, window_start timestamptz NOT NULL, window_end timestamptz NOT NULL,
 period_unit text NOT NULL CHECK (period_unit IN ('day','week','month','year')), period_count integer NOT NULL CHECK (period_count > 0), period_anchor text NOT NULL CHECK (period_anchor IN ('calendar','plan_assignment','subscription_start','rolling')),
    period_timezone text NOT NULL, allowance numeric(20,6) NOT NULL CHECK (allowance >= 0), reserved numeric(20,6) NOT NULL DEFAULT 0 CHECK (reserved >= 0), consumed numeric(20,6) NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    CHECK (window_end > window_start),
    CHECK (reserved + consumed <= allowance),
    UNIQUE (account_id, plan_id, catalog_revision_id, feature, window_start, window_end),
    FOREIGN KEY (plan_id, catalog_revision_id)
        REFERENCES bursar.catalog_plans(id, catalog_revision_id)
);
CREATE TABLE bursar.feature_call_windows (
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id), feature text NOT NULL, window_start timestamptz NOT NULL, window_end timestamptz NOT NULL,
    admitted integer NOT NULL DEFAULT 0 CHECK (admitted >= 0), reserved integer NOT NULL DEFAULT 0 CHECK (reserved >= 0), consumed integer NOT NULL DEFAULT 0 CHECK (consumed >= 0), limit_value integer CHECK (limit_value IS NULL OR limit_value >= 0),
    PRIMARY KEY (account_id, feature, window_start),
    CHECK (window_end > window_start),
    CHECK (reserved + consumed = admitted),
    CHECK (limit_value IS NULL OR admitted <= limit_value)
);

CREATE TABLE bursar.feature_limit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id),
    feature text NOT NULL,
    window_start timestamptz NOT NULL,
    action text NOT NULL CHECK (action IN ('warn','notify')),
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id,feature,window_start,idempotency_key)
);
CREATE TABLE bursar.credit_leases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id), operation text NOT NULL, feature text,
    policy_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions(id),
    reserved_amount numeric(20,6) NOT NULL CHECK (reserved_amount > 0), minimum_balance numeric(20,6) NOT NULL DEFAULT 0, max_concurrent integer CHECK (max_concurrent IS NULL OR max_concurrent > 0), reserved_calls integer NOT NULL DEFAULT 0 CHECK (reserved_calls >= 0),
    feature_window_start timestamptz, feature_window_end timestamptz,
    expires_at timestamptz NOT NULL, status bursar.lease_status NOT NULL DEFAULT 'active', idempotency_key text NOT NULL, request_digest bytea NOT NULL CHECK (octet_length(request_digest)=32),
    settled_amount numeric(20,6), ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries(id), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, idempotency_key),
    CHECK (
        (reserved_calls = 0 AND feature_window_start IS NULL AND feature_window_end IS NULL)
        OR (reserved_calls > 0 AND feature IS NOT NULL AND feature_window_start IS NOT NULL AND feature_window_end > feature_window_start)
    ),
    CHECK (settled_amount IS NULL OR settled_amount >= 0)
);
CREATE TABLE bursar.credit_teams (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL UNIQUE REFERENCES bursar.subjects(id), name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200), created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE bursar.credit_team_members (team_id uuid NOT NULL REFERENCES bursar.credit_teams(id) ON DELETE CASCADE, subject_id uuid NOT NULL REFERENCES bursar.subjects(id), role text NOT NULL CHECK (role IN ('owner','admin','member')), created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (team_id, subject_id));
CREATE TABLE bursar.signup_credit_grants (
    subject_id uuid PRIMARY KEY REFERENCES bursar.subjects(id) ON DELETE CASCADE,
    catalog_revision_id uuid NOT NULL REFERENCES bursar.catalog_revisions(id),
    ledger_entry_id uuid NOT NULL UNIQUE REFERENCES bursar.credit_ledger_entries(id),
    granted_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bursar.credit_plan_migrations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), from_plan_id uuid REFERENCES bursar.catalog_plans(id), to_plan_id uuid NOT NULL REFERENCES bursar.catalog_plans(id),
    cursor_account_id uuid, migrated_count integer NOT NULL DEFAULT 0 CHECK (migrated_count >= 0), status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (from_plan_id IS NULL OR from_plan_id <> to_plan_id)
);
