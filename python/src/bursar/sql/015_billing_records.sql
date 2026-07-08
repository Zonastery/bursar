-- bursar: billing records — disputes, payment/refund/invoice RPCs.
--
-- Provides upsert RPCs for the billing payment/refund/invoice lifecycle
-- and a disputes table for tracking provider-side disputes.

-- ── billing_disputes table ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_disputes (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_dispute_id TEXT NOT NULL,
    provider_payment_id TEXT,
    user_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_response',
    reason TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_disputes_provider ON public.billing_disputes (provider, provider_dispute_id);
CREATE INDEX IF NOT EXISTS idx_billing_disputes_user ON public.billing_disputes (user_id);

ALTER TABLE public.billing_disputes ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only billing_disputes' AND tablename = 'billing_disputes' AND schemaname = 'public') THEN
        CREATE POLICY "Server-only billing_disputes" ON public.billing_disputes USING (false);
    END IF;
END; $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_disputes_updated_at'
        AND tgrelid = 'public.billing_disputes'::regclass
    ) THEN
        CREATE TRIGGER set_billing_disputes_updated_at
            BEFORE UPDATE ON public.billing_disputes
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END; $$;

-- ── RPC: upsert_billing_payment ───────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.upsert_billing_payment(
    p_provider TEXT,
    p_provider_payment_id TEXT,
    p_provider_invoice_id TEXT,
    p_user_id UUID,
    p_amount_minor INTEGER,
    p_tax_minor INTEGER,
    p_currency TEXT,
    p_purpose TEXT,
    p_metadata JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    INSERT INTO public.billing_payments (
        provider, provider_payment_id, provider_invoice_id, user_id,
        amount_minor, tax_minor, currency, purpose, metadata
    )
    VALUES (
        p_provider, p_provider_payment_id, p_provider_invoice_id, p_user_id,
        p_amount_minor, p_tax_minor, p_currency, p_purpose,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (provider, provider_payment_id) DO UPDATE SET
        provider_invoice_id = EXCLUDED.provider_invoice_id,
        amount_minor = EXCLUDED.amount_minor,
        tax_minor = EXCLUDED.tax_minor,
        currency = EXCLUDED.currency,
        purpose = EXCLUDED.purpose,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('id', v_id, 'provider_payment_id', p_provider_payment_id);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_billing_payment(TEXT, TEXT, TEXT, UUID, INTEGER, INTEGER, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;

-- ── RPC: upsert_billing_refund ────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.upsert_billing_refund(
    p_provider TEXT,
    p_provider_refund_id TEXT,
    p_provider_payment_id TEXT,
    p_user_id UUID,
    p_amount_minor INTEGER,
    p_currency TEXT,
    p_reason TEXT,
    p_metadata JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    INSERT INTO public.billing_refunds (
        provider, provider_refund_id, provider_payment_id, user_id,
        amount_minor, currency, reason, metadata
    )
    VALUES (
        p_provider, p_provider_refund_id, p_provider_payment_id, p_user_id,
        p_amount_minor, p_currency, p_reason,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (provider, provider_refund_id) DO UPDATE SET
        provider_payment_id = EXCLUDED.provider_payment_id,
        amount_minor = EXCLUDED.amount_minor,
        currency = EXCLUDED.currency,
        reason = EXCLUDED.reason,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('id', v_id, 'provider_refund_id', p_provider_refund_id);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_billing_refund(TEXT, TEXT, TEXT, UUID, INTEGER, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;

-- ── RPC: upsert_billing_invoice ───────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.upsert_billing_invoice(
    p_provider TEXT,
    p_provider_invoice_id TEXT,
    p_provider_subscription_id TEXT,
    p_user_id UUID,
    p_status TEXT,
    p_amount_paid_minor INTEGER,
    p_amount_due_minor INTEGER,
    p_currency TEXT,
    p_period_start TIMESTAMPTZ,
    p_period_end TIMESTAMPTZ,
    p_metadata JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    INSERT INTO public.billing_invoices (
        provider, provider_invoice_id, provider_subscription_id, user_id,
        status, amount_paid_minor, amount_due_minor, currency,
        period_start, period_end, metadata
    )
    VALUES (
        p_provider, p_provider_invoice_id, p_provider_subscription_id, p_user_id,
        p_status, p_amount_paid_minor, p_amount_due_minor, p_currency,
        p_period_start, p_period_end,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (provider, provider_invoice_id) DO UPDATE SET
        provider_subscription_id = EXCLUDED.provider_subscription_id,
        status = EXCLUDED.status,
        amount_paid_minor = EXCLUDED.amount_paid_minor,
        amount_due_minor = EXCLUDED.amount_due_minor,
        currency = EXCLUDED.currency,
        period_start = EXCLUDED.period_start,
        period_end = EXCLUDED.period_end,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('id', v_id, 'provider_invoice_id', p_provider_invoice_id);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_billing_invoice(TEXT, TEXT, TEXT, UUID, TEXT, INTEGER, INTEGER, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, JSONB) FROM PUBLIC, anon, authenticated;

-- ── RPC: upsert_billing_dispute ───────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.upsert_billing_dispute(
    p_provider TEXT,
    p_provider_dispute_id TEXT,
    p_provider_payment_id TEXT,
    p_user_id UUID,
    p_status TEXT,
    p_reason TEXT,
    p_metadata JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    INSERT INTO public.billing_disputes (
        provider, provider_dispute_id, provider_payment_id, user_id,
        status, reason, metadata
    )
    VALUES (
        p_provider, p_provider_dispute_id, p_provider_payment_id, p_user_id,
        p_status, p_reason,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (provider, provider_dispute_id) DO UPDATE SET
        provider_payment_id = EXCLUDED.provider_payment_id,
        status = EXCLUDED.status,
        reason = EXCLUDED.reason,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    RETURNING id INTO v_id;

    RETURN jsonb_build_object('id', v_id, 'provider_dispute_id', p_provider_dispute_id);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_billing_dispute(TEXT, TEXT, TEXT, UUID, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;

-- ── RPC: get_billing_payment_for_refund ───────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_billing_payment_for_refund(
    p_provider TEXT,
    p_provider_payment_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_payment RECORD;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT purpose, amount_minor, currency, user_id
    INTO v_payment
    FROM public.billing_payments
    WHERE provider = p_provider AND provider_payment_id = p_provider_payment_id
    LIMIT 1;

    IF v_payment.purpose IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'purpose', v_payment.purpose,
        'amount_minor', v_payment.amount_minor,
        'currency', v_payment.currency,
        'user_id', v_payment.user_id
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_billing_payment_for_refund(TEXT, TEXT) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;

NOTIFY pgrst, 'reload schema';
