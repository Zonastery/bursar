-- Public v1 catalog contract.  The persisted document is the YAML/SDK shape;
-- this helper adapts it only while populating the pre-existing operational
-- tables, which are intentionally migrated separately from catalog storage.

CREATE OR REPLACE FUNCTION bursar.internal_catalog_config_from_public(p_config jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path TO ''
AS $$
WITH buckets AS (
  SELECT COALESCE(jsonb_object_agg(key,
    jsonb_build_object(
      'label', key,
      'priority', ordinality - 1,
      'default', key = p_config#>>'{credits,default_bucket}',
      'expires', value ? 'expires_after',
      'ttl_days', CASE value#>>'{expires_after,unit}'
        WHEN 'day' THEN COALESCE((value#>>'{expires_after,count}')::int, 0)
        WHEN 'week' THEN COALESCE((value#>>'{expires_after,count}')::int, 0) * 7
        WHEN 'month' THEN COALESCE((value#>>'{expires_after,count}')::int, 0) * 30
        WHEN 'year' THEN COALESCE((value#>>'{expires_after,count}')::int, 0) * 365
      END)), '{}'::jsonb) AS value
  FROM unnest(COALESCE(ARRAY(SELECT jsonb_array_elements_text(p_config#>'{credits,spend_order}')), ARRAY[]::text[]))
       WITH ORDINALITY AS t(key, ordinality)
  CROSS JOIN LATERAL (SELECT p_config#>ARRAY['credits', 'buckets', key] AS value) AS bucket
), plans AS (
  SELECT COALESCE(jsonb_object_agg(key, jsonb_build_object(
    'label', value->>'display_name',
    'allowance', CASE WHEN value ? 'included_credits' THEN jsonb_build_object(
      'amount', value#>>'{included_credits,amount}',
      'period', CASE value#>>'{included_credits,reset,anchor}' WHEN 'rolling' THEN 'rolling_30d' WHEN 'plan_assignment' THEN 'anniversary' ELSE 'calendar_month' END) END,
    'rate_overrides', '{}'::jsonb,
    'entitlements', COALESCE(value->'features', '{}'::jsonb),
    'safety', jsonb_build_object(
      'billing_mode', COALESCE(value#>>'{spending,mode}', 'strict'),
      'max_concurrent', value#>>'{spending,max_concurrent}',
      'overdraft_floor', CASE WHEN value#>>'{spending,overdraft_limit}' IS NULL THEN NULL ELSE -((value#>>'{spending,overdraft_limit}')::numeric) END,
      'per_operation', COALESCE(value#>'{spending,operations}', '{}'::jsonb)))), '{}'::jsonb) AS value
  FROM jsonb_each(COALESCE(p_config->'plans', '{}'::jsonb))
), subscriptions AS (
  SELECT COALESCE(jsonb_object_agg(key, jsonb_build_object(
    'plan', value->>'plan',
    'interval', value#>>'{billing_period,unit}',
    'interval_count', COALESCE((value#>>'{billing_period,count}')::int, 1),
    'grant', CASE WHEN value ? 'renewal_credits' THEN jsonb_build_object('mode','cycle_grant','credits',value#>>'{renewal_credits,amount}','bucket',value#>>'{renewal_credits,bucket}','replace_prior',(value#>>'{renewal_credits,behavior}') = 'replace') ELSE jsonb_build_object('mode','allowance') END,
    'providers', COALESCE((SELECT jsonb_object_agg(provider, jsonb_build_object(ref#>>'{lookup,type}', ref#>>'{lookup,value}')) FROM jsonb_each(value->'providers') AS p(provider, ref)), '{}'::jsonb))), '{}'::jsonb) AS value
  FROM jsonb_each(COALESCE(p_config#>'{payments,subscriptions}', '{}'::jsonb))
), topups AS (
  SELECT COALESCE(jsonb_object_agg(key, jsonb_build_object(
    'deposit_to', value->>'bucket', 'credits_per_unit', value->>'credits',
    'providers', COALESCE((SELECT jsonb_object_agg(provider, jsonb_build_object(ref#>>'{lookup,type}', ref#>>'{lookup,value}')) FROM jsonb_each(value->'providers') AS p(provider, ref)), '{}'::jsonb))), '{}'::jsonb) AS value
  FROM jsonb_each(COALESCE(p_config#>'{payments,topups}', '{}'::jsonb))
)
SELECT jsonb_build_object(
  'version', 1,
  'metering', jsonb_build_object('models', jsonb_build_object('*','0')),
  'ledger', jsonb_build_object('min_balance','0','buckets',(SELECT value FROM buckets),'signup_grant',p_config#>'{credits,signup_grant}'),
  'plans', (SELECT value FROM plans),
  'billing', jsonb_build_object('currency','USD','subscriptions',(SELECT value FROM subscriptions),'topups',(SELECT value FROM topups)));
$$;

CREATE OR REPLACE FUNCTION bursar.validate_bursar_config(p_config jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
  IF p_config IS NULL OR jsonb_typeof(p_config) <> 'object' OR p_config->>'version' <> '1' THEN
    RAISE EXCEPTION 'bursar config must be a version 1 object' USING ERRCODE='22023';
  END IF;
  IF p_config ? 'metering' OR p_config ? 'ledger' OR p_config ? 'billing' OR p_config ? '_bursar_public_config' THEN
    RAISE EXCEPTION 'legacy or private catalog sections are not allowed' USING ERRCODE='22023';
  END IF;
  IF p_config ? 'usage' AND (jsonb_typeof(p_config#>'{usage,operations}') <> 'object' OR jsonb_typeof(p_config#>'{usage,rate_cards}') <> 'object') THEN
    RAISE EXCEPTION 'usage.operations and usage.rate_cards must be objects' USING ERRCODE='22023';
  END IF;
  IF p_config ? 'credits' AND jsonb_typeof(p_config#>'{credits,buckets}') <> 'object' THEN
    RAISE EXCEPTION 'credits.buckets must be an object' USING ERRCODE='22023';
  END IF;
  IF p_config ? 'plans' AND jsonb_typeof(p_config->'plans') <> 'object' THEN
    RAISE EXCEPTION 'plans must be an object' USING ERRCODE='22023';
  END IF;
END;
$$;
