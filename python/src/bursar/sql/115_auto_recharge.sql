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
