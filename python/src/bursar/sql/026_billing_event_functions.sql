






















































CREATE FUNCTION bursar.claim_billing_event(p_provider text, p_event_id text, p_event_type text, p_envelope jsonb DEFAULT '{}'::jsonb, p_lease_seconds integer DEFAULT 300, p_attempt_limit integer DEFAULT 3) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_event bursar.billing_events;
  v_token uuid := gen_random_uuid();
BEGIN
  INSERT INTO bursar.billing_events(provider, provider_event_id, event_type, status, envelope, claim_token, claim_expires_at)
  VALUES (p_provider, p_event_id, p_event_type, 'processing',
          jsonb_strip_nulls(jsonb_build_object('id', p_envelope->>'id', 'type', p_envelope->>'type', 'created_at', p_envelope->>'created_at')),
          v_token, now() + make_interval(secs => greatest(p_lease_seconds, 1)))
  ON CONFLICT (provider, provider_event_id) DO NOTHING
  RETURNING * INTO v_event;
  IF v_event.id IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'claimed', 'event_id', v_event.id, 'claim_token', v_token);
  END IF;
  SELECT * INTO v_event FROM bursar.billing_events
  WHERE provider = p_provider AND provider_event_id = p_event_id FOR UPDATE;
  IF v_event.status = 'completed' THEN RETURN jsonb_build_object('status', 'duplicate'); END IF;
  IF v_event.retry_count >= greatest(p_attempt_limit, 1) THEN RETURN jsonb_build_object('status', 'max_retries_exceeded'); END IF;
  IF v_event.status = 'processing' AND v_event.claim_expires_at >= now() THEN RETURN jsonb_build_object('status', 'retry'); END IF;
  UPDATE bursar.billing_events
  SET status = 'processing', retry_count = retry_count + 1, claim_token = v_token,
      claim_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 1)), updated_at = now()
  WHERE id = v_event.id;
  RETURN jsonb_build_object('status', 'claimed', 'event_id', v_event.id, 'claim_token', v_token);
END
$$;



CREATE FUNCTION bursar.complete_billing_event(p_provider text, p_event_id text, p_claim_token uuid) RETURNS boolean
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO ''
    AS $$
  UPDATE bursar.billing_events SET status = 'completed', claim_token = NULL, claim_expires_at = NULL, updated_at = now()
  WHERE provider = p_provider AND provider_event_id = p_event_id AND status = 'processing'
    AND claim_token = p_claim_token AND claim_expires_at >= now()
  RETURNING true























CREATE FUNCTION bursar.deactivate_other_provider_subscriptions(p_user_id uuid, p_keep_provider text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE bursar.billing_subscriptions
    SET status = 'canceled',
        cancel_at_period_end = true,
        updated_at = now()
    WHERE user_id = p_user_id
      AND provider != p_keep_provider
      AND status IN ('active', 'trialing');

    GET DIAGNOSTICS v_count = ROW_COUNT;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'keep_provider', p_keep_provider,
        'deactivated_count', v_count











END;
$$;
