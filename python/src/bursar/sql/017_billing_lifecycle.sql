-- bursar: billing lifecycle — offer archiving, provider ref soft-delete,
-- lookup-key resolution, and scheduled validity windows.
--
-- Phases 2 + 3 + 5 consolidated:
--   - billing_offers: status (active/archived), valid_from, valid_to
--   - billing_credit_topups: status (active/archived)
--   - billing_provider_refs: active (soft-delete), updated_at
--   - sync_billing_from_config: archive absent offers, soft-deactivate stale refs
--   - resolve_billing_offer_by_price: filter by active/status/validity
--   - resolve_credit_topup_by_price: filter by active/status
--   - NEW resolve_billing_offer_by_lookup
--   - NEW resolve_credit_topup_by_lookup

-- ── Schema: lifecycle columns on billing_offers ─────────────────────────

ALTER TABLE public.billing_offers ADD COLUMN IF NOT EXISTS status TEXT
    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived'));
ALTER TABLE public.billing_offers ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE public.billing_offers ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;


-- ── Schema: lifecycle columns on billing_credit_topups ──────────────────

ALTER TABLE public.billing_credit_topups ADD COLUMN IF NOT EXISTS status TEXT
    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived'));


-- ── Schema: soft-delete columns on billing_provider_refs ─────────────────

ALTER TABLE public.billing_provider_refs ADD COLUMN IF NOT EXISTS active BOOLEAN
    NOT NULL DEFAULT true;
ALTER TABLE public.billing_provider_refs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
    NOT NULL DEFAULT now();

-- Add updated_at trigger for billing_provider_refs
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_provider_refs_updated_at'
        AND tgrelid = 'public.billing_provider_refs'::regclass
    ) THEN
        CREATE TRIGGER set_billing_provider_refs_updated_at
            BEFORE UPDATE ON public.billing_provider_refs
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;


-- ── Drop old sync_billing_from_config and both resolve RPCs ─────────────

DO $$ DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname IN ('sync_billing_from_config', 'resolve_billing_offer_by_price',
                          'resolve_credit_topup_by_price')
          AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;


-- ── sync_billing_from_config — soft-delete + upsert lifecycle ───────────
--
-- Instead of DELETE + re-INSERT (which hard-deleted refs for removed offers,
-- breaking webhook resolution during provider migrations), this version:
--   1. Archives offers not present in the config (status = 'archived')
--   2. Soft-deactivates ALL provider refs for the resource_type
--   3. UPSERTs/inserts new refs from the config (active = true)
--
-- Rows are never deleted — only soft-deactivated. Old webhooks for removed
-- offers/topups still resolve (the offer is archived, not deleted; refs are
-- deactivated, not removed).

CREATE OR REPLACE FUNCTION public.sync_billing_from_config(p_config JSONB)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_key TEXT;
    v_item JSONB;
    v_ref JSONB;
    v_provider TEXT;
    v_ref_id UUID;
    v_price_id TEXT;
    v_product_id TEXT;
    v_variant_id TEXT;
    v_lookup_key TEXT;
    v_config_keys TEXT[];
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN;
    END IF;

    -- ── Sync billing offers ─────────────────────────────────────────────
    IF p_config ? 'subscriptions' AND jsonb_typeof(p_config->'subscriptions') = 'object' THEN
        -- Collect config offer keys for archiving absent offers
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(p_config->'subscriptions') k;

        -- Archive offers not in config
        IF v_config_keys IS NOT NULL THEN
            UPDATE public.billing_offers SET status = 'archived', updated_at = now()
            WHERE offer_key != ALL(v_config_keys)
              AND status = 'active';
        END IF;

        -- Soft-deactivate all existing offer refs (will be re-activated below)
        UPDATE public.billing_provider_refs SET active = false, updated_at = now()
        WHERE resource_type = 'offer';

        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'subscriptions')
        LOOP
            -- Upsert offer (activate/reactivate)
            INSERT INTO public.billing_offers (
                offer_key, plan, interval, interval_count,
                grant_mode, grant_credits, grant_bucket, grant_replace_prior
            )
            VALUES (
                v_key,
                v_item->>'plan',
                COALESCE(v_item->>'interval', 'month'),
                COALESCE((v_item->>'interval_count')::INTEGER, 1),
                COALESCE(v_item#>>'{grant,mode}', 'allowance'),
                (v_item#>>'{grant,credits}')::INTEGER,
                v_item#>>'{grant,bucket}',
                COALESCE((v_item#>>'{grant,replace_prior}')::BOOLEAN, true)
            )
            ON CONFLICT (offer_key) DO UPDATE SET
                plan = EXCLUDED.plan,
                interval = EXCLUDED.interval,
                interval_count = EXCLUDED.interval_count,
                grant_mode = EXCLUDED.grant_mode,
                grant_credits = EXCLUDED.grant_credits,
                grant_bucket = EXCLUDED.grant_bucket,
                grant_replace_prior = EXCLUDED.grant_replace_prior,
                status = 'active',
                updated_at = now();

            -- Sync provider refs for this offer
            IF v_item ? 'providers' AND jsonb_typeof(v_item->'providers') = 'object' THEN
                FOR v_provider, v_ref IN SELECT * FROM jsonb_each(v_item->'providers')
                LOOP
                    v_price_id := v_ref->>'price_id';
                    v_product_id := v_ref->>'product_id';
                    v_variant_id := v_ref->>'variant_id';
                    v_lookup_key := v_ref->>'lookup_key';

                    -- Try to find existing ref by any unique identifier
                    SELECT id INTO v_ref_id FROM public.billing_provider_refs
                    WHERE provider = v_provider AND resource_type = 'offer'
                    AND (
                        (v_price_id IS NOT NULL AND price_id = v_price_id)
                        OR (v_product_id IS NOT NULL AND product_id = v_product_id)
                        OR (v_lookup_key IS NOT NULL AND lookup_key = v_lookup_key)
                    )
                    ORDER BY updated_at DESC
                    LIMIT 1;

                    IF v_ref_id IS NOT NULL THEN
                        UPDATE public.billing_provider_refs SET
                            price_id = COALESCE(v_price_id, price_id),
                            product_id = COALESCE(v_product_id, product_id),
                            variant_id = COALESCE(v_variant_id, variant_id),
                            lookup_key = COALESCE(v_lookup_key, lookup_key),
                            resource_key = v_key,
                            active = true,
                            updated_at = now()
                        WHERE id = v_ref_id;
                    ELSE
                        INSERT INTO public.billing_provider_refs (
                            provider, price_id, product_id, variant_id,
                            lookup_key, resource_type, resource_key, active
                        ) VALUES (
                            v_provider, v_price_id, v_product_id, v_variant_id,
                            v_lookup_key, 'offer', v_key, true
                        );
                    END IF;
                END LOOP;
            END IF;
        END LOOP;
    END IF;

    -- ── Sync credit topups ──────────────────────────────────────────────
    IF p_config ? 'topups' AND jsonb_typeof(p_config->'topups') = 'object' THEN
        -- Collect config topup keys for archiving absent topups
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(p_config->'topups') k;

        -- Archive topups not in config
        IF v_config_keys IS NOT NULL THEN
            UPDATE public.billing_credit_topups SET status = 'archived', updated_at = now()
            WHERE topup_key != ALL(v_config_keys)
              AND status = 'active';
        END IF;

        -- Soft-deactivate all existing topup refs
        UPDATE public.billing_provider_refs SET active = false, updated_at = now()
        WHERE resource_type = 'topup';

        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'topups')
        LOOP
            -- Upsert topup (activate/reactivate)
            INSERT INTO public.billing_credit_topups (
                topup_key, deposit_to, credits_per_unit,
                min_amount_minor, max_amount_minor, tax_behavior
            )
            VALUES (
                v_key,
                COALESCE(v_item->>'deposit_to', 'purchased'),
                COALESCE((v_item->>'credits_per_unit')::INTEGER, 1000),
                COALESCE((v_item->>'min_amount_minor')::INTEGER, 500),
                COALESCE((v_item->>'max_amount_minor')::INTEGER, 500000),
                COALESCE(v_item->>'tax_behavior', 'exclude_tax')
            )
            ON CONFLICT (topup_key) DO UPDATE SET
                deposit_to = EXCLUDED.deposit_to,
                credits_per_unit = EXCLUDED.credits_per_unit,
                min_amount_minor = EXCLUDED.min_amount_minor,
                max_amount_minor = EXCLUDED.max_amount_minor,
                tax_behavior = EXCLUDED.tax_behavior,
                status = 'active',
                updated_at = now();

            -- Sync provider refs for this topup
            IF v_item ? 'providers' AND jsonb_typeof(v_item->'providers') = 'object' THEN
                FOR v_provider, v_ref IN SELECT * FROM jsonb_each(v_item->'providers')
                LOOP
                    v_price_id := v_ref->>'price_id';
                    v_product_id := v_ref->>'product_id';
                    v_variant_id := v_ref->>'variant_id';
                    v_lookup_key := v_ref->>'lookup_key';

                    SELECT id INTO v_ref_id FROM public.billing_provider_refs
                    WHERE provider = v_provider AND resource_type = 'topup'
                    AND (
                        (v_price_id IS NOT NULL AND price_id = v_price_id)
                        OR (v_product_id IS NOT NULL AND product_id = v_product_id)
                        OR (v_lookup_key IS NOT NULL AND lookup_key = v_lookup_key)
                    )
                    ORDER BY updated_at DESC
                    LIMIT 1;

                    IF v_ref_id IS NOT NULL THEN
                        UPDATE public.billing_provider_refs SET
                            price_id = COALESCE(v_price_id, price_id),
                            product_id = COALESCE(v_product_id, product_id),
                            variant_id = COALESCE(v_variant_id, variant_id),
                            lookup_key = COALESCE(v_lookup_key, lookup_key),
                            resource_key = v_key,
                            active = true,
                            updated_at = now()
                        WHERE id = v_ref_id;
                    ELSE
                        INSERT INTO public.billing_provider_refs (
                            provider, price_id, product_id, variant_id,
                            lookup_key, resource_type, resource_key, active
                        ) VALUES (
                            v_provider, v_price_id, v_product_id, v_variant_id,
                            v_lookup_key, 'topup', v_key, true
                        );
                    END IF;
                END LOOP;
            END IF;
        END LOOP;
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sync_billing_from_config(JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sync_billing_from_config(JSONB) TO service_role;


-- ── resolve_billing_offer_by_price — filter by active/status/validity ───

CREATE OR REPLACE FUNCTION public.resolve_billing_offer_by_price(
    p_provider TEXT,
    p_price_id TEXT DEFAULT NULL,
    p_product_id TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_ref RECORD;
    v_offer RECORD;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    IF p_price_id IS NULL AND p_product_id IS NULL THEN
        RETURN NULL;
    END IF;

    IF p_price_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM public.billing_provider_refs
        WHERE provider = p_provider AND price_id = p_price_id
          AND resource_type = 'offer' AND active = true
        LIMIT 1;
    ELSIF p_product_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM public.billing_provider_refs
        WHERE provider = p_provider AND product_id = p_product_id
          AND resource_type = 'offer' AND active = true
        LIMIT 1;
    END IF;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_offer
    FROM public.billing_offers
    WHERE offer_key = v_ref.resource_key
      AND status = 'active'
      AND (valid_from IS NULL OR valid_from <= now())
      AND (valid_to IS NULL OR valid_to > now());

    IF v_offer.offer_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'offer_key', v_offer.offer_key,
        'plan', v_offer.plan,
        'interval', v_offer.interval,
        'interval_count', v_offer.interval_count,
        'grant_mode', v_offer.grant_mode,
        'grant_credits', v_offer.grant_credits,
        'grant_bucket', v_offer.grant_bucket,
        'grant_replace_prior', v_offer.grant_replace_prior
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.resolve_billing_offer_by_price(TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_billing_offer_by_price(TEXT, TEXT, TEXT) TO service_role;


-- ── resolve_credit_topup_by_price — filter by active/status ─────────────

CREATE OR REPLACE FUNCTION public.resolve_credit_topup_by_price(
    p_provider TEXT,
    p_price_id TEXT DEFAULT NULL,
    p_product_id TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_ref RECORD;
    v_topup RECORD;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    IF p_price_id IS NULL AND p_product_id IS NULL THEN
        RETURN NULL;
    END IF;

    IF p_price_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM public.billing_provider_refs
        WHERE provider = p_provider AND price_id = p_price_id
          AND resource_type = 'topup' AND active = true
        LIMIT 1;
    ELSIF p_product_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM public.billing_provider_refs
        WHERE provider = p_provider AND product_id = p_product_id
          AND resource_type = 'topup' AND active = true
        LIMIT 1;
    END IF;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_topup
    FROM public.billing_credit_topups
    WHERE topup_key = v_ref.resource_key
      AND status = 'active';

    IF v_topup.topup_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'topup_key', v_topup.topup_key,
        'deposit_to', v_topup.deposit_to,
        'credits_per_unit', v_topup.credits_per_unit,
        'min_amount_minor', v_topup.min_amount_minor,
        'max_amount_minor', v_topup.max_amount_minor,
        'tax_behavior', v_topup.tax_behavior
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.resolve_credit_topup_by_price(TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_credit_topup_by_price(TEXT, TEXT, TEXT) TO service_role;


-- ── resolve_billing_offer_by_lookup — resolve by lookup_key (Phase 5) ───

CREATE OR REPLACE FUNCTION public.resolve_billing_offer_by_lookup(
    p_provider TEXT,
    p_lookup_key TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_ref RECORD;
    v_offer RECORD;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    IF p_lookup_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_ref
    FROM public.billing_provider_refs
    WHERE provider = p_provider AND lookup_key = p_lookup_key
      AND resource_type = 'offer' AND active = true
    LIMIT 1;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_offer
    FROM public.billing_offers
    WHERE offer_key = v_ref.resource_key
      AND status = 'active'
      AND (valid_from IS NULL OR valid_from <= now())
      AND (valid_to IS NULL OR valid_to > now());

    IF v_offer.offer_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'offer_key', v_offer.offer_key,
        'plan', v_offer.plan,
        'interval', v_offer.interval,
        'interval_count', v_offer.interval_count,
        'grant_mode', v_offer.grant_mode,
        'grant_credits', v_offer.grant_credits,
        'grant_bucket', v_offer.grant_bucket,
        'grant_replace_prior', v_offer.grant_replace_prior
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.resolve_billing_offer_by_lookup(TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_billing_offer_by_lookup(TEXT, TEXT) TO service_role;


-- ── resolve_credit_topup_by_lookup — resolve by lookup_key (Phase 5) ────

CREATE OR REPLACE FUNCTION public.resolve_credit_topup_by_lookup(
    p_provider TEXT,
    p_lookup_key TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_ref RECORD;
    v_topup RECORD;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    IF p_lookup_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_ref
    FROM public.billing_provider_refs
    WHERE provider = p_provider AND lookup_key = p_lookup_key
      AND resource_type = 'topup' AND active = true
    LIMIT 1;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_topup
    FROM public.billing_credit_topups
    WHERE topup_key = v_ref.resource_key
      AND status = 'active';

    IF v_topup.topup_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'topup_key', v_topup.topup_key,
        'deposit_to', v_topup.deposit_to,
        'credits_per_unit', v_topup.credits_per_unit,
        'min_amount_minor', v_topup.min_amount_minor,
        'max_amount_minor', v_topup.max_amount_minor,
        'tax_behavior', v_topup.tax_behavior
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.resolve_credit_topup_by_lookup(TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_credit_topup_by_lookup(TEXT, TEXT) TO service_role;

NOTIFY pgrst, 'reload schema';
