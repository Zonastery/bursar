
-- ============================================================================
-- Source: 010_tables.sql
-- ============================================================================

-- Name: billing_credit_topups; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_credit_topups (
    topup_key text NOT NULL,
    deposit_to text DEFAULT 'purchased'::text NOT NULL,
    credits_per_unit numeric(18,4) DEFAULT 1000 NOT NULL,
    min_amount_minor integer DEFAULT 500 NOT NULL,
    max_amount_minor integer DEFAULT 500000 NOT NULL,
    tax_behavior text DEFAULT 'exclude_tax'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    CONSTRAINT billing_credit_topups_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text]))),
    CONSTRAINT billing_credit_topups_tax_behavior_check CHECK ((tax_behavior = ANY (ARRAY['exclude_tax'::text, 'include_tax'::text]))),
    CONSTRAINT billing_credit_topups_amounts_check CHECK (credits_per_unit > 0 AND min_amount_minor >= 0 AND max_amount_minor >= min_amount_minor)
);


--
-- Name: billing_customers; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_customers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    provider_customer_id text NOT NULL,
    user_id uuid,
    email text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    subject_id uuid
);


--
-- Name: billing_disputes; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_disputes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    provider_dispute_id text NOT NULL,
    provider_payment_id text,
    user_id uuid,
    status text DEFAULT 'needs_response'::text NOT NULL,
    reason text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    subject_id uuid
);


--
-- Name: billing_events; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    provider_event_id text NOT NULL,
    event_type text NOT NULL,
    status text DEFAULT 'processing'::text NOT NULL,
    retry_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    claim_token uuid,
    claim_expires_at timestamp with time zone,
    envelope jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT billing_events_status_check CHECK ((status = ANY (ARRAY['processing'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: billing_invoices; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_invoices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    provider_invoice_id text NOT NULL,
    provider_subscription_id text,
    user_id uuid,
    status text DEFAULT 'open'::text NOT NULL,
    amount_paid_minor bigint,
    amount_due_minor bigint,
    currency text DEFAULT 'USD'::text NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    subject_id uuid,
    CONSTRAINT billing_invoices_amounts_nonnegative CHECK ((((amount_paid_minor IS NULL) OR (amount_paid_minor >= 0)) AND ((amount_due_minor IS NULL) OR (amount_due_minor >= 0)))),
    CONSTRAINT billing_invoices_currency_iso CHECK ((currency ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT billing_invoices_period_valid CHECK (((period_end IS NULL) OR (period_start IS NULL) OR (period_end > period_start)))
);


--
-- Name: billing_offers; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_offers (
    offer_key text NOT NULL,
    plan text NOT NULL,
    "interval" text DEFAULT 'month'::text NOT NULL,
    billing_interval text GENERATED ALWAYS AS ("interval") STORED,
    interval_count integer DEFAULT 1 NOT NULL,
    grant_mode text DEFAULT 'allowance'::text NOT NULL,
    grant_credits numeric(18,4),
    grant_bucket text,
    grant_replace_prior boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    valid_from timestamp with time zone,
    valid_to timestamp with time zone,
    CONSTRAINT billing_offers_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);


--
-- Name: billing_payments; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_payments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    provider_payment_id text NOT NULL,
    provider_invoice_id text,
    user_id uuid,
    amount_minor bigint NOT NULL,
    tax_minor bigint,
    currency text DEFAULT 'USD'::text NOT NULL,
    purpose text DEFAULT 'unknown'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    subject_id uuid,
    CONSTRAINT billing_payments_amount_nonnegative CHECK ((amount_minor >= 0)),
    CONSTRAINT billing_payments_tax_nonnegative CHECK (tax_minor IS NULL OR tax_minor >= 0),
    CONSTRAINT billing_payments_currency_iso CHECK ((currency ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT billing_payments_purpose_check CHECK ((purpose = ANY (ARRAY['subscription'::text, 'credit_topup'::text, 'unknown'::text])))
);


--
-- Name: billing_preferences; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_preferences (
    user_id uuid NOT NULL,
    auto_recharge boolean DEFAULT false NOT NULL,
    overage_protection boolean DEFAULT true NOT NULL,
    email_notifications boolean DEFAULT true NOT NULL,
    usage_alerts boolean DEFAULT true NOT NULL,
    invoice_reminders boolean DEFAULT false NOT NULL,
    usage_limit_alerts boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: billing_provider_refs; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_provider_refs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    price_id text,
    product_id text,
    variant_id text,
    lookup_key text,
    resource_type text NOT NULL,
    resource_key text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    active boolean DEFAULT true NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    environment text DEFAULT 'live'::text NOT NULL,
    CONSTRAINT billing_provider_refs_environment_check CHECK ((environment = ANY (ARRAY['live'::text, 'test'::text, 'sandbox'::text]))),
    CONSTRAINT billing_provider_refs_resource_type_check CHECK ((resource_type = ANY (ARRAY['offer'::text, 'topup'::text])))
);


--
-- Name: COLUMN billing_provider_refs.environment; Type: COMMENT; Schema: bursar; Owner: -
--

COMMENT ON COLUMN bursar.billing_provider_refs.environment IS 'Provider environment used in identifier resolution; defaults to live for legacy references.';


--
-- Name: billing_refunds; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_refunds (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    provider_refund_id text NOT NULL,
    provider_payment_id text,
    user_id uuid,
    amount_minor bigint NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    reason text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    subject_id uuid,
    CONSTRAINT billing_refunds_amount_nonnegative CHECK ((amount_minor >= 0)),
    CONSTRAINT billing_refunds_currency_iso CHECK ((currency ~ '^[A-Z]{3}$'::text))
);


--
-- Name: billing_subscriptions; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_subscriptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    provider text NOT NULL,
    provider_subscription_id text NOT NULL,
    provider_customer_id text,
    offer_key text,
    plan text,
    status text DEFAULT 'incomplete'::text NOT NULL,
    current_period_start timestamp with time zone,
    current_period_end timestamp with time zone,
    cancel_at_period_end boolean DEFAULT false NOT NULL,
    grace_ends_at timestamp with time zone,
    "interval" text,
    billing_interval text GENERATED ALWAYS AS ("interval") STORED,
    interval_count integer,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    catalog_version integer,
    plan_version_id uuid,
    subject_id uuid
);


--
-- Name: bursar_config; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.bursar_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    config jsonb NOT NULL,
    active boolean DEFAULT false NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    label text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: catalog_object_versions; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.catalog_object_versions (
    config_version integer NOT NULL,
    object_type text NOT NULL,
    object_key text NOT NULL,
    definition jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT catalog_object_versions_object_type_check CHECK ((object_type = ANY (ARRAY['plan'::text, 'bucket'::text, 'offer'::text, 'topup'::text, 'provider_ref'::text])))
);


--
-- Name: TABLE catalog_object_versions; Type: COMMENT; Schema: bursar; Owner: -
--

COMMENT ON TABLE bursar.catalog_object_versions IS 'Immutable config snapshots for plans, buckets, offers, topups, and provider references.';


--
-- Name: credit_accounts; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_type text NOT NULL CHECK (account_type IN ('personal', 'team')),
    user_id uuid,
    team_id uuid,
    subject_id uuid,
    balance numeric(18,4) NOT NULL DEFAULT 0 CHECK (bursar.is_finite_numeric(balance)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (account_type = 'personal' AND team_id IS NULL AND num_nonnulls(user_id, subject_id) = 1)
        OR (account_type = 'team' AND team_id IS NOT NULL AND user_id IS NULL AND subject_id IS NULL)
    )
);





CREATE TABLE bursar.credit_buckets (
    bucket_key text PRIMARY KEY,
    label text NOT NULL,
    priority integer NOT NULL,
    expires boolean NOT NULL DEFAULT false,
    ttl_days integer CHECK (ttl_days IS NULL OR ttl_days > 0),
    allow_overdraft boolean NOT NULL DEFAULT false,
    is_default boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    config_version integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bursar.credit_ledger_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id),
    actor_user_id uuid,
    amount numeric(18,4) NOT NULL CHECK (amount <> 0 AND bursar.is_finite_numeric(amount)),
    entry_type text NOT NULL,
    reference_entry_id uuid REFERENCES bursar.credit_ledger_entries(id),
    reversal_entry_id uuid REFERENCES bursar.credit_ledger_entries(id),
    idempotency_key text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);




CREATE TABLE bursar.credit_lots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id),
    source_entry_id uuid NOT NULL UNIQUE REFERENCES bursar.credit_ledger_entries(id),
    granted numeric(18,4) NOT NULL CHECK (granted > 0 AND bursar.is_finite_numeric(granted)),
    consumed numeric(18,4) NOT NULL DEFAULT 0 CHECK (consumed >= 0 AND consumed <= granted),
    expires_at timestamptz,
    bucket text NOT NULL DEFAULT 'default',
    created_at timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE bursar.credit_lot_allocations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    debit_entry_id uuid NOT NULL REFERENCES bursar.credit_ledger_entries(id),
    lot_id uuid NOT NULL REFERENCES bursar.credit_lots(id),
    amount numeric(18,4) NOT NULL CHECK (amount > 0 AND bursar.is_finite_numeric(amount)),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (debit_entry_id, lot_id)
);

CREATE TABLE bursar.credit_lot_reversals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    refund_entry_id uuid NOT NULL REFERENCES bursar.credit_ledger_entries(id),
    original_allocation_id uuid NOT NULL REFERENCES bursar.credit_lot_allocations(id),
    amount numeric(18,4) NOT NULL CHECK (amount > 0 AND bursar.is_finite_numeric(amount)),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (refund_entry_id, original_allocation_id)
);

CREATE TABLE bursar.credit_plans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_key text NOT NULL,
    display_name text NOT NULL,
    rate_card text,
    included_amount numeric(18,4) NOT NULL DEFAULT 0 CHECK (included_amount >= 0 AND bursar.is_finite_numeric(included_amount)),
    included_reset jsonb,
    features jsonb NOT NULL DEFAULT '{}'::jsonb,
    limits jsonb NOT NULL DEFAULT '{}'::jsonb,
    spending jsonb NOT NULL DEFAULT '{}'::jsonb,
    config_version integer NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_key, config_version)
);

CREATE TABLE bursar.credit_plan_migrations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_key text NOT NULL,
    target_plan_id uuid NOT NULL REFERENCES bursar.credit_plans(id),
    target_config_version integer NOT NULL,
    migrated_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bursar.credit_leases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id),
    actor_user_id uuid,
    amount numeric(18,4) NOT NULL CHECK (amount > 0 AND bursar.is_finite_numeric(amount)),
    operation_type text NOT NULL,
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '00:10:00'),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'settled', 'released', 'expired')),
    overdraft_floor numeric(18,4) CHECK (overdraft_floor IS NULL OR (overdraft_floor <= 0 AND bursar.is_finite_numeric(overdraft_floor))),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE bursar.account_plan_assignments (
    account_id uuid PRIMARY KEY REFERENCES bursar.credit_accounts(id),
    plan_id uuid REFERENCES bursar.credit_plans(id),
    plan_key text,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bursar.credit_spend_caps (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id),
    cap_type text NOT NULL CHECK (cap_type IN ('daily', 'monthly')),
    model text,
    cap_limit numeric(18,4) NOT NULL CHECK (cap_limit >= 0 AND bursar.is_finite_numeric(cap_limit)),
    action text NOT NULL DEFAULT 'deny' CHECK (action IN ('deny', 'warn', 'notify')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (account_id, cap_type, model)
);

CREATE TABLE bursar.credit_teams (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bursar.credit_team_members (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id uuid NOT NULL REFERENCES bursar.credit_teams(id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    role text NOT NULL DEFAULT 'member',
    spend_cap numeric(18,4) CHECK (spend_cap IS NULL OR (spend_cap >= 0 AND bursar.is_finite_numeric(spend_cap))),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (team_id, user_id)
);

CREATE TABLE bursar.credit_usage_window (
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id),
    feature text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    count integer NOT NULL DEFAULT 0 CHECK (count >= 0),
    PRIMARY KEY (account_id, feature, period_start)
);

CREATE TABLE bursar.account_allowance_usage (
    account_id uuid NOT NULL REFERENCES bursar.credit_accounts(id),
    period_start date NOT NULL,
    consumed numeric(18,4) NOT NULL DEFAULT 0
        CHECK (consumed >= 0 AND bursar.is_finite_numeric(consumed)),
    PRIMARY KEY (account_id, period_start)
);

CREATE TABLE bursar.signup_grant_failures (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    error jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--


-- ============================================================================
-- Source: 020_constraints.sql
-- ============================================================================

-- Name: billing_credit_topups billing_credit_topups_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_credit_topups
    ADD CONSTRAINT billing_credit_topups_pkey PRIMARY KEY (topup_key);


--
-- Name: billing_customers billing_customers_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_customers
    ADD CONSTRAINT billing_customers_pkey PRIMARY KEY (id);


--
-- Name: billing_disputes billing_disputes_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_disputes
    ADD CONSTRAINT billing_disputes_pkey PRIMARY KEY (id);


--
-- Name: billing_events billing_events_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_events
    ADD CONSTRAINT billing_events_pkey PRIMARY KEY (id);


--
-- Name: billing_invoices billing_invoices_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_invoices
    ADD CONSTRAINT billing_invoices_pkey PRIMARY KEY (id);


--
-- Name: billing_offers billing_offers_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_offers
    ADD CONSTRAINT billing_offers_pkey PRIMARY KEY (offer_key);


--
-- Name: billing_payments billing_payments_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_payments
    ADD CONSTRAINT billing_payments_pkey PRIMARY KEY (id);


--
-- Name: billing_preferences billing_preferences_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_preferences
    ADD CONSTRAINT billing_preferences_pkey PRIMARY KEY (user_id);


--
-- Name: billing_provider_refs billing_provider_refs_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_provider_refs
    ADD CONSTRAINT billing_provider_refs_pkey PRIMARY KEY (id);


--
-- Name: billing_refunds billing_refunds_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_refunds
    ADD CONSTRAINT billing_refunds_pkey PRIMARY KEY (id);


--
-- Name: billing_subscriptions billing_subscriptions_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_subscriptions
    ADD CONSTRAINT billing_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: bursar_config bursar_config_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.bursar_config
    ADD CONSTRAINT bursar_config_pkey PRIMARY KEY (id);


--
-- Name: catalog_object_versions catalog_object_versions_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.catalog_object_versions
    ADD CONSTRAINT catalog_object_versions_pkey PRIMARY KEY (config_version, object_type, object_key);


--
--
-- Name: credit_accounts credit_accounts_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_buckets credit_buckets_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_ledger_entries credit_ledger_entries_account_id_entry_type_idempotency_key_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_ledger_entries credit_ledger_entries_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--






--
-- Name: credit_lot_allocations credit_lot_allocations_debit_entry_id_lot_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_allocations credit_lot_allocations_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_reversals credit_lot_reversals_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_reversals credit_lot_reversals_refund_entry_id_original_allocation_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lots credit_lots_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lots credit_lots_source_entry_unique; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_plan_migrations credit_plan_migrations_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_plans credit_plans_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_leases credit_leases_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_spend_caps credit_spend_caps_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_team_members credit_team_members_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_team_members credit_team_members_team_id_user_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_teams credit_teams_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--



--
-- Name: credit_usage_window credit_usage_window_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: signup_grant_failures signup_grant_failures_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--



--


-- ============================================================================
-- Source: 030_indexes.sql
-- ============================================================================

-- Name: billing_events_claimable_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX billing_events_claimable_idx ON bursar.billing_events USING btree (created_at, id) WHERE (status = ANY (ARRAY['processing'::text, 'failed'::text]));


--
-- Name: billing_provider_refs_lookup_environment_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX billing_provider_refs_lookup_environment_uq ON bursar.billing_provider_refs USING btree (provider, environment, lookup_key) WHERE (lookup_key IS NOT NULL);


--
-- Name: billing_provider_refs_price_environment_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX billing_provider_refs_price_environment_uq ON bursar.billing_provider_refs USING btree (provider, environment, price_id) WHERE (price_id IS NOT NULL);


--
-- Name: billing_provider_refs_product_environment_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX billing_provider_refs_product_environment_uq ON bursar.billing_provider_refs USING btree (provider, environment, product_id) WHERE (product_id IS NOT NULL);


--
-- Name: billing_provider_refs_variant_environment_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX billing_provider_refs_variant_environment_uq ON bursar.billing_provider_refs USING btree (provider, environment, variant_id) WHERE (variant_id IS NOT NULL);


--
-- Name: catalog_object_versions_type_key_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX catalog_object_versions_type_key_idx ON bursar.catalog_object_versions USING btree (object_type, object_key, config_version DESC);


--
-- Name: credit_accounts_personal_owner_uq; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: credit_accounts_team_owner_uq; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: credit_ledger_account_cursor_idx; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_allocations_lot_idx; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_reversals_original_idx; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: credit_lots_active_idx; Type: INDEX; Schema: bursar; Owner: -
--




--


--


--


--
-- Name: idx_billing_customers_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_customers_provider ON bursar.billing_customers USING btree (provider, provider_customer_id);


--
-- Name: idx_billing_customers_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_customers_user ON bursar.billing_customers USING btree (user_id);


--
-- Name: idx_billing_disputes_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_disputes_provider ON bursar.billing_disputes USING btree (provider, provider_dispute_id);


--
-- Name: idx_billing_disputes_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_disputes_user ON bursar.billing_disputes USING btree (user_id);


--
-- Name: idx_billing_events_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_events_provider ON bursar.billing_events USING btree (provider, provider_event_id);


--
-- Name: idx_billing_events_status; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_events_status ON bursar.billing_events USING btree (status);


--
-- Name: idx_billing_invoices_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_invoices_provider ON bursar.billing_invoices USING btree (provider, provider_invoice_id);


--
-- Name: idx_billing_invoices_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_invoices_user ON bursar.billing_invoices USING btree (user_id);


--
-- Name: idx_billing_offers_plan; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_offers_plan ON bursar.billing_offers USING btree (plan);


--
-- Name: idx_billing_payments_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_payments_provider ON bursar.billing_payments USING btree (provider, provider_payment_id);


--
-- Name: idx_billing_payments_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_payments_user ON bursar.billing_payments USING btree (user_id);


--
-- Name: idx_billing_provider_refs_resource; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_provider_refs_resource ON bursar.billing_provider_refs USING btree (resource_type, resource_key);


--
-- Name: idx_billing_refunds_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_refunds_provider ON bursar.billing_refunds USING btree (provider, provider_refund_id);


--
-- Name: idx_billing_subscriptions_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_subscriptions_provider ON bursar.billing_subscriptions USING btree (provider, provider_subscription_id);


--
-- Name: idx_billing_subscriptions_status; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_subscriptions_status ON bursar.billing_subscriptions USING btree (status);


--
-- Name: idx_billing_subscriptions_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_subscriptions_user ON bursar.billing_subscriptions USING btree (user_id);


--
-- Name: idx_bursar_config_active_unique; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_bursar_config_active_unique ON bursar.bursar_config USING btree (active) WHERE (active = true);


--
-- Name: idx_bursar_config_version_unique; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_bursar_config_version_unique ON bursar.bursar_config USING btree (version);


--
-- Name: idx_credit_buckets_single_default; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: idx_credit_buckets_single_overdraft; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: idx_credit_plan_migrations_user; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: idx_credit_plans_plan_key; Type: INDEX; Schema: bursar; Owner: -
--



--
-- Name: idx_credit_plans_plan_key_version; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: idx_credit_leases_active; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: idx_credit_leases_user_expires; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: idx_credit_spend_caps_unique; Type: INDEX; Schema: bursar; Owner: -
--




--


--


--
--



--
--



--


--


--


--
--



--
--



--
-- Name: idx_credit_usage_window_plan_id; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: idx_credit_usage_window_unique; Type: INDEX; Schema: bursar; Owner: -
--




--
-- Name: idx_signup_grant_failures_user; Type: INDEX; Schema: bursar; Owner: -
--




--
--
-- The composite primary key already covers user_id lookups.

CREATE INDEX billing_subscriptions_offer_key_idx ON bursar.billing_subscriptions (offer_key) WHERE offer_key IS NOT NULL;
CREATE INDEX billing_subscriptions_plan_version_id_idx ON bursar.billing_subscriptions (plan_version_id) WHERE plan_version_id IS NOT NULL;




--

ALTER TABLE ONLY bursar.billing_subscriptions
    ADD CONSTRAINT billing_subscriptions_offer_key_fkey FOREIGN KEY (offer_key) REFERENCES bursar.billing_offers(offer_key);


--
-- Name: billing_subscriptions billing_subscriptions_plan_version_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_subscriptions
    ADD CONSTRAINT billing_subscriptions_plan_version_id_fkey FOREIGN KEY (plan_version_id) REFERENCES bursar.credit_plans(id);


--
-- Name: catalog_object_versions catalog_object_versions_config_version_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.catalog_object_versions
    ADD CONSTRAINT catalog_object_versions_config_version_fkey FOREIGN KEY (config_version) REFERENCES bursar.bursar_config(version) ON DELETE RESTRICT;


--
-- Name: credit_ledger_entries credit_ledger_entries_account_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_allocations credit_lot_allocations_debit_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_allocations credit_lot_allocations_lot_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_reversals credit_lot_reversals_original_allocation_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lot_reversals credit_lot_reversals_refund_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lots credit_lots_account_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_lots credit_lots_source_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_plan_migrations credit_plan_migrations_from_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_plan_migrations credit_plan_migrations_to_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_plan_migrations credit_plan_migrations_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar


--
-- Name: credit_leases credit_leases_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar



--
-- Name: credit_team_members credit_team_members_team_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_team_members credit_team_members_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar


--
-- Name: credit_usage_window credit_usage_window_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--




--
-- Name: credit_usage_window credit_usage_window_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar


--

CREATE TABLE bursar.billing_checkout_intents (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    actor_key text NOT NULL,
    provider text NOT NULL,
    type text NOT NULL CHECK (type IN ('subscription', 'credit_pack')),
    product_id text NOT NULL,
    request_fingerprint text NOT NULL,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'completed', 'failed', 'expired')),
    provider_session_id text,
    checkout_url text,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (actor_key, provider, type, product_id, request_fingerprint)
);

CREATE TABLE bursar.billing_subscription_changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    provider text NOT NULL,
    provider_subscription_id text NOT NULL,
    from_plan text NOT NULL,
    from_interval text NOT NULL,
    to_plan text NOT NULL,
    to_interval text NOT NULL,
    effective_at text NOT NULL,
    state text NOT NULL,
    proration_billing_mode text NOT NULL,
    quote jsonb,
    quote_hash text,
    provider_operation_id text,
    effective_date timestamptz,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX billing_subscription_changes_open_idx
    ON bursar.billing_subscription_changes(provider, provider_subscription_id, created_at DESC)
    WHERE state IN ('awaiting_payment', 'scheduled');

CREATE TABLE bursar.billing_subscription_conflicts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid,
    provider text NOT NULL,
    duplicate_subscription_id text NOT NULL,
    existing_subscription_id text,
    event_id text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolution text,
    UNIQUE (provider, duplicate_subscription_id)
);

CREATE TABLE bursar.billing_auto_recharge_profiles (
    user_id uuid PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'disabled'
        CHECK (state IN ('disabled', 'active', 'suspended')),
    armed boolean NOT NULL DEFAULT true,
    provider text,
    provider_customer_id text,
    payment_method_id text,
    policy_override jsonb,
    policy_snapshot jsonb,
    policy_hash text,
    quote_snapshot jsonb,
    consent_reference text,
    consent_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    suspended_reason text,
    consented_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bursar.billing_auto_recharge_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid,
    subject_id uuid,
    provider text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    provider_payment_id text,
    topup_key text NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    trigger_balance numeric(18,4)
        CHECK (trigger_balance IS NULL OR bursar.is_finite_numeric(trigger_balance)),
    policy_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    policy_hash text,
    quoted_amount_minor bigint CHECK (quoted_amount_minor IS NULL OR quoted_amount_minor >= 0),
    final_amount_minor bigint CHECK (final_amount_minor IS NULL OR final_amount_minor >= 0),
    currency text CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    state text NOT NULL DEFAULT 'claimed'
        CHECK (state IN (
            'claimed', 'submitted', 'processing', 'unknown',
            'succeeded', 'failed', 'action_required'
        )),
    credits numeric(18,4)
        CHECK (credits IS NULL OR bursar.is_finite_numeric(credits)),
    failure_category text,
    failure_code text,
    failure_message text,
    action_url text,
    submitted_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX billing_auto_recharge_attempts_user_created_idx
    ON bursar.billing_auto_recharge_attempts(user_id, created_at DESC);
CREATE INDEX billing_auto_recharge_attempts_provider_payment_idx
    ON bursar.billing_auto_recharge_attempts(provider, provider_payment_id)
    WHERE provider_payment_id IS NOT NULL;

CREATE UNIQUE INDEX credit_accounts_user_uq
    ON bursar.credit_accounts(user_id)
    WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX credit_accounts_subject_uq
    ON bursar.credit_accounts(subject_id)
    WHERE subject_id IS NOT NULL;
CREATE UNIQUE INDEX credit_accounts_team_uq
    ON bursar.credit_accounts(team_id)
    WHERE team_id IS NOT NULL;
CREATE UNIQUE INDEX credit_ledger_entries_idempotency_uq
    ON bursar.credit_ledger_entries(account_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX credit_ledger_entries_cursor_idx
    ON bursar.credit_ledger_entries(account_id, created_at DESC, id DESC);
CREATE INDEX credit_ledger_entries_type_cursor_idx
    ON bursar.credit_ledger_entries(account_id, entry_type, created_at DESC, id DESC);
CREATE INDEX credit_lots_spend_idx
    ON bursar.credit_lots(account_id, expires_at, created_at, id)
    WHERE consumed < granted;
CREATE INDEX credit_leases_active_idx
    ON bursar.credit_leases(account_id, expires_at)
    WHERE status = 'active';
