
-- ============================================================================
-- Source: 110_triggers.sql
-- ============================================================================

-- Name: bursar_config bursar_config_catalog_snapshot; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER bursar_config_catalog_snapshot AFTER INSERT OR UPDATE OF active ON bursar.bursar_config FOR EACH ROW EXECUTE FUNCTION bursar.capture_activated_catalog_snapshot();


--
-- Name: bursar_config bursar_config_immutable; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER bursar_config_immutable BEFORE UPDATE ON bursar.bursar_config FOR EACH ROW EXECUTE FUNCTION bursar.prevent_bursar_config_payload_mutation();


--
-- Name: credit_ledger_entries credit_ledger_lot_allocation; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER credit_ledger_lot_allocation AFTER INSERT ON bursar.credit_ledger_entries FOR EACH ROW EXECUTE FUNCTION bursar.allocate_ledger_entry_lots();


--
-- Name: credit_ledger_entries credit_ledger_refund_lot_provenance; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER credit_ledger_refund_lot_provenance AFTER INSERT ON bursar.credit_ledger_entries FOR EACH ROW EXECUTE FUNCTION bursar.record_refund_lot_provenance();


--
-- Name: credit_transactions credit_transaction_account_assignment; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE OR REPLACE FUNCTION bursar.assign_reservation_account()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
  IF NEW.account_id IS NULL THEN
    INSERT INTO bursar.credit_accounts (account_type, user_id)
    VALUES ('personal', NEW.user_id)
    ON CONFLICT DO NOTHING;
    SELECT id INTO NEW.account_id
    FROM bursar.credit_accounts
    WHERE account_type = 'personal' AND user_id = NEW.user_id;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER credit_reservation_account_assignment
BEFORE INSERT ON bursar.credit_reservations
FOR EACH ROW EXECUTE FUNCTION bursar.assign_reservation_account();

CREATE TRIGGER credit_transaction_account_assignment BEFORE INSERT ON bursar.credit_transactions FOR EACH ROW EXECUTE FUNCTION bursar.assign_credit_account();


--
-- Name: credit_transactions credit_transaction_ledger_projection; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE CONSTRAINT TRIGGER credit_transaction_ledger_projection AFTER INSERT ON bursar.credit_transactions DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION bursar.project_credit_transaction();


--
-- Name: billing_credit_topups set_billing_credit_topups_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_credit_topups_updated_at BEFORE UPDATE ON bursar.billing_credit_topups FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_customers set_billing_customers_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_customers_updated_at BEFORE UPDATE ON bursar.billing_customers FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_disputes set_billing_disputes_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_disputes_updated_at BEFORE UPDATE ON bursar.billing_disputes FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_events set_billing_events_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_events_updated_at BEFORE UPDATE ON bursar.billing_events FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_invoices set_billing_invoices_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_invoices_updated_at BEFORE UPDATE ON bursar.billing_invoices FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_offers set_billing_offers_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_offers_updated_at BEFORE UPDATE ON bursar.billing_offers FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_payments set_billing_payments_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_payments_updated_at BEFORE UPDATE ON bursar.billing_payments FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_preferences set_billing_preferences_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_preferences_updated_at BEFORE UPDATE ON bursar.billing_preferences FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_provider_refs set_billing_provider_refs_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_provider_refs_updated_at BEFORE UPDATE ON bursar.billing_provider_refs FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_refunds set_billing_refunds_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_refunds_updated_at BEFORE UPDATE ON bursar.billing_refunds FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_subscriptions set_billing_subscriptions_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_subscriptions_updated_at BEFORE UPDATE ON bursar.billing_subscriptions FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: credit_buckets set_credit_buckets_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_credit_buckets_updated_at BEFORE UPDATE ON bursar.credit_buckets FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: user_credit_buckets set_user_credit_buckets_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_user_credit_buckets_updated_at BEFORE UPDATE ON bursar.user_credit_buckets FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: user_credits set_user_credits_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_user_credits_updated_at BEFORE UPDATE ON bursar.user_credits FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--


-- ============================================================================
-- Source: 115_auto_recharge.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS bursar.billing_auto_recharge_profiles (
  user_id uuid PRIMARY KEY,
  enabled boolean NOT NULL DEFAULT false,
  state text NOT NULL DEFAULT 'disabled' CHECK (state IN ('disabled','active','suspended')),
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

CREATE TABLE IF NOT EXISTS bursar.billing_auto_recharge_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid,
  subject_id uuid,
  provider text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  provider_payment_id text,
  topup_key text NOT NULL,
  quantity integer NOT NULL CHECK (quantity > 0),
  trigger_balance numeric(18,4),
  policy_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  policy_hash text,
  quoted_amount_minor bigint,
  final_amount_minor bigint,
  currency text,
  state text NOT NULL DEFAULT 'claimed' CHECK (state IN ('claimed','submitted','processing','unknown','succeeded','failed','action_required')),
  credits numeric(18,4),
  failure_category text,
  failure_code text,
  failure_message text,
  action_url text,
  submitted_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT billing_auto_recharge_attempts_amounts_check CHECK (
    (trigger_balance IS NULL OR bursar.is_finite_numeric(trigger_balance)) AND
    (credits IS NULL OR bursar.is_finite_numeric(credits)) AND
    (quoted_amount_minor IS NULL OR quoted_amount_minor >= 0) AND
    (final_amount_minor IS NULL OR final_amount_minor >= 0)
  ),
  CONSTRAINT billing_auto_recharge_attempts_currency_check CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$')
);

CREATE UNIQUE INDEX IF NOT EXISTS billing_auto_recharge_attempts_active_user_idx
  ON bursar.billing_auto_recharge_attempts (user_id)
  WHERE state IN ('claimed','submitted','processing','unknown','action_required');

CREATE TRIGGER set_billing_auto_recharge_profiles_updated_at
BEFORE UPDATE ON bursar.billing_auto_recharge_profiles
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();
CREATE TRIGGER set_billing_auto_recharge_attempts_updated_at
BEFORE UPDATE ON bursar.billing_auto_recharge_attempts
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

ALTER TABLE bursar.billing_auto_recharge_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.billing_auto_recharge_attempts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Server-only auto recharge profiles" ON bursar.billing_auto_recharge_profiles;
CREATE POLICY "Server-only auto recharge profiles" ON bursar.billing_auto_recharge_profiles USING (false);
DROP POLICY IF EXISTS "Server-only auto recharge attempts" ON bursar.billing_auto_recharge_attempts;
CREATE POLICY "Server-only auto recharge attempts" ON bursar.billing_auto_recharge_attempts USING (false);

CREATE OR REPLACE FUNCTION bursar.claim_auto_recharge_attempt(
  p_user_id uuid, p_provider text, p_topup_key text, p_quantity integer,
  p_window_start timestamptz, p_max_charges integer, p_trigger_balance numeric,
  p_policy_snapshot jsonb, p_policy_hash text, p_quoted_amount_minor bigint, p_currency text
) RETURNS SETOF bursar.billing_auto_recharge_attempts
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_profile bursar.billing_auto_recharge_profiles;
        v_attempt bursar.billing_auto_recharge_attempts;
        v_count integer;
BEGIN
  SELECT * INTO v_profile FROM bursar.billing_auto_recharge_profiles
   WHERE user_id = p_user_id FOR UPDATE;
  IF NOT FOUND OR NOT v_profile.enabled OR v_profile.state <> 'active' OR NOT v_profile.armed THEN RETURN; END IF;
  SELECT * INTO v_attempt FROM bursar.billing_auto_recharge_attempts
   WHERE user_id = p_user_id AND state IN ('claimed','submitted','processing','unknown','action_required')
   ORDER BY created_at DESC LIMIT 1;
  IF FOUND THEN RETURN NEXT v_attempt; RETURN; END IF;
  SELECT count(*) INTO v_count FROM bursar.billing_auto_recharge_attempts
   WHERE user_id = p_user_id
     AND created_at >= p_window_start
     AND state IN ('submitted','processing','succeeded','action_required');
  IF p_max_charges IS NOT NULL AND v_count >= p_max_charges THEN RETURN; END IF;
  INSERT INTO bursar.billing_auto_recharge_attempts
    (user_id, provider, idempotency_key, topup_key, quantity, trigger_balance, policy_snapshot, policy_hash, quoted_amount_minor, currency)
  VALUES (p_user_id, p_provider, 'auto-recharge:' || p_user_id::text || ':' || gen_random_uuid()::text,
          p_topup_key, p_quantity, p_trigger_balance, p_policy_snapshot, p_policy_hash, p_quoted_amount_minor, p_currency)
  RETURNING * INTO v_attempt;
  UPDATE bursar.billing_auto_recharge_profiles SET armed = false, updated_at = now() WHERE user_id = p_user_id;
  RETURN NEXT v_attempt;
END; $$;

REVOKE ALL ON FUNCTION bursar.claim_auto_recharge_attempt(uuid,text,text,integer,timestamptz,integer,numeric,jsonb,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bursar.claim_auto_recharge_attempt(uuid,text,text,integer,timestamptz,integer,numeric,jsonb,text,bigint,text) TO service_role;

REVOKE ALL ON TABLE bursar.billing_auto_recharge_profiles, bursar.billing_auto_recharge_attempts FROM PUBLIC, anon, authenticated;


-- ============================================================================
-- Source: 116_subscription_lifecycle.sql
-- ============================================================================

-- Durable, provider-neutral state for customer initiated subscription changes.
ALTER TABLE bursar.billing_subscriptions
  ADD COLUMN IF NOT EXISTS grace_ends_at timestamptz;

CREATE TABLE IF NOT EXISTS bursar.billing_subscription_changes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid,
  subject_id uuid,
  provider text NOT NULL, provider_subscription_id text NOT NULL,
  from_plan text, from_interval text, to_plan text NOT NULL, to_interval text NOT NULL,
  effective_at text NOT NULL CHECK (effective_at IN ('immediately', 'next_billing_date', 'trial_end')),
  state text NOT NULL CHECK (state IN ('draft', 'awaiting_payment', 'scheduled', 'completed', 'failed', 'canceled', 'superseded')),
  proration_billing_mode text NOT NULL, quote jsonb NOT NULL DEFAULT '{}'::jsonb, quote_hash text NOT NULL,
  provider_operation_id text, provider_payment_id text, failure_code text, failure_message text,
  effective_date timestamptz, expires_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
  CONSTRAINT billing_subscription_changes_subject_check CHECK (num_nonnulls(user_id, subject_id) = 1),
  CONSTRAINT billing_subscription_changes_dates_check CHECK (expires_at IS NULL OR effective_date IS NULL OR expires_at >= effective_date),
  CONSTRAINT billing_subscription_changes_completion_check CHECK ((state = 'completed') = (completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS billing_subscription_changes_one_open_idx ON bursar.billing_subscription_changes (provider, provider_subscription_id) WHERE state IN ('awaiting_payment', 'scheduled');
CREATE INDEX IF NOT EXISTS billing_subscription_changes_user_idx ON bursar.billing_subscription_changes (user_id, created_at DESC);
CREATE TRIGGER set_billing_subscription_changes_updated_at
BEFORE UPDATE ON bursar.billing_subscription_changes
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();
ALTER TABLE bursar.billing_subscription_changes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Server-only subscription changes" ON bursar.billing_subscription_changes;
CREATE POLICY "Server-only subscription changes" ON bursar.billing_subscription_changes USING (false);
REVOKE ALL ON TABLE bursar.billing_subscription_changes FROM PUBLIC, anon, authenticated;

