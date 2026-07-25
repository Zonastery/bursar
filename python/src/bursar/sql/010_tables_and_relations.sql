
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
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_type text NOT NULL,
    user_id uuid,
    team_id uuid,
    subject_id uuid,
    balance numeric(18,4) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_accounts_account_type_check CHECK ((account_type = ANY (ARRAY['personal'::text, 'team'::text]))),
    CONSTRAINT credit_accounts_check CHECK (((account_type = 'personal'::text AND team_id IS NULL AND num_nonnulls(user_id, subject_id) = 1) OR (account_type = 'team'::text AND team_id IS NOT NULL AND user_id IS NULL AND subject_id IS NULL))),
    CONSTRAINT credit_accounts_balance_finite CHECK (bursar.is_finite_numeric(balance))
);


--
-- Name: credit_buckets; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_buckets (
    bucket_key text NOT NULL,
    label text NOT NULL,
    priority integer NOT NULL,
    expires boolean DEFAULT false NOT NULL,
    ttl_days integer,
    allow_overdraft boolean DEFAULT false NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    config_version integer,
    CONSTRAINT credit_buckets_status_check CHECK ((status = ANY (ARRAY['active'::text, 'retired'::text])))
);


--
-- Name: credit_ledger_entries; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_ledger_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    source_transaction_id uuid,
    amount numeric(18,4) NOT NULL,
    entry_type text NOT NULL,
    idempotency_key text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reference_transaction_id uuid,
    CONSTRAINT credit_ledger_entries_amount_check CHECK ((amount <> (0)::numeric AND bursar.is_finite_numeric(amount)))
);


--
-- Name: TABLE credit_ledger_entries; Type: COMMENT; Schema: bursar; Owner: -
--

COMMENT ON TABLE bursar.credit_ledger_entries IS 'Append-only account ledger; credit_transactions is a compatibility input projection.';


--
-- Name: credit_lot_allocations; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_lot_allocations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    debit_entry_id uuid NOT NULL,
    lot_id uuid,
    amount numeric(18,4) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_lot_allocations_amount_check CHECK ((amount > (0)::numeric AND bursar.is_finite_numeric(amount)))
);


--
-- Name: credit_lot_reversals; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_lot_reversals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    refund_entry_id uuid NOT NULL,
    original_allocation_id uuid NOT NULL,
    amount numeric(18,4) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_lot_reversals_amount_check CHECK ((amount > (0)::numeric AND bursar.is_finite_numeric(amount)))
);


--
-- Name: credit_lots; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_lots (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    source_entry_id uuid NOT NULL,
    granted numeric(18,4) NOT NULL,
    consumed numeric(18,4) DEFAULT 0 NOT NULL,
    expires_at timestamp with time zone,
    bucket text DEFAULT 'default'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_lots_check CHECK ((bursar.is_finite_numeric(consumed) AND consumed >= (0)::numeric) AND (consumed <= granted)),
    CONSTRAINT credit_lots_granted_check CHECK ((granted > (0)::numeric AND bursar.is_finite_numeric(granted)))
);


--
-- Name: credit_plan_migrations; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_plan_migrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    from_plan_id uuid,
    to_plan_id uuid NOT NULL,
    from_config_version integer,
    to_config_version integer,
    effective_at timestamp with time zone DEFAULT now() NOT NULL,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: credit_plans; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    label text NOT NULL,
    description text,
    allowance_amount numeric(18,4) DEFAULT 0 NOT NULL,
    rate_overrides jsonb DEFAULT '{}'::jsonb,
    entitlements jsonb DEFAULT '{}'::jsonb,
    plan_key text,
    billing_mode text DEFAULT 'strict'::text NOT NULL,
    per_operation jsonb,
    max_concurrent integer,
    overdraft_floor numeric(18,4),
    allowance_period text DEFAULT 'calendar_month'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    config_version integer NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    CONSTRAINT credit_plans_status_check CHECK ((status = ANY (ARRAY['active'::text, 'retired'::text]))),
    CONSTRAINT credit_plans_allowance_check CHECK (allowance_amount >= 0 AND bursar.is_finite_numeric(allowance_amount)),
    CONSTRAINT credit_plans_billing_mode_check CHECK (billing_mode IN ('strict', 'overdraft')),
    CONSTRAINT credit_plans_max_concurrent_check CHECK (max_concurrent IS NULL OR max_concurrent > 0),
    CONSTRAINT credit_plans_overdraft_floor_check CHECK (overdraft_floor IS NULL OR (overdraft_floor <= 0 AND bursar.is_finite_numeric(overdraft_floor))),
    CONSTRAINT credit_plans_allowance_period_check CHECK (allowance_period IN ('calendar_month', 'rolling_30d', 'anniversary'))
);


--
-- Name: credit_reservations; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_reservations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    amount numeric(18,4) NOT NULL,
    operation_type text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    expires_at timestamp with time zone DEFAULT (now() + '00:10:00'::interval) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    billing_mode text DEFAULT 'strict'::text NOT NULL,
    overdraft_floor numeric(18,4),
    settle_tx_id uuid,
    account_id uuid NOT NULL,
    idempotency_key text GENERATED ALWAYS AS ((metadata ->> 'idempotency_key'::text)) STORED,
    CONSTRAINT credit_reservations_amount_check CHECK ((amount > (0)::numeric AND bursar.is_finite_numeric(amount))),
    CONSTRAINT credit_reservations_billing_mode_check CHECK (billing_mode IN ('strict', 'overdraft')),
    CONSTRAINT credit_reservations_overdraft_floor_check CHECK (overdraft_floor IS NULL OR (overdraft_floor <= 0 AND bursar.is_finite_numeric(overdraft_floor))),
    CONSTRAINT credit_reservations_status_check CHECK (status IN ('active', 'settled', 'released', 'expired'))
);


--
-- Name: credit_spend_caps; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_spend_caps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    cap_type text NOT NULL,
    model text,
    cap_limit numeric(18,4) NOT NULL,
    action text DEFAULT 'deny'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_spend_caps_action_check CHECK ((action = ANY (ARRAY['deny'::text, 'warn'::text, 'notify'::text]))),
    CONSTRAINT credit_spend_caps_cap_type_check CHECK ((cap_type = ANY (ARRAY['daily'::text, 'monthly'::text]))),
    CONSTRAINT credit_spend_caps_limit_check CHECK (cap_limit > 0 AND bursar.is_finite_numeric(cap_limit))
);


--
-- Name: credit_team_members; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_team_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    team_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role text DEFAULT 'member'::text NOT NULL,
    spend_cap numeric(18,4),
    total_spent numeric(18,4) DEFAULT 0 NOT NULL,
    joined_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_team_members_spend_check CHECK (spend_cap IS NULL OR (spend_cap >= 0 AND bursar.is_finite_numeric(spend_cap))),
    CONSTRAINT credit_team_members_total_spent_check CHECK (total_spent >= 0 AND bursar.is_finite_numeric(total_spent))
);


--
-- Name: credit_teams; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_teams (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    balance numeric(18,4) DEFAULT 0 NOT NULL,
    member_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_teams_balance_finite CHECK (bursar.is_finite_numeric(balance)),
    CONSTRAINT credit_teams_member_count_check CHECK (member_count >= 0)
);


--
-- Name: credit_transactions; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    amount numeric(18,4) NOT NULL,
    type bursar.credit_tx_type NOT NULL,
    reference_type text,
    reference_id uuid,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    idempotency_key text GENERATED ALWAYS AS ((metadata ->> 'idempotency_key'::text)) STORED,
    account_id uuid NOT NULL,
    acting_user_id uuid NOT NULL,
    CONSTRAINT credit_transactions_amount_finite CHECK (bursar.is_finite_numeric(amount))
);


--
-- Name: COLUMN credit_transactions.account_id; Type: COMMENT; Schema: bursar; Owner: -
--

COMMENT ON COLUMN bursar.credit_transactions.account_id IS 'The charged personal or team account; user_id is actor/compatibility ownership.';


--
-- Name: credit_usage_window; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.credit_usage_window (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    billing_period date NOT NULL,
    usage numeric(18,4) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_usage_window_usage_check CHECK (usage >= 0 AND bursar.is_finite_numeric(usage))
);


--
-- Name: signup_grant_failures; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.signup_grant_failures (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    error jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_credit_buckets; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.user_credit_buckets (
    user_id uuid NOT NULL,
    bucket_key text NOT NULL,
    balance numeric(18,4) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_credit_buckets_balance_finite CHECK (bursar.is_finite_numeric(balance))
);


--
-- Name: user_credits; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.user_credits (
    user_id uuid NOT NULL,
    balance numeric(18,4) DEFAULT 0 NOT NULL,
    lifetime_purchased numeric(18,4) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    plan_id uuid,
    plan_assigned_at timestamp with time zone,
    catalog_version integer
    ,CONSTRAINT user_credits_balance_finite CHECK (bursar.is_finite_numeric(balance))
    ,CONSTRAINT user_credits_lifetime_purchased_check CHECK (lifetime_purchased >= 0 AND bursar.is_finite_numeric(lifetime_purchased))
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

ALTER TABLE ONLY bursar.credit_accounts
    ADD CONSTRAINT credit_accounts_pkey PRIMARY KEY (id);


--
-- Name: credit_buckets credit_buckets_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_buckets
    ADD CONSTRAINT credit_buckets_pkey PRIMARY KEY (bucket_key);


--
-- Name: credit_ledger_entries credit_ledger_entries_account_id_entry_type_idempotency_key_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_ledger_entries
    ADD CONSTRAINT credit_ledger_entries_account_id_entry_type_idempotency_key_key UNIQUE (account_id, entry_type, idempotency_key);


--
-- Name: credit_ledger_entries credit_ledger_entries_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_ledger_entries
    ADD CONSTRAINT credit_ledger_entries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY bursar.credit_ledger_entries
    ADD CONSTRAINT credit_ledger_entries_source_transaction_uq UNIQUE (source_transaction_id);


--
-- Name: credit_lot_allocations credit_lot_allocations_debit_entry_id_lot_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_allocations
    ADD CONSTRAINT credit_lot_allocations_debit_entry_id_lot_id_key UNIQUE (debit_entry_id, lot_id);


--
-- Name: credit_lot_allocations credit_lot_allocations_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_allocations
    ADD CONSTRAINT credit_lot_allocations_pkey PRIMARY KEY (id);


--
-- Name: credit_lot_reversals credit_lot_reversals_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_reversals
    ADD CONSTRAINT credit_lot_reversals_pkey PRIMARY KEY (id);


--
-- Name: credit_lot_reversals credit_lot_reversals_refund_entry_id_original_allocation_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_reversals
    ADD CONSTRAINT credit_lot_reversals_refund_entry_id_original_allocation_id_key UNIQUE (refund_entry_id, original_allocation_id);


--
-- Name: credit_lots credit_lots_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lots
    ADD CONSTRAINT credit_lots_pkey PRIMARY KEY (id);


--
-- Name: credit_lots credit_lots_source_entry_unique; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lots
    ADD CONSTRAINT credit_lots_source_entry_unique UNIQUE (source_entry_id);


--
-- Name: credit_plan_migrations credit_plan_migrations_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plan_migrations
    ADD CONSTRAINT credit_plan_migrations_pkey PRIMARY KEY (id);


--
-- Name: credit_plans credit_plans_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plans
    ADD CONSTRAINT credit_plans_pkey PRIMARY KEY (id);


--
-- Name: credit_reservations credit_reservations_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_reservations
    ADD CONSTRAINT credit_reservations_pkey PRIMARY KEY (id);


--
-- Name: credit_spend_caps credit_spend_caps_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_spend_caps
    ADD CONSTRAINT credit_spend_caps_pkey PRIMARY KEY (id);


--
-- Name: credit_team_members credit_team_members_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_team_members
    ADD CONSTRAINT credit_team_members_pkey PRIMARY KEY (id);


--
-- Name: credit_team_members credit_team_members_team_id_user_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_team_members
    ADD CONSTRAINT credit_team_members_team_id_user_id_key UNIQUE (team_id, user_id);


--
-- Name: credit_teams credit_teams_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_teams
    ADD CONSTRAINT credit_teams_pkey PRIMARY KEY (id);


--
-- Name: credit_transactions credit_transactions_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_transactions
    ADD CONSTRAINT credit_transactions_pkey PRIMARY KEY (id);


--
-- Name: credit_usage_window credit_usage_window_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_usage_window
    ADD CONSTRAINT credit_usage_window_pkey PRIMARY KEY (id);


--
-- Name: signup_grant_failures signup_grant_failures_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.signup_grant_failures
    ADD CONSTRAINT signup_grant_failures_pkey PRIMARY KEY (id);


--
-- Name: user_credit_buckets user_credit_buckets_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credit_buckets
    ADD CONSTRAINT user_credit_buckets_pkey PRIMARY KEY (user_id, bucket_key);


--
-- Name: user_credits user_credits_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credits
    ADD CONSTRAINT user_credits_pkey PRIMARY KEY (user_id);


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

CREATE UNIQUE INDEX credit_accounts_personal_owner_uq ON bursar.credit_accounts USING btree (user_id) WHERE (account_type = 'personal'::text);


--
-- Name: credit_accounts_team_owner_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX credit_accounts_team_owner_uq ON bursar.credit_accounts USING btree (team_id) WHERE (account_type = 'team'::text);


--
-- Name: credit_ledger_account_cursor_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_ledger_account_cursor_idx ON bursar.credit_ledger_entries USING btree (account_id, created_at DESC, id DESC);


--
-- Name: credit_lot_allocations_lot_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_lot_allocations_lot_idx ON bursar.credit_lot_allocations USING btree (lot_id, created_at DESC) WHERE (lot_id IS NOT NULL);


--
-- Name: credit_lot_reversals_original_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_lot_reversals_original_idx ON bursar.credit_lot_reversals USING btree (original_allocation_id, created_at DESC);


--
-- Name: credit_lots_active_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_lots_active_idx ON bursar.credit_lots USING btree (account_id, expires_at) WHERE (consumed < granted);


--
-- Name: credit_transactions_account_cursor_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_transactions_account_cursor_idx ON bursar.credit_transactions USING btree (account_id, created_at DESC, id DESC);


--
-- Name: credit_transactions_operation_key_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX credit_transactions_operation_key_uq ON bursar.credit_transactions USING btree (account_id, type, idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: credit_transactions_user_cursor_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_transactions_user_cursor_idx ON bursar.credit_transactions USING btree (user_id, created_at DESC, id DESC);


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

CREATE UNIQUE INDEX idx_credit_buckets_single_default ON bursar.credit_buckets USING btree (is_default) WHERE (is_default = true);


--
-- Name: idx_credit_buckets_single_overdraft; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_buckets_single_overdraft ON bursar.credit_buckets USING btree (allow_overdraft) WHERE (allow_overdraft = true);


--
-- Name: idx_credit_plan_migrations_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_plan_migrations_user ON bursar.credit_plan_migrations USING btree (user_id, created_at DESC);


--
-- Name: idx_credit_plans_plan_key; Type: INDEX; Schema: bursar; Owner: -
--



--
-- Name: idx_credit_plans_plan_key_version; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_plans_plan_key_version ON bursar.credit_plans USING btree (plan_key, config_version) WHERE (plan_key IS NOT NULL);


--
-- Name: idx_credit_reservations_active; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_reservations_active ON bursar.credit_reservations USING btree (user_id, operation_type, status, expires_at);


--
-- Name: idx_credit_reservations_user_expires; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_reservations_user_expires ON bursar.credit_reservations USING btree (user_id, expires_at);


--
-- Name: idx_credit_spend_caps_unique; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_spend_caps_unique ON bursar.credit_spend_caps USING btree (user_id, cap_type, COALESCE(model, ''::text));


--
-- Name: idx_credit_transactions_created_at; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_created_at ON bursar.credit_transactions USING btree (created_at);


--
-- Name: idx_credit_transactions_expires_at; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_expires_at ON bursar.credit_transactions USING btree (((metadata ->> 'expires_at'::text))) WHERE ((metadata ? 'expires_at'::text) AND (NOT (metadata ? 'swept_at'::text)));


--
-- Name: idx_credit_transactions_idempotency_team_usage; Type: INDEX; Schema: bursar; Owner: -
--



--
-- Name: idx_credit_transactions_idempotency_user; Type: INDEX; Schema: bursar; Owner: -
--



--
-- Name: idx_credit_transactions_reference_id; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_reference_id ON bursar.credit_transactions USING btree (reference_id) WHERE (reference_id IS NOT NULL);


--
-- Name: idx_credit_transactions_type_created; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_type_created ON bursar.credit_transactions USING btree (type, created_at DESC);


--
-- Name: idx_credit_transactions_user_expires; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_user_expires ON bursar.credit_transactions USING btree (user_id, ((metadata ->> 'expires_at'::text))) WHERE ((metadata ? 'expires_at'::text) AND (NOT (metadata ? 'swept_at'::text)));


--
-- Name: idx_credit_transactions_user_id; Type: INDEX; Schema: bursar; Owner: -
--



--
-- Name: idx_credit_transactions_user_id_created_at; Type: INDEX; Schema: bursar; Owner: -
--



--
-- Name: idx_credit_usage_window_plan_id; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_usage_window_plan_id ON bursar.credit_usage_window USING btree (plan_id);


--
-- Name: idx_credit_usage_window_unique; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_usage_window_unique ON bursar.credit_usage_window USING btree (user_id, plan_id, billing_period);


--
-- Name: idx_signup_grant_failures_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_signup_grant_failures_user ON bursar.signup_grant_failures USING btree (user_id, created_at DESC);


--
-- Name: idx_user_credit_buckets_user; Type: INDEX; Schema: bursar; Owner: -
--
-- The composite primary key already covers user_id lookups.

CREATE INDEX billing_subscriptions_offer_key_idx ON bursar.billing_subscriptions (offer_key) WHERE offer_key IS NOT NULL;
CREATE INDEX billing_subscriptions_plan_version_id_idx ON bursar.billing_subscriptions (plan_version_id) WHERE plan_version_id IS NOT NULL;
CREATE INDEX credit_plan_migrations_from_plan_id_idx ON bursar.credit_plan_migrations (from_plan_id) WHERE from_plan_id IS NOT NULL;
CREATE INDEX credit_plan_migrations_to_plan_id_idx ON bursar.credit_plan_migrations (to_plan_id);
CREATE INDEX credit_reservations_settle_tx_id_idx ON bursar.credit_reservations (settle_tx_id) WHERE settle_tx_id IS NOT NULL;
CREATE INDEX credit_reservations_account_id_idx ON bursar.credit_reservations (account_id);
CREATE INDEX credit_team_members_user_id_idx ON bursar.credit_team_members (user_id);
CREATE INDEX user_credits_plan_id_idx ON bursar.user_credits (plan_id) WHERE plan_id IS NOT NULL;

CREATE INDEX credit_transactions_feature_window_idx
  ON bursar.credit_transactions (user_id, ((metadata ->> 'feature')), created_at)
  WHERE type = 'usage'::bursar.credit_tx_type;
CREATE INDEX credit_transactions_model_window_idx
  ON bursar.credit_transactions (user_id, ((metadata ->> 'model')), created_at)
  INCLUDE (amount)
  WHERE type IN ('usage'::bursar.credit_tx_type, 'team_usage'::bursar.credit_tx_type) AND amount < 0;
CREATE INDEX credit_transactions_team_window_idx
  ON bursar.credit_transactions (account_id, acting_user_id, created_at)
  INCLUDE (amount)
  WHERE type = 'team_usage'::bursar.credit_tx_type AND amount < 0;

--


-- ============================================================================
-- Source: 040_foreign_keys.sql
-- ============================================================================

-- Name: billing_subscriptions billing_subscriptions_offer_key_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
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

ALTER TABLE ONLY bursar.credit_ledger_entries
    ADD CONSTRAINT credit_ledger_entries_account_id_fkey FOREIGN KEY (account_id) REFERENCES bursar.credit_accounts(id);


--
-- Name: credit_lot_allocations credit_lot_allocations_debit_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_allocations
    ADD CONSTRAINT credit_lot_allocations_debit_entry_id_fkey FOREIGN KEY (debit_entry_id) REFERENCES bursar.credit_ledger_entries(id);


--
-- Name: credit_lot_allocations credit_lot_allocations_lot_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_allocations
    ADD CONSTRAINT credit_lot_allocations_lot_id_fkey FOREIGN KEY (lot_id) REFERENCES bursar.credit_lots(id);


--
-- Name: credit_lot_reversals credit_lot_reversals_original_allocation_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_reversals
    ADD CONSTRAINT credit_lot_reversals_original_allocation_id_fkey FOREIGN KEY (original_allocation_id) REFERENCES bursar.credit_lot_allocations(id);


--
-- Name: credit_lot_reversals credit_lot_reversals_refund_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_reversals
    ADD CONSTRAINT credit_lot_reversals_refund_entry_id_fkey FOREIGN KEY (refund_entry_id) REFERENCES bursar.credit_ledger_entries(id);


--
-- Name: credit_lots credit_lots_account_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lots
    ADD CONSTRAINT credit_lots_account_id_fkey FOREIGN KEY (account_id) REFERENCES bursar.credit_accounts(id);


--
-- Name: credit_lots credit_lots_source_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lots
    ADD CONSTRAINT credit_lots_source_entry_id_fkey FOREIGN KEY (source_entry_id) REFERENCES bursar.credit_ledger_entries(id);


--
-- Name: credit_plan_migrations credit_plan_migrations_from_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plan_migrations
    ADD CONSTRAINT credit_plan_migrations_from_plan_id_fkey FOREIGN KEY (from_plan_id) REFERENCES bursar.credit_plans(id);


--
-- Name: credit_plan_migrations credit_plan_migrations_to_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plan_migrations
    ADD CONSTRAINT credit_plan_migrations_to_plan_id_fkey FOREIGN KEY (to_plan_id) REFERENCES bursar.credit_plans(id);


--
-- Name: credit_plan_migrations credit_plan_migrations_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plan_migrations
    ADD CONSTRAINT credit_plan_migrations_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_reservations credit_reservations_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_reservations
    ADD CONSTRAINT credit_reservations_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;

ALTER TABLE ONLY bursar.credit_reservations
    ADD CONSTRAINT credit_reservations_account_id_fkey FOREIGN KEY (account_id) REFERENCES bursar.credit_accounts(id);

ALTER TABLE ONLY bursar.credit_reservations
    ADD CONSTRAINT credit_reservations_settle_tx_id_fkey FOREIGN KEY (settle_tx_id) REFERENCES bursar.credit_transactions(id);


--
-- Name: credit_spend_caps credit_spend_caps_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_spend_caps
    ADD CONSTRAINT credit_spend_caps_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_team_members credit_team_members_team_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_team_members
    ADD CONSTRAINT credit_team_members_team_id_fkey FOREIGN KEY (team_id) REFERENCES bursar.credit_teams(id) ON DELETE CASCADE;


--
-- Name: credit_team_members credit_team_members_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_team_members
    ADD CONSTRAINT credit_team_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_transactions credit_transactions_account_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_transactions
    ADD CONSTRAINT credit_transactions_account_id_fkey FOREIGN KEY (account_id) REFERENCES bursar.credit_accounts(id);


--
-- Name: credit_transactions credit_transactions_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_transactions
    ADD CONSTRAINT credit_transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_usage_window credit_usage_window_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_usage_window
    ADD CONSTRAINT credit_usage_window_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES bursar.credit_plans(id);


--
-- Name: credit_usage_window credit_usage_window_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_usage_window
    ADD CONSTRAINT credit_usage_window_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: user_credit_buckets user_credit_buckets_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credit_buckets
    ADD CONSTRAINT user_credit_buckets_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: user_credits user_credits_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credits
    ADD CONSTRAINT user_credits_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES bursar.credit_plans(id);


--
-- Name: user_credits user_credits_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credits
    ADD CONSTRAINT user_credits_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;


--

