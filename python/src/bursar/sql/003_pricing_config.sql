-- bursar: versioned pricing configuration storage.
-- Enables live pricing updates without redeploys.

CREATE TABLE IF NOT EXISTS public.bursar_config (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    config JSONB NOT NULL,
    active BOOLEAN NOT NULL DEFAULT false,
    version INTEGER NOT NULL DEFAULT 1,
    label TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only one active config at a time
CREATE UNIQUE INDEX IF NOT EXISTS idx_bursar_config_active_unique
    ON public.bursar_config (active)
    WHERE active = true;

-- Serialize version assignment: a unique constraint on version turns a
-- lost-update race into a hard failure instead of two configs sharing a
-- version. Publishers additionally take an advisory lock (see
-- set_active_bursar_config).
CREATE UNIQUE INDEX IF NOT EXISTS idx_bursar_config_version_unique
    ON public.bursar_config (version);

-- Block direct table access — all reads/writes go through RPCs.
ALTER TABLE public.bursar_config ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only pricing config'
        AND tablename = 'bursar_config'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only pricing config" ON public.bursar_config
            USING (false);
    END IF;
END;
$$;


-- get_active_bursar_config: Fetch the currently active pricing configuration.
CREATE OR REPLACE FUNCTION public.get_active_bursar_config()
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
    SELECT id, config, version INTO v_id, v_config, v_version
    FROM public.bursar_config
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


-- SUPERSEDED by 016_plan_versioning.sql — this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.set_active_bursar_config(p_config JSONB, p_label TEXT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN RETURN NULL; END;
$$;

-- get_bursar_configs: List all pricing configs ordered by version.
CREATE OR REPLACE FUNCTION public.get_bursar_configs()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
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
        FROM public.bursar_config
    );
END;
$$;

-- get_bursar_config: Fetch a specific pricing config by version.
CREATE OR REPLACE FUNCTION public.get_bursar_config(p_version INTEGER)
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
    SELECT id, config, version INTO v_id, v_config, v_version
    FROM public.bursar_config
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

-- SUPERSEDED by 016_plan_versioning.sql — this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.activate_bursar_config(p_version INTEGER)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN RETURN NULL; END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_active_bursar_config FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.set_active_bursar_config FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_bursar_configs FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_bursar_config FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.activate_bursar_config FROM PUBLIC, anon, authenticated;

NOTIFY pgrst, 'reload schema';
