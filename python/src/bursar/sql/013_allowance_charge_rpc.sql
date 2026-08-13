-- Migration: 013_allowance_charge_rpc.sql
-- Purpose: Couple fixed-window allowance consumption to one usage charge.
-- Depends on: 009_credit_account_rpc.sql and 010_usage_charge_rpc.sql.
-- Security: SECURITY DEFINER binds through the tenant account and locks it before
--   replay or allowance admission; internal mutation remains in delegated RPCs.

-- Couple fixed-window allowance consumption to one usage charge.

-- Lock the tenant account before replay detection, atomically consume the exact
-- allowance amount, and delegate canonical usage idempotency to charge_usage.
-- A failed first charge compensates that consumption in the same transaction;
-- identical retries reuse stored allowance attribution and divergent payloads conflict.

CREATE FUNCTION bursar.charge_usage_with_window(
    p_subject_id uuid,
    p_operation text,
    p_requested numeric,
    p_idempotency_key text,
    p_allowance_key text,
    p_window_start timestamptz,
    p_window_end timestamptz,
    p_allowance numeric,
    p_model text DEFAULT NULL,
    p_region text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_charge_feature text DEFAULT NULL,
    p_measures jsonb DEFAULT '{}'::jsonb,
    p_dimensions jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    charge_id uuid,
    ledger_entry_id uuid,
    charged numeric,
    allowance_covered numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_result record;
 v_allowance numeric;
 v_allowance_requested numeric;
 v_account uuid;
 v_existing bursar.credit_usage_charges;
 v_event_at timestamptz := now();
 v_charge_metadata jsonb;

BEGIN
    p_requested := round(p_requested, 6);
    p_allowance := round(p_allowance, 6);

    IF p_subject_id IS NULL
       OR p_requested IS NULL
       OR NOT bursar.is_credit_numeric(p_requested)
       OR p_requested < 0
       OR NOT bursar.is_credit_numeric(p_allowance)
       OR p_allowance < 0
       OR NOT bursar.is_nonempty_text(p_operation)
       OR NOT bursar.is_bounded_text(p_operation, 255)
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR NOT bursar.is_bounded_text(p_idempotency_key, 255)
       OR NOT bursar.is_nonempty_text(p_allowance_key)
       OR NOT bursar.is_bounded_text(p_allowance_key, 255)
       OR p_window_start IS NULL
       OR p_window_end IS NULL
       OR NOT pg_catalog.isfinite(p_window_start)
       OR NOT pg_catalog.isfinite(p_window_end)
       OR p_window_end <= p_window_start
       OR (
           p_charge_feature IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_charge_feature, 255)
       )
       OR (
           p_model IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_model, 255)
       )
       OR (
           p_region IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_region, 255)
       )
       OR NOT bursar.is_bounded_json_object(
           COALESCE(p_metadata, '{}'::jsonb),
           1048576
       )
       OR NOT bursar.valid_measure_object(
           COALESCE(p_measures, '{}'::jsonb),
           16384
       )
       OR NOT bursar.valid_dimension_object(
           COALESCE(p_dimensions, '{}'::jsonb),
           65536
       )
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            0::numeric,
            0::numeric,
            false,
            'invalid_request'::text;
        RETURN;
    END IF;

    v_allowance_requested := LEAST(p_requested, p_allowance);
    v_allowance := v_allowance_requested;
    v_charge_metadata := COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_build_object(
            '_bursar_allowance_window',
            jsonb_build_object(
                'allowance_key', p_allowance_key,
                'window_start', p_window_start,
                'window_end', p_window_end,
                'allowance_requested',
                    bursar.digest_numeric_text(v_allowance_requested)
            )
        );

    IF NOT bursar.is_bounded_json_object(v_charge_metadata, 1048576) THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::uuid, 0::numeric, 0::numeric,
               false, 'invalid_request'::text;
        RETURN;
    END IF;

    v_account:=bursar.account_for_subject(p_subject_id);

    -- Account-scoped serialization makes the pre-charge idempotency check and
    -- allowance consumption one admission decision. Without it, two first
    -- attempts can both consume allowance before charge_usage sees the replay.
    PERFORM 1
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    SELECT charge.*
    INTO v_existing
    FROM bursar.credit_usage_charges AS charge
    WHERE charge.account_id = v_account
      AND charge.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        SELECT * INTO v_result
        FROM bursar.charge_usage(
        p_subject_id,p_operation,p_requested,p_idempotency_key,p_charge_feature,p_model,p_region,
        v_existing.allowance_covered,
        v_charge_metadata,
        v_existing.allowance_requested,
        p_event_at => v_existing.event_at,
        p_measures => COALESCE(p_measures, '{}'::jsonb),
        p_dimensions => COALESCE(p_dimensions, '{}'::jsonb)
        );

        RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;

        RETURN;

    END IF;

    IF v_allowance > 0 THEN
        UPDATE bursar.allowance_windows AS allowance_window
        SET consumed = allowance_window.consumed + v_allowance
        WHERE allowance_window.account_id = v_account
          AND allowance_window.allowance_key = p_allowance_key
          AND allowance_window.window_start = p_window_start
          AND allowance_window.window_end = p_window_end
          AND allowance_window.consumed
                + allowance_window.reserved
                + v_allowance <= allowance_window.allowance;

        IF NOT FOUND THEN
            v_allowance := 0;
        END IF;
    END IF;

    SELECT * INTO v_result FROM bursar.charge_usage(
        p_subject_id,
        p_operation,
        p_requested,
        p_idempotency_key,
        p_charge_feature,
        p_model,
        p_region,
        v_allowance,
        v_charge_metadata,
        v_allowance_requested,
        p_event_at => v_event_at,
        p_measures => COALESCE(p_measures, '{}'::jsonb),
        p_dimensions => COALESCE(p_dimensions, '{}'::jsonb)
    );

    IF v_result.error_code IS NOT NULL AND v_allowance > 0 THEN
        UPDATE bursar.allowance_windows AS aw
        SET consumed=aw.consumed-v_allowance
        WHERE aw.account_id=v_account AND aw.allowance_key=p_allowance_key AND aw.window_start=p_window_start
          AND aw.window_end=p_window_end AND aw.consumed>=v_allowance;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'allowance rollback is inconsistent'
                USING ERRCODE = '23514';
        END IF;

    END IF;

    RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;

END $$;
