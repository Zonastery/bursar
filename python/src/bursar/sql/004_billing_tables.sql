CREATE TABLE bursar.billing_customers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id), provider text NOT NULL, provider_customer_id text NOT NULL,
    email text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE (provider, provider_customer_id), UNIQUE (subject_id, provider)
);
CREATE TABLE bursar.billing_subscriptions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id), provider text NOT NULL, provider_subscription_id text NOT NULL,
    provider_customer_id text, offer_id uuid NOT NULL, catalog_revision_id uuid NOT NULL, status bursar.billing_subscription_status NOT NULL,
    current_period_start timestamptz, current_period_end timestamptz, cancel_at_period_end boolean NOT NULL DEFAULT false, metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_subscription_id),
    UNIQUE (id, subject_id),
    FOREIGN KEY (offer_id, catalog_revision_id)
        REFERENCES bursar.catalog_offers(id, catalog_revision_id),
    CHECK (current_period_end IS NULL OR current_period_start IS NULL OR current_period_end > current_period_start)
);
CREATE TABLE bursar.billing_entitlement_sources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id), subscription_id uuid NOT NULL, selected boolean NOT NULL DEFAULT false,
    selected_at timestamptz, UNIQUE (subject_id, subscription_id),
    FOREIGN KEY (subscription_id, subject_id)
        REFERENCES bursar.billing_subscriptions(id, subject_id),
    CHECK (selected_at IS NOT NULL OR NOT selected)
);
CREATE TABLE bursar.billing_payments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id), provider text NOT NULL, provider_payment_id text NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor >= 0), tax_minor bigint NOT NULL DEFAULT 0 CHECK (tax_minor >= 0), currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    purpose text NOT NULL CHECK (purpose IN ('subscription','credit_topup')), status bursar.billing_payment_status NOT NULL DEFAULT 'pending', created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_payment_id),
    UNIQUE (id, subject_id)
);
CREATE TABLE bursar.billing_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), provider text NOT NULL, provider_event_id text NOT NULL, event_type text NOT NULL, envelope jsonb NOT NULL DEFAULT '{}'::jsonb,
    status bursar.billing_event_status NOT NULL DEFAULT 'processing', retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0), claim_token uuid, claim_expires_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_event_id),
    CHECK (
        (status = 'processing' AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
        OR (status <> 'processing' AND claim_token IS NULL AND claim_expires_at IS NULL)
    )
);
CREATE TABLE bursar.billing_credit_grants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), payment_id uuid, subject_id uuid NOT NULL REFERENCES bursar.subjects(id), topup_id uuid,
    subscription_id uuid, catalog_revision_id uuid NOT NULL, configured_credits numeric(20,6) NOT NULL CHECK (configured_credits > 0), quantity integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries(id), billing_event_id uuid REFERENCES bursar.billing_events(id), created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (payment_id, subject_id) REFERENCES bursar.billing_payments(id, subject_id),
    FOREIGN KEY (subscription_id, subject_id) REFERENCES bursar.billing_subscriptions(id, subject_id),
    FOREIGN KEY (topup_id, catalog_revision_id) REFERENCES bursar.catalog_topups(id, catalog_revision_id),
    CHECK (
        (payment_id IS NOT NULL AND topup_id IS NOT NULL AND subscription_id IS NULL)
        OR (payment_id IS NULL AND topup_id IS NULL AND subscription_id IS NOT NULL)
    )
);
CREATE TABLE bursar.billing_refunds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), payment_id uuid NOT NULL REFERENCES bursar.billing_payments(id), provider text NOT NULL, provider_refund_id text NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor > 0), currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'), created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (provider, provider_refund_id)
);
CREATE TABLE bursar.billing_refund_grants (
    refund_id uuid NOT NULL REFERENCES bursar.billing_refunds(id) ON DELETE CASCADE, grant_id uuid NOT NULL REFERENCES bursar.billing_credit_grants(id), amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    credit_amount numeric(20,6) NOT NULL CHECK (credit_amount > 0),
    ledger_entry_id uuid REFERENCES bursar.credit_ledger_entries(id), PRIMARY KEY (refund_id, grant_id)
);
CREATE TABLE bursar.billing_subscription_changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subscription_id uuid NOT NULL REFERENCES bursar.billing_subscriptions(id), state text NOT NULL CHECK (state IN ('awaiting_payment','scheduled','applied','canceled','failed')),
    from_offer_id uuid NOT NULL REFERENCES bursar.catalog_offers(id), to_offer_id uuid NOT NULL REFERENCES bursar.catalog_offers(id), effective_at timestamptz, provider_operation_id text,
    idempotency_key text NOT NULL CHECK (idempotency_key <> ''),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (subscription_id,idempotency_key),
    CHECK (from_offer_id <> to_offer_id)
);
CREATE TABLE bursar.billing_auto_recharge_profiles (
    subject_id uuid PRIMARY KEY REFERENCES bursar.subjects(id), enabled boolean NOT NULL DEFAULT false, armed boolean NOT NULL DEFAULT true, state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','paused','disabled')),
    provider text, topup_id uuid REFERENCES bursar.catalog_topups(id), quantity integer NOT NULL DEFAULT 1 CHECK (quantity > 0), threshold numeric(20,6) NOT NULL DEFAULT 0 CHECK (threshold >= 0),
    max_charges_per_window integer CHECK (max_charges_per_window IS NULL OR max_charges_per_window > 0),
    window_unit text NOT NULL DEFAULT 'day' CHECK (window_unit IN ('day','week','month','year')),
    window_count integer NOT NULL DEFAULT 1 CHECK (window_count > 0),
    window_anchor text NOT NULL DEFAULT 'calendar' CHECK (window_anchor IN ('calendar','plan_assignment','rolling')),
    window_timezone text NOT NULL DEFAULT 'UTC',
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (NOT enabled OR (provider IS NOT NULL AND topup_id IS NOT NULL))
);
CREATE TABLE bursar.billing_auto_recharge_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id), provider text NOT NULL, idempotency_key text NOT NULL, state bursar.recharge_attempt_status NOT NULL DEFAULT 'claimed',
 topup_id uuid NOT NULL REFERENCES bursar.catalog_topups(id), quantity integer NOT NULL CHECK (quantity > 0), window_start timestamptz NOT NULL, window_end timestamptz NOT NULL, quoted_amount_minor bigint CHECK (quoted_amount_minor IS NULL OR quoted_amount_minor >= 0), currency char(3), provider_attempt_id text, failure_code text, failure_message text, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE (subject_id, idempotency_key),
    CHECK (window_end > window_start),
    CHECK ((quoted_amount_minor IS NULL) = (currency IS NULL)),
    CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$')
);
CREATE TABLE bursar.catalog_auto_recharge_policies (
    catalog_revision_id uuid PRIMARY KEY REFERENCES bursar.catalog_revisions(id) ON DELETE CASCADE,
    topup_key text NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    balance_below numeric(20,6) NOT NULL CHECK (balance_below >= 0),
    max_purchases integer NOT NULL CHECK (max_purchases > 0),
    period_unit text NOT NULL CHECK (period_unit IN ('day','week','month','year')),
    period_count integer NOT NULL CHECK (period_count > 0),
    period_anchor text NOT NULL CHECK (period_anchor IN ('calendar','plan_assignment','rolling')),
    period_timezone text NOT NULL,
    FOREIGN KEY (catalog_revision_id, topup_key)
        REFERENCES bursar.catalog_topups(catalog_revision_id, topup_key)
);
CREATE TABLE bursar.billing_checkout_intents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id), provider text NOT NULL,
    checkout_kind text NOT NULL CHECK (checkout_kind IN ('subscription','credit_topup')), product_key text NOT NULL,
    request_digest bytea NOT NULL CHECK (octet_length(request_digest)=32), status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','completed','failed','expired')),
    provider_session_id text, checkout_url text, expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (subject_id, provider, checkout_kind, product_key, request_digest),
    CHECK (expires_at > created_at)
);
CREATE TABLE bursar.billing_invoices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES bursar.subjects(id), provider text NOT NULL, provider_invoice_id text NOT NULL,
    subscription_id uuid REFERENCES bursar.billing_subscriptions(id), status text NOT NULL CHECK (status IN ('draft','open','paid','void','uncollectible')), amount_due_minor bigint NOT NULL CHECK (amount_due_minor >= 0), amount_paid_minor bigint NOT NULL DEFAULT 0 CHECK (amount_paid_minor >= 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'), period_start timestamptz, period_end timestamptz, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (provider, provider_invoice_id), CHECK (period_end IS NULL OR period_start IS NULL OR period_end > period_start)
);
CREATE TABLE bursar.billing_disputes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid REFERENCES bursar.subjects(id), provider text NOT NULL, provider_dispute_id text NOT NULL,
    payment_id uuid REFERENCES bursar.billing_payments(id), status text NOT NULL CHECK (status IN ('needs_response','under_review','won','lost','closed')), reason text, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (provider, provider_dispute_id)
);
CREATE TABLE bursar.billing_preferences (
    subject_id uuid PRIMARY KEY REFERENCES bursar.subjects(id), auto_recharge boolean NOT NULL DEFAULT false, overage_protection boolean NOT NULL DEFAULT true,
    email_notifications boolean NOT NULL DEFAULT true, usage_alerts boolean NOT NULL DEFAULT true, invoice_reminders boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX billing_auto_recharge_one_active ON bursar.billing_auto_recharge_attempts(subject_id)
  WHERE state IN ('claimed','submitted','processing','unknown','action_required');
