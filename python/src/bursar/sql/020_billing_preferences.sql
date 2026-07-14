-- bursar: billing preferences table — per-user billing settings.
-- Stores auto-recharge, notification toggles, and overage protection.
-- Server-only access (service_role bypasses RLS); no FK to any user table
-- so bursar stays user-table-agnostic.

CREATE TABLE IF NOT EXISTS public.billing_preferences (
    user_id              UUID        PRIMARY KEY,
    auto_recharge        BOOLEAN     NOT NULL DEFAULT false,
    overage_protection   BOOLEAN     NOT NULL DEFAULT true,
    email_notifications  BOOLEAN     NOT NULL DEFAULT true,
    usage_alerts         BOOLEAN     NOT NULL DEFAULT true,
    invoice_reminders    BOOLEAN     NOT NULL DEFAULT false,
    usage_limit_alerts   BOOLEAN     NOT NULL DEFAULT true,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.billing_preferences ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_preferences'
        AND tablename = 'billing_preferences'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_preferences" ON public.billing_preferences USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_preferences_updated_at'
        AND tgrelid = 'public.billing_preferences'::regclass
    ) THEN
        CREATE TRIGGER set_billing_preferences_updated_at
            BEFORE UPDATE ON public.billing_preferences
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── RPC: upsert_billing_preferences ──────────────────────────────────────

CREATE OR REPLACE FUNCTION public.upsert_billing_preferences(
    p_user_id              UUID,
    p_auto_recharge        BOOLEAN DEFAULT false,
    p_overage_protection   BOOLEAN DEFAULT true,
    p_email_notifications  BOOLEAN DEFAULT true,
    p_usage_alerts         BOOLEAN DEFAULT true,
    p_invoice_reminders    BOOLEAN DEFAULT false,
    p_usage_limit_alerts   BOOLEAN DEFAULT true
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    INSERT INTO public.billing_preferences (
        user_id, auto_recharge, overage_protection,
        email_notifications, usage_alerts, invoice_reminders, usage_limit_alerts
    )
    VALUES (
        p_user_id, p_auto_recharge, p_overage_protection,
        p_email_notifications, p_usage_alerts, p_invoice_reminders, p_usage_limit_alerts
    )
    ON CONFLICT (user_id) DO UPDATE SET
        auto_recharge       = COALESCE(p_auto_recharge, billing_preferences.auto_recharge),
        overage_protection  = COALESCE(p_overage_protection, billing_preferences.overage_protection),
        email_notifications = COALESCE(p_email_notifications, billing_preferences.email_notifications),
        usage_alerts        = COALESCE(p_usage_alerts, billing_preferences.usage_alerts),
        invoice_reminders   = COALESCE(p_invoice_reminders, billing_preferences.invoice_reminders),
        usage_limit_alerts  = COALESCE(p_usage_limit_alerts, billing_preferences.usage_limit_alerts),
        updated_at          = now();

    RETURN jsonb_build_object('status', 'ok');
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_billing_preferences(UUID, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_billing_preferences(UUID, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN) TO service_role;

-- ── RPC: get_billing_preferences ─────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_billing_preferences(
    p_user_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_row RECORD;
BEGIN
    SELECT * INTO v_row FROM public.billing_preferences WHERE user_id = p_user_id LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'user_id', v_row.user_id,
        'auto_recharge', v_row.auto_recharge,
        'overage_protection', v_row.overage_protection,
        'email_notifications', v_row.email_notifications,
        'usage_alerts', v_row.usage_alerts,
        'invoice_reminders', v_row.invoice_reminders,
        'usage_limit_alerts', v_row.usage_limit_alerts
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_billing_preferences(UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_billing_preferences(UUID) TO service_role;

-- ── RPC: get_billing_customer_by_user_id (reverse lookup) ────────────────

CREATE OR REPLACE FUNCTION public.get_billing_customer_by_user_id(
    p_user_id  UUID,
    p_provider TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_row RECORD;
BEGIN
    IF p_provider IS NOT NULL THEN
        SELECT provider, provider_customer_id INTO v_row
        FROM public.billing_customers
        WHERE user_id = p_user_id AND provider = p_provider
        ORDER BY updated_at DESC
        LIMIT 1;
    ELSE
        SELECT provider, provider_customer_id INTO v_row
        FROM public.billing_customers
        WHERE user_id = p_user_id
        ORDER BY updated_at DESC
        LIMIT 1;
    END IF;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'provider', v_row.provider,
        'provider_customer_id', v_row.provider_customer_id
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_billing_customer_by_user_id(UUID, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_billing_customer_by_user_id(UUID, TEXT) TO service_role;

NOTIFY pgrst, 'reload schema';
