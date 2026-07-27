-- Regression coverage for the irreversible data-model decisions in the clean-slate schema.
DO $$
DECLARE
    v_revision uuid;
    v_subject uuid := '00000000-0000-0000-0000-000000000123';
    v_entry uuid;
BEGIN
    SELECT revision_id INTO v_revision FROM bursar.publish_and_activate_catalog(
        1,
        '{
          "version": 1,
          "usage": {"operations": {"chat": {"measures": ["tokens"], "dimensions": []}}, "rate_cards": {"standard": {"prices": {"chat": [{"default": true, "formula": "tokens"}]}}}},
          "credits": {
            "buckets": {"expiring": {"expires_after": {"unit": "month", "count": 2, "anchor": "rolling", "timezone": "Asia/Kolkata"}}},
            "spend_order": ["expiring"], "default_bucket": "expiring"
          },
          "plans": {"p": {"display_name": "P", "rate_card": "standard", "included_credits": {"amount": 3, "reset": {"unit": "month", "count": 1, "anchor": "rolling", "timezone": "Asia/Kolkata"}}}},
          "payments": {
            "subscriptions": {"o": {"plan": "p", "billing_period": {"unit": "year", "count": 1, "anchor": "calendar", "timezone": "Europe/London"}, "providers": {"stripe": {"lookup": {"type": "price_id", "value": "price_1"}}}}},
            "topups": {"pack": {"credits": 10, "bucket": "expiring", "providers": {"stripe": {"lookup": {"type": "custom_provider_key", "value": "pack_1"}}}}},
            "auto_recharge": {"trigger": {"balance_below": 2}, "purchase": {"topup": "pack", "quantity": 2}, "limit": {"max_purchases": 3, "period": {"unit": "month", "count": 1, "anchor": "calendar", "timezone": "UTC"}}}
          }
        }'::jsonb,
        'regression'
    );
    IF NOT EXISTS (SELECT 1 FROM bursar.catalog_buckets WHERE catalog_revision_id=v_revision AND expires_after_unit='month' AND expires_after_count=2 AND expires_after_timezone='Asia/Kolkata') THEN
        RAISE EXCEPTION 'raw YAML bucket period was not projected';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM bursar.catalog_plans WHERE catalog_revision_id=v_revision AND included_credits_reset_anchor='rolling') THEN
        RAISE EXCEPTION 'included-credit period was not projected';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM bursar.catalog_provider_refs WHERE catalog_revision_id=v_revision AND lookup_type='custom_provider_key') THEN
        RAISE EXCEPTION 'arbitrary provider lookup type was rejected';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM bursar.catalog_auto_recharge_policies WHERE catalog_revision_id=v_revision AND quantity=2 AND max_purchases=3) THEN
        RAISE EXCEPTION 'auto-recharge policy was not projected';
    END IF;

    SELECT entry_id INTO v_entry FROM bursar.post_credit(v_subject,'grant',5,'grant','digest-1','{"source":"a"}'::jsonb,'expiring',v_revision);
    IF v_entry IS NULL OR (SELECT expires_at FROM bursar.credit_lots WHERE source_entry_id=v_entry) IS NULL THEN
        RAISE EXCEPTION 'rolling bucket expiry was not derived';
    END IF;
    IF (SELECT error_code FROM bursar.post_credit(v_subject,'grant',5,'grant','digest-1','{"source":"b"}'::jsonb,'expiring',v_revision)) <> 'idempotency_conflict' THEN
        RAISE EXCEPTION 'request digest conflict was not detected';
    END IF;
END $$;

DO $$
DECLARE
    v_subject uuid := '00000000-0000-0000-0000-000000000555';
    v_plan uuid;
    v_result record;
BEGIN
    PERFORM bursar.publish_and_activate_catalog(
        1,
        '{"version":1,"credits":{"buckets":{"default":{}},"spend_order":["default"],"default_bucket":"default"},"plans":{"metered":{"display_name":"Metered","included_credits":{"amount":3,"reset":{"unit":"month","count":1,"anchor":"calendar","timezone":"UTC"}},"limits":{"chat":{"max_calls":1,"period":{"unit":"month","count":1,"anchor":"calendar","timezone":"UTC"},"action":"deny"}}}},"payments":{"subscriptions":{},"topups":{}}}'::jsonb,
        'atomic-policy'
    );
    SELECT id INTO v_plan FROM bursar.catalog_plans WHERE plan_key='metered' AND catalog_revision_id=(SELECT id FROM bursar.catalog_revisions WHERE status='active');
    PERFORM bursar.assign_plan(v_subject,v_plan,now(),NULL);
    SELECT * INTO v_result FROM bursar.charge_usage_for_operation(v_subject,'chat',2,'policy-charge-1','chat');
    IF v_result.allowance_covered <> 2 OR v_result.error_code IS NOT NULL THEN
        RAISE EXCEPTION 'included allowance was not consumed atomically';
    END IF;
    SELECT * INTO v_result FROM bursar.charge_usage_for_operation(v_subject,'chat',1,'policy-charge-2','chat');
    IF v_result.error_code <> 'feature_limit_reached' THEN
        RAISE EXCEPTION 'feature limit was not enforced atomically';
    END IF;
END $$;
