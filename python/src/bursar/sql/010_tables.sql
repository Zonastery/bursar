-- Name: billing_credit_topups; Type: TABLE; Schema: bursar; Owner: -
--

CREATE TABLE bursar.billing_credit_topups (
    topup_key text NOT NULL,
    deposit_to text DEFAULT 'purchased'::text NOT NULL,
    credits_per_unit integer DEFAULT 1000 NOT NULL,
    min_amount_minor integer DEFAULT 500 NOT NULL,
    max_amount_minor integer DEFAULT 500000 NOT NULL,
    tax_behavior text DEFAULT 'exclude_tax'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    CONSTRAINT billing_credit_topups_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text]))),
    CONSTRAINT billing_credit_topups_tax_behavior_check CHECK ((tax_behavior = ANY (ARRAY['exclude_tax'::text, 'include_tax'::text])))
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
    interval_count integer DEFAULT 1 NOT NULL,
    grant_mode text DEFAULT 'allowance'::text NOT NULL,
    grant_credits integer,
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
    balance numeric(18,4) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_accounts_account_type_check CHECK ((account_type = ANY (ARRAY['personal'::text, 'team'::text]))),
    CONSTRAINT credit_accounts_check CHECK ((((account_type = 'personal'::text) AND (user_id IS NOT NULL) AND (team_id IS NULL)) OR ((account_type = 'team'::text) AND (team_id IS NOT NULL) AND (user_id IS NULL))))
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
    CONSTRAINT credit_ledger_entries_amount_check CHECK ((amount <> (0)::numeric))
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
    CONSTRAINT credit_lot_allocations_amount_check CHECK ((amount > (0)::numeric))
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
    CONSTRAINT credit_lot_reversals_amount_check CHECK ((amount > (0)::numeric))
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
    CONSTRAINT credit_lots_check CHECK (((consumed >= (0)::numeric) AND (consumed <= granted))),
    CONSTRAINT credit_lots_granted_check CHECK ((granted > (0)::numeric))
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
    CONSTRAINT credit_plans_status_check CHECK ((status = ANY (ARRAY['active'::text, 'retired'::text])))
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
    CONSTRAINT credit_reservations_amount_check CHECK ((amount > (0)::numeric))
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
    CONSTRAINT credit_spend_caps_cap_type_check CHECK ((cap_type = ANY (ARRAY['daily'::text, 'monthly'::text])))
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
    joined_at timestamp with time zone DEFAULT now() NOT NULL
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
    updated_at timestamp with time zone DEFAULT now() NOT NULL
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
    account_id uuid,
    acting_user_id uuid
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
    updated_at timestamp with time zone DEFAULT now() NOT NULL
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
    updated_at timestamp with time zone DEFAULT now() NOT NULL
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
);


--
