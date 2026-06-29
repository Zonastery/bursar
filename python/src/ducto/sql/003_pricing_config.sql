-- ducto: versioned pricing configuration storage.
-- Enables live pricing updates without redeploys.

CREATE TABLE IF NOT EXISTS public.credit_pricing_config (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    config JSONB NOT NULL,
    active BOOLEAN NOT NULL DEFAULT false,
    version INTEGER NOT NULL DEFAULT 1,
    label TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only one active config at a time
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_pricing_config_active_unique
    ON public.credit_pricing_config (active)
    WHERE active = true;

-- Serialize version assignment (M14): a unique constraint on version turns a
-- lost-update race into a hard failure instead of two configs sharing a version.
-- Publishers additionally take an advisory lock (see set_active_pricing_config).
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_pricing_config_version_unique
    ON public.credit_pricing_config (version);

-- Block direct table access — all reads/writes go through RPCs.
ALTER TABLE public.credit_pricing_config ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only pricing config'
        AND tablename = 'credit_pricing_config'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only pricing config" ON public.credit_pricing_config
            USING (false);
    END IF;
END;
$$;


-- get_active_pricing_config: Fetch the currently active pricing configuration.
CREATE OR REPLACE FUNCTION public.get_active_pricing_config()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_config JSONB;
    v_version INTEGER;
    v_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    SELECT id, config, version INTO v_id, v_config, v_version
    FROM public.credit_pricing_config
    WHERE active = true
    ORDER BY created_at DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'id', v_id,
        'config', v_config,
        'version', v_version
    );
END;
$$;


-- set_active_pricing_config: Publish a new pricing config and deactivate the old one.
CREATE OR REPLACE FUNCTION public.set_active_pricing_config(
    p_config JSONB,
    p_label TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_new_id UUID;
    v_next_version INTEGER;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Serialize concurrent publishers so version assignment can't race (M14).
    PERFORM pg_advisory_xact_lock(hashtext('ducto_pricing_version'));

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM public.credit_pricing_config;

    -- Deactivate all existing active configs
    UPDATE public.credit_pricing_config SET active = false WHERE active = true;

    -- Insert new active config
    INSERT INTO public.credit_pricing_config (config, active, version, label)
    VALUES (p_config, true, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true
    );
END;
$$;

-- get_pricing_configs: List all pricing configs ordered by version.
CREATE OR REPLACE FUNCTION public.get_pricing_configs()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN '[]'::JSONB;
    END IF;

    RETURN (
        SELECT jsonb_agg(
            jsonb_build_object(
                'id', id,
                'version', version,
                'label', label,
                'active', active,
                'created_at', created_at
            )
            ORDER BY version DESC
        )
        FROM public.credit_pricing_config
    );
END;
$$;

-- get_pricing_config: Fetch a specific pricing config by version.
CREATE OR REPLACE FUNCTION public.get_pricing_config(p_version INTEGER)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_config JSONB;
    v_id UUID;
    v_version INTEGER;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    SELECT id, config, version INTO v_id, v_config, v_version
    FROM public.credit_pricing_config
    WHERE version = p_version
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'id', v_id,
        'config', v_config,
        'version', v_version
    );
END;
$$;

-- activate_pricing_config: Switch to a specific version and deactivate all others.
CREATE OR REPLACE FUNCTION public.activate_pricing_config(p_version INTEGER)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_target_version INTEGER;
    v_target_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Verify the target version exists
    SELECT id INTO v_target_id
    FROM public.credit_pricing_config
    WHERE version = p_version;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'version_not_found');
    END IF;

    -- Deactivate all configs
    UPDATE public.credit_pricing_config SET active = false WHERE active = true;

    -- Activate the target version
    UPDATE public.credit_pricing_config SET active = true
    WHERE version = p_version
    RETURNING id INTO v_target_id;

    RETURN jsonb_build_object(
        'id', v_target_id,
        'version', p_version,
        'active', true
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_active_pricing_config FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.set_active_pricing_config FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_pricing_configs FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_pricing_config FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.activate_pricing_config FROM anon, authenticated;
