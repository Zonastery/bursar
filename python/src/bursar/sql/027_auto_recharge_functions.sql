CREATE FUNCTION bursar.claim_auto_recharge_attempt(
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
