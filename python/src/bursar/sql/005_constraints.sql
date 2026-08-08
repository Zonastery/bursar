-- Relational invariant and lifecycle trigger functions.

CREATE FUNCTION bursar.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

CREATE TRIGGER tenant_updated_at
BEFORE UPDATE ON bursar.tenants
FOR EACH ROW EXECUTE FUNCTION bursar.touch_updated_at();

CREATE FUNCTION bursar.project_usage_charge()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_charge bursar.credit_usage_charges;
BEGIN
    SELECT *
    INTO v_charge
    FROM bursar.credit_usage_charges AS charge
    WHERE charge.id = NEW.charge_id
      AND charge.event_at = NEW.event_at;

    IF NOT FOUND OR v_charge.billing_disposition = 'record_only' THEN
        RETURN NEW;
    END IF;

    IF bursar.current_usage_backend() = 'postgres' THEN
        INSERT INTO bursar.usage_daily_rollups(
        usage_day,
        account_id,
        operation,
        model_key,
        region_key,
        rollup_shard,
        charged,
        allowance_covered,
        charge_count
    )
    VALUES(
        (NEW.event_at AT TIME ZONE 'UTC')::date,
            v_charge.account_id,
            v_charge.operation,
            COALESCE(NEW.model, ''),
            COALESCE(NEW.region, ''),
        (get_byte(uuid_send(NEW.charge_id), 15) & 31)::smallint,
            v_charge.charged,
            v_charge.allowance_covered,
        1
    )
        ON CONFLICT (
        usage_day,
        account_id,
        operation,
        model_key,
        region_key,
        rollup_shard
    )
        DO UPDATE
        SET charged = bursar.usage_daily_rollups.charged + EXCLUDED.charged,
        allowance_covered =
            bursar.usage_daily_rollups.allowance_covered
            + EXCLUDED.allowance_covered,
        charge_count = bursar.usage_daily_rollups.charge_count + 1,
            updated_at = now();
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.enqueue_quota_notification()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    INSERT INTO bursar.event_outbox(
        topic,
        aggregate_type,
        aggregate_id,
        idempotency_key,
        payload
    )
    VALUES(
        CASE NEW.event_type
            WHEN 'threshold' THEN 'quota.threshold_reached'
            ELSE 'quota.admission_blocked'
        END,
        'quota_event',
        NEW.id,
        'quota-event:' || NEW.id::text,
        jsonb_strip_nulls(
            jsonb_build_object(
                'tenant_id', NEW.tenant_id,
                'quota_event_id', NEW.id,
                'quota_window_id', NEW.quota_window_id,
                'usage_charge_id', NEW.usage_charge_id,
                'event_type', NEW.event_type,
                'threshold_percent', NEW.threshold_percent,
                'created_at', NEW.created_at
            )
        )
    )
    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.enqueue_quota_measurement()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    INSERT INTO bursar.event_outbox(
        topic,
        aggregate_type,
        aggregate_id,
        idempotency_key,
        payload
    )
    VALUES(
        'quota.measurement_recorded',
        'quota_usage_event',
        NEW.id,
        'quota-usage-event:' || NEW.id::text,
        jsonb_strip_nulls(
            jsonb_build_object(
                'tenant_id', NEW.tenant_id,
                'quota_usage_event_id', NEW.id,
                'account_id', NEW.account_id,
                'catalog_quota_id', NEW.catalog_quota_id,
                'quota_key', NEW.quota_key,
                'operation_key', NEW.operation_key,
                'measure_key', NEW.measure_key,
                'amount', bursar.digest_numeric_text(NEW.amount),
                'event_at', NEW.event_at,
                'usage_charge_id', NEW.usage_charge_id,
                'correction_of_event_id', NEW.correction_of_event_id
            )
        )
    )
    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.enqueue_completed_billing_event()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    IF bursar.current_billing_payload_backend() = 'postgres'
       AND NEW.status IN ('completed', 'ignored')
       AND OLD.status IS DISTINCT FROM NEW.status
    THEN
        INSERT INTO bursar.event_outbox(
            topic,
            aggregate_type,
            aggregate_id,
            idempotency_key,
            payload
        )
        VALUES(
            'billing.webhook_completed',
            'billing_event',
            NEW.id,
            'billing-event-completed:' || NEW.id::text,
            jsonb_build_object(
                'tenant_id', NEW.tenant_id,
                'billing_event_id', NEW.id,
                'provider', NEW.provider,
                'provider_environment', NEW.provider_environment,
                'provider_event_id', NEW.provider_event_id,
                'event_type', NEW.event_type,
                'status', NEW.status,
                'completed_at', NEW.completed_at
            )
        )
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.require_valid_timezone()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_timezone text := to_jsonb(NEW)->>TG_ARGV[0];
BEGIN
    IF v_timezone IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_timezone_names
           WHERE name = v_timezone
       )
    THEN
        RAISE EXCEPTION 'invalid timezone: %', v_timezone
            USING ERRCODE = '22023';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.rearm_auto_recharge_profile()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    IF NEW.account_kind = 'personal' AND NEW.balance > OLD.balance THEN
        UPDATE bursar.billing_auto_recharge_profiles
        SET armed = true
        WHERE subject_id = NEW.subject_id
          AND enabled
          AND state = 'active'
          AND NEW.balance >= rearm_above;
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.validate_billing_payment_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    IF OLD.status IN ('pending', 'requires_action')
       OR (
           OLD.status = 'succeeded'
           AND NEW.status IN (
               'partially_refunded', 'refunded', 'disputed'
           )
       )
       OR (
           OLD.status = 'partially_refunded'
           AND NEW.status IN ('refunded', 'disputed')
       )
       OR (
           OLD.status = 'disputed'
           AND NEW.status IN (
               'succeeded', 'partially_refunded', 'refunded'
           )
       )
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'invalid billing payment status transition: % -> %',
        OLD.status,
        NEW.status
        USING ERRCODE = '23514';
END
$$;

CREATE FUNCTION bursar.validate_billing_refund_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    IF NEW.status = OLD.status OR OLD.status = 'pending' THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'invalid billing refund status transition: % -> %',
        OLD.status,
        NEW.status
        USING ERRCODE = '23514';
END
$$;

CREATE FUNCTION bursar.require_internal_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    IF current_setting('bursar.mutation_context', true)
       IS DISTINCT FROM 'internal'
    THEN
        RAISE EXCEPTION 'direct mutation not allowed'
            USING ERRCODE = '42501';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.is_subject_pseudonymized(
    p_subject_id uuid
)
RETURNS boolean
LANGUAGE sql STABLE
SET search_path TO ''
AS $$
    SELECT COALESCE(
        (
            SELECT subject.pseudonymized_at IS NOT NULL
            FROM bursar.subjects AS subject
            WHERE subject.id = p_subject_id
        ),
        false
    )
$$;

CREATE FUNCTION bursar.check_ledger_balance()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_balance numeric;
    v_reference_account uuid;
BEGIN
    SELECT balance
    INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = NEW.account_id
    FOR UPDATE;

    IF NOT FOUND OR NEW.balance_after <> v_balance + NEW.amount THEN
        RAISE EXCEPTION 'ledger balance invariant violated'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.reference_entry_id IS NOT NULL THEN
        SELECT account_id
        INTO v_reference_account
        FROM bursar.credit_ledger_entries
        WHERE id = NEW.reference_entry_id;

        IF v_reference_account IS DISTINCT FROM NEW.account_id THEN
            RAISE EXCEPTION 'ledger reference belongs to another account'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.check_credit_lot()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_entry_account uuid;
    v_entry_amount numeric;
BEGIN
    SELECT account_id, amount
    INTO v_entry_account, v_entry_amount
    FROM bursar.credit_ledger_entries
    WHERE id = NEW.source_entry_id
    FOR UPDATE;

    IF NOT FOUND
       OR v_entry_account <> NEW.account_id
       OR v_entry_amount <= 0
       OR NEW.granted > v_entry_amount
    THEN
        RAISE EXCEPTION 'credit lot source invariant violated'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.check_credit_lot_source()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_entry_account uuid;
    v_entry_amount numeric;
    v_lot_account uuid;
BEGIN
    SELECT account_id, amount
    INTO v_entry_account, v_entry_amount
    FROM bursar.credit_ledger_entries
    WHERE id = NEW.ledger_entry_id
    FOR UPDATE;

    SELECT account_id
    INTO v_lot_account
    FROM bursar.credit_lots
    WHERE id = NEW.lot_id
    FOR UPDATE;

    IF v_entry_account IS NULL
       OR v_entry_account <> v_lot_account
       OR v_entry_amount <= 0
       OR NEW.amount > v_entry_amount
    THEN
        RAISE EXCEPTION 'credit lot source invariant violated'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.check_lot_allocation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_debit numeric;
    v_account uuid;
    v_lot_account uuid;
    v_total numeric;
BEGIN
    SELECT amount, account_id
    INTO v_debit, v_account
    FROM bursar.credit_ledger_entries
    WHERE id = NEW.debit_entry_id
    FOR UPDATE;

    SELECT account_id
    INTO v_lot_account
    FROM bursar.credit_lots
    WHERE id = NEW.lot_id
    FOR UPDATE;

    IF v_debit IS NULL OR v_debit >= 0 OR v_account <> v_lot_account THEN
        RAISE EXCEPTION 'invalid lot allocation'
            USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(sum(amount), 0)
    INTO v_total
    FROM bursar.credit_lot_allocations
    WHERE debit_entry_id = NEW.debit_entry_id;

    IF v_total + NEW.amount > -v_debit THEN
        RAISE EXCEPTION 'lot allocations exceed debit'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.check_lot_source_allocation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_allocation_lot uuid;
    v_allocation_amount numeric;
    v_source_lot uuid;
    v_source_amount numeric;
    v_allocation_total numeric;
    v_source_consumed numeric;
BEGIN
    SELECT lot_id, amount
    INTO v_allocation_lot, v_allocation_amount
    FROM bursar.credit_lot_allocations
    WHERE id = NEW.lot_allocation_id
    FOR UPDATE;

    SELECT lot_id, amount
    INTO v_source_lot, v_source_amount
    FROM bursar.credit_lot_sources
    WHERE id = NEW.lot_source_id
    FOR UPDATE;

    SELECT COALESCE(sum(amount), 0)
    INTO v_allocation_total
    FROM bursar.credit_lot_source_allocations
    WHERE lot_allocation_id = NEW.lot_allocation_id;

    SELECT
        COALESCE((
            SELECT sum(source_allocation.amount)
            FROM bursar.credit_lot_source_allocations AS source_allocation
            WHERE source_allocation.lot_source_id = NEW.lot_source_id
        ), 0)
        - COALESCE((
            SELECT sum(restored.amount)
            FROM bursar.credit_lot_source_restorations AS restored
            JOIN bursar.credit_lot_source_allocations AS source_allocation
              ON source_allocation.id = restored.source_allocation_id
            WHERE source_allocation.lot_source_id = NEW.lot_source_id
        ), 0)
    INTO v_source_consumed;

    IF v_allocation_lot IS NULL
       OR v_source_lot IS NULL
       OR v_allocation_lot <> v_source_lot
       OR v_allocation_total + NEW.amount > v_allocation_amount
       OR v_source_consumed + NEW.amount > v_source_amount
    THEN
        RAISE EXCEPTION 'invalid lot source allocation'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.check_lot_restoration()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_refund_account uuid;
    v_refund_amount numeric;
    v_reference_entry uuid;
    v_debit_entry uuid;
    v_lot uuid;
    v_allocation_amount numeric;
    v_restored numeric;
    v_refund_restored numeric;
BEGIN
    SELECT account_id, amount, reference_entry_id
    INTO v_refund_account, v_refund_amount, v_reference_entry
    FROM bursar.credit_ledger_entries
    WHERE id = NEW.refund_entry_id
      AND kind = 'refund'
    FOR UPDATE;

    SELECT debit_entry_id, lot_id, amount
    INTO v_debit_entry, v_lot, v_allocation_amount
    FROM bursar.credit_lot_allocations
    WHERE id = NEW.original_allocation_id
    FOR UPDATE;

    IF v_refund_amount IS NULL
       OR v_refund_amount <= 0
       OR v_reference_entry IS DISTINCT FROM v_debit_entry
       OR v_lot IS DISTINCT FROM NEW.lot_id
       OR NOT EXISTS (
           SELECT 1
           FROM bursar.credit_lots
           WHERE id = NEW.lot_id
             AND account_id = v_refund_account
       )
    THEN
        RAISE EXCEPTION 'invalid lot restoration'
            USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(sum(amount), 0)
    INTO v_restored
    FROM bursar.credit_lot_restorations
    WHERE original_allocation_id = NEW.original_allocation_id;

    SELECT COALESCE(sum(amount), 0)
    INTO v_refund_restored
    FROM bursar.credit_lot_restorations
    WHERE refund_entry_id = NEW.refund_entry_id;

    IF v_restored + NEW.amount > v_allocation_amount
       OR v_refund_restored + NEW.amount > v_refund_amount
    THEN
        RAISE EXCEPTION 'lot restoration exceeds source'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.check_lot_source_restoration()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_original_allocation uuid;
    v_restoration_amount numeric;
    v_source_allocation_id uuid;
    v_source_allocation_amount numeric;
    v_restoration_total numeric;
    v_source_restored numeric;
BEGIN
    SELECT original_allocation_id, amount
    INTO v_original_allocation, v_restoration_amount
    FROM bursar.credit_lot_restorations
    WHERE id = NEW.lot_restoration_id
    FOR UPDATE;

    SELECT lot_allocation_id, amount
    INTO v_source_allocation_id, v_source_allocation_amount
    FROM bursar.credit_lot_source_allocations
    WHERE id = NEW.source_allocation_id
    FOR UPDATE;

    SELECT COALESCE(sum(amount), 0)
    INTO v_restoration_total
    FROM bursar.credit_lot_source_restorations
    WHERE lot_restoration_id = NEW.lot_restoration_id;

    SELECT COALESCE(sum(amount), 0)
    INTO v_source_restored
    FROM bursar.credit_lot_source_restorations
    WHERE source_allocation_id = NEW.source_allocation_id;

    IF v_original_allocation IS NULL
       OR v_source_allocation_id IS NULL
       OR v_original_allocation <> v_source_allocation_id
       OR v_restoration_total + NEW.amount > v_restoration_amount
       OR v_source_restored + NEW.amount > v_source_allocation_amount
    THEN
        RAISE EXCEPTION 'invalid lot source restoration'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.check_quota_usage_event()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_quota record;
    v_original bursar.quota_usage_events;
    v_corrected numeric;
    v_storage bursar.storage_settings;
BEGIN
    SELECT *
    INTO v_storage
    FROM bursar.storage_settings
    WHERE singleton;

    IF NEW.correction_of_event_id IS NULL
       AND (
           NEW.event_at < now()
               - make_interval(secs => v_storage.quota_max_lateness_seconds)
           OR NEW.event_at > now() + interval '5 minutes'
       )
    THEN
        RAISE EXCEPTION 'quota usage event outside accepted time horizon'
            USING ERRCODE = '22023';
    END IF;

    SELECT
        quota.catalog_revision_id,
        quota.plan_key,
        quota.quota_key,
        quota.operation_key,
        quota.measure_key,
        plan.id AS plan_id
    INTO v_quota
    FROM bursar.catalog_plan_quotas AS quota
    JOIN bursar.catalog_plans AS plan
      ON plan.catalog_revision_id = quota.catalog_revision_id
     AND plan.plan_key = quota.plan_key
    WHERE quota.id = NEW.catalog_quota_id;

    IF v_quota.catalog_revision_id IS NULL
       OR (
           v_quota.catalog_revision_id,
           v_quota.plan_id,
           v_quota.quota_key,
           v_quota.operation_key,
           v_quota.measure_key
       ) IS DISTINCT FROM (
           NEW.catalog_revision_id,
           NEW.plan_id,
           NEW.quota_key,
           NEW.operation_key,
           NEW.measure_key
       )
       OR NOT EXISTS (
           SELECT 1
           FROM bursar.credit_accounts AS account
           WHERE account.id = NEW.account_id
       )
       OR (
           NEW.usage_charge_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
               FROM bursar.credit_usage_charges AS charge
               WHERE charge.id = NEW.usage_charge_id
                 AND charge.account_id = NEW.account_id
           )
       )
    THEN
        RAISE EXCEPTION 'invalid quota usage event'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.correction_of_event_id IS NOT NULL THEN
        SELECT *
        INTO v_original
        FROM bursar.quota_usage_events
        WHERE id = NEW.correction_of_event_id
        FOR UPDATE;

        IF NOT FOUND
           OR v_original.correction_of_event_id IS NOT NULL
           OR v_original.account_id <> NEW.account_id
           OR v_original.catalog_quota_id <> NEW.catalog_quota_id
        THEN
            RAISE EXCEPTION 'quota correction source mismatch'
                USING ERRCODE = '23514';
        END IF;

        SELECT COALESCE(sum(-amount), 0)
        INTO v_corrected
        FROM bursar.quota_usage_events
        WHERE correction_of_event_id = NEW.correction_of_event_id;

        IF v_corrected + (-NEW.amount) > v_original.amount THEN
            RAISE EXCEPTION 'quota correction exceeds original event'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.archive_plan_assignment()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_end timestamptz;
BEGIN
    IF TG_OP = 'UPDATE'
       AND ROW(
           NEW.plan_id,
           NEW.catalog_revision_id,
           NEW.plan_key,
           NEW.source_type,
           NEW.source_id,
           NEW.starts_at,
           NEW.ends_at
       ) IS NOT DISTINCT FROM ROW(
           OLD.plan_id,
           OLD.catalog_revision_id,
           OLD.plan_key,
           OLD.source_type,
           OLD.source_id,
           OLD.starts_at,
           OLD.ends_at
       )
    THEN
        RETURN NEW;
    END IF;

    v_end := CASE
        WHEN TG_OP = 'UPDATE'
             AND NEW.starts_at > OLD.starts_at
            THEN LEAST(
                COALESCE(OLD.ends_at, NEW.starts_at),
                NEW.starts_at
            )
        ELSE LEAST(COALESCE(OLD.ends_at, now()), now())
    END;

    IF OLD.starts_at < v_end THEN
        INSERT INTO bursar.account_plan_assignment_history(
            assignment_id,
            account_id,
            plan_id,
            catalog_revision_id,
            plan_key,
            catalog_revision_pinned,
            source_type,
            source_id,
            starts_at,
            ends_at,
            replacement_reason
        )
        VALUES (
            OLD.assignment_id,
            OLD.account_id,
            OLD.plan_id,
            OLD.catalog_revision_id,
            OLD.plan_key,
            OLD.catalog_revision_pinned,
            OLD.source_type,
            OLD.source_id,
            OLD.starts_at,
            v_end,
            COALESCE(
                NULLIF(
                    current_setting('bursar.assignment_reason', true),
                    ''
                ),
                CASE WHEN TG_OP = 'DELETE' THEN 'removed' ELSE 'reassigned' END
            )
        )
        ON CONFLICT (assignment_id, ends_at) DO NOTHING;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    IF (NEW.plan_id, NEW.catalog_revision_id, NEW.starts_at)
       IS DISTINCT FROM
       (OLD.plan_id, OLD.catalog_revision_id, OLD.starts_at)
    THEN
        NEW.assignment_id := bursar.uuid_v7();
        NEW.created_at := now();
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.reject_catalog_projection_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    RAISE EXCEPTION 'projected catalog rows are immutable'
        USING ERRCODE = '55000';
END
$$;

CREATE FUNCTION bursar.validate_catalog_entitlement_feature()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_value_schema json;
    v_definition_name text := CASE NEW.value_type
        WHEN 'boolean' THEN 'BooleanFeature'
        WHEN 'integer' THEN 'IntegerFeature'
        WHEN 'string' THEN 'StringFeature'
        WHEN 'enum' THEN 'EnumFeature'
    END;
BEGIN
    IF v_definition_name IS NULL
       OR NOT bursar.matches_catalog_definitions(
           NEW.definition,
           v_definition_name
       )
       OR NEW.definition->>'type' IS DISTINCT FROM NEW.value_type
       OR NEW.definition->'default' IS DISTINCT FROM NEW.default_value
    THEN
        RAISE EXCEPTION 'invalid entitlement feature definition'
            USING ERRCODE = '23514';
    END IF;

    v_value_schema := bursar.entitlement_value_schema(NEW.definition);
    IF v_value_schema IS NULL
       OR NOT extensions.jsonschema_is_valid(v_value_schema)
    THEN
        RAISE EXCEPTION 'invalid entitlement value schema'
            USING ERRCODE = '23514';
    END IF;

    IF NOT extensions.jsonb_matches_schema(
        v_value_schema,
        NEW.default_value
    ) THEN
        RAISE EXCEPTION 'invalid entitlement default'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.validate_catalog_plan_feature()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_feature bursar.catalog_entitlement_features;
    v_value_schema json;
BEGIN
    SELECT *
    INTO v_feature
    FROM bursar.catalog_entitlement_features
    WHERE catalog_revision_id = NEW.catalog_revision_id
      AND feature_key = NEW.feature_key;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'plan entitlement feature is not declared'
            USING ERRCODE = '23503';
    END IF;

    v_value_schema := bursar.entitlement_value_schema(v_feature.definition);
    IF v_value_schema IS NULL
       OR NOT extensions.jsonschema_is_valid(v_value_schema) THEN
        RAISE EXCEPTION 'invalid entitlement value schema'
            USING ERRCODE = '23514';
    END IF;

    IF NOT extensions.jsonb_matches_schema(
        v_value_schema,
        NEW.feature_value
    ) THEN
        RAISE EXCEPTION 'invalid plan entitlement value'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.validate_catalog_plan_quota()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_measures jsonb;
    v_threshold integer;
    v_previous integer := 0;
    v_duration_seconds bigint;
    v_required_seconds bigint;
    v_storage bursar.storage_settings;
BEGIN
    SELECT measures
    INTO v_measures
    FROM bursar.catalog_operations
    WHERE catalog_revision_id = NEW.catalog_revision_id
      AND operation_key = NEW.operation_key;

    IF NOT FOUND OR NOT (v_measures ? NEW.measure_key) THEN
        RAISE EXCEPTION 'quota references an unknown operation measure'
            USING ERRCODE = '23503';
    END IF;

    FOREACH v_threshold IN ARRAY NEW.emit_at_percent LOOP
        IF v_threshold < 1
           OR v_threshold > 100
           OR v_threshold <= v_previous
        THEN
            RAISE EXCEPTION
                'quota thresholds must be unique, increasing, and within 1..100'
                USING ERRCODE = '23514';
        END IF;
        v_previous := v_threshold;
    END LOOP;

    v_duration_seconds := bursar.policy_duration_seconds(NEW.window_policy);

    IF v_duration_seconds IS NULL THEN
        RAISE EXCEPTION 'invalid rolling quota duration'
            USING ERRCODE = '23514';
    END IF;

    IF v_duration_seconds > 0 THEN
        SELECT *
        INTO v_storage
        FROM bursar.storage_settings
        WHERE singleton;

        v_required_seconds :=
            v_duration_seconds
            + v_storage.quota_max_lateness_seconds
            + v_storage.quota_correction_window_days * 86400::bigint
            + v_storage.quota_retention_safety_days * 86400::bigint;

        IF v_required_seconds
           > v_storage.quota_event_retention_days * 86400::bigint
        THEN
            RAISE EXCEPTION
                'rolling quota exceeds configured event retention horizon'
                USING ERRCODE = '23514',
                      DETAIL = format(
                          'required=%s seconds configured=%s seconds',
                          v_required_seconds,
                          v_storage.quota_event_retention_days * 86400::bigint
                      );
        END IF;
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.validate_catalog_grant_award()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_trigger_type text;
BEGIN
    SELECT trigger_type
    INTO v_trigger_type
    FROM bursar.catalog_grant_programs
    WHERE id = NEW.grant_program_id
      AND catalog_revision_id = NEW.catalog_revision_id;

    IF v_trigger_type IS NULL
       OR (
           NEW.recipient = 'referrer'
           AND v_trigger_type <> 'referral_completed'
       )
    THEN
        RAISE EXCEPTION
            'referrer grant awards require referral_completed trigger'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.validate_catalog_provider_ref()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM bursar.catalog_provider_refs AS existing
        WHERE existing.provider = NEW.provider
          AND existing.provider_environment = NEW.provider_environment
          AND existing.lookup_type = NEW.lookup_type
          AND existing.lookup_value = NEW.lookup_value
          AND (
              existing.object_type,
              existing.object_key
          ) IS DISTINCT FROM (
              NEW.object_type,
              NEW.object_key
          )
    )
    THEN
        RAISE EXCEPTION
            'provider lookup identity cannot be reassigned across catalogs'
            USING ERRCODE = '23505';
    END IF;

    IF (
        NEW.object_type = 'offer'
        AND EXISTS (
            SELECT 1
            FROM bursar.catalog_offers
            WHERE catalog_revision_id = NEW.catalog_revision_id
              AND offer_key = NEW.object_key
        )
    )
    OR (
        NEW.object_type = 'topup'
        AND EXISTS (
            SELECT 1
            FROM bursar.catalog_topups
            WHERE catalog_revision_id = NEW.catalog_revision_id
              AND topup_key = NEW.object_key
        )
    )
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'catalog provider reference target missing'
        USING ERRCODE = '23503';
END
$$;

CREATE FUNCTION bursar.one_active_catalog_revision()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    IF OLD.status <> 'draft'
       AND (
           NEW.yaml_schema_version <> OLD.yaml_schema_version
           OR NEW.source_document <> OLD.source_document
           OR NEW.digest <> OLD.digest
           OR NEW.label IS DISTINCT FROM OLD.label
       )
    THEN
        RAISE EXCEPTION 'catalog revision content is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NOT (
        NEW.status = OLD.status
        OR (OLD.status = 'draft' AND NEW.status IN ('published', 'active'))
        OR (OLD.status = 'published' AND NEW.status IN ('active', 'retired'))
        OR (OLD.status = 'active' AND NEW.status = 'retired')
        OR (OLD.status = 'retired' AND NEW.status = 'active')
    )
    THEN
        RAISE EXCEPTION 'invalid catalog revision transition: % -> %',
            OLD.status,
            NEW.status
            USING ERRCODE = '55000';
    END IF;

    IF NEW.status = 'active' THEN
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                'bursar.tenant:'
                || bursar.require_tenant_id()::text
                || ':catalog.active',
                0
            )
        );

        UPDATE bursar.catalog_revisions
        SET status = 'retired',
            retired_at = now()
        WHERE tenant_id = NEW.tenant_id
          AND status = 'active'
          AND id <> NEW.id;

        IF OLD.status <> 'active' THEN
            NEW.activated_at := now();
        END IF;

        NEW.published_at := COALESCE(NEW.published_at, now());
        NEW.retired_at := NULL;
    END IF;

    IF NEW.status = 'published' THEN
        NEW.published_at := COALESCE(NEW.published_at, now());
    END IF;

    IF NEW.status = 'retired' AND OLD.status <> 'retired' THEN
        NEW.retired_at := now();
    END IF;

    RETURN NEW;
END
$$;

CREATE FUNCTION bursar.reject_revision_delete()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    IF OLD.status <> 'draft' THEN
        RAISE EXCEPTION 'catalog revisions are immutable'
            USING ERRCODE = '55000';
    END IF;

    RETURN OLD;
END
$$;

CREATE FUNCTION bursar.check_refund_bounds()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
DECLARE
    v_payment uuid;
    v_grant_payment uuid;
    v_original bigint;
    v_refund_amount bigint;
    v_refunded bigint;
    v_payment_provider text;
    v_payment_environment text;
    v_payment_currency text;
    v_grant_credits numeric;
    v_total_credits numeric;
    v_clawed_back numeric;
BEGIN
    IF TG_TABLE_NAME = 'billing_refunds' THEN
        SELECT
            amount_minor,
            provider,
            provider_environment,
            currency
        INTO
            v_original,
            v_payment_provider,
            v_payment_environment,
            v_payment_currency
        FROM bursar.billing_payments
        WHERE id = NEW.payment_id
        FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'refund payment missing'
                USING ERRCODE = '23503';
        END IF;

        IF NEW.provider <> v_payment_provider
           OR NEW.provider_environment <> v_payment_environment
           OR NEW.currency <> v_payment_currency
        THEN
            RAISE EXCEPTION 'refund payment mismatch'
                USING ERRCODE = '23514';
        END IF;

        SELECT COALESCE(sum(amount_minor), 0)
        INTO v_refunded
        FROM bursar.billing_refunds
        WHERE payment_id = NEW.payment_id
          AND status NOT IN ('failed', 'canceled')
          AND id <> NEW.id;

        IF v_refunded + NEW.amount_minor > v_original THEN
            RAISE EXCEPTION 'refund exceeds payment'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        SELECT payment_id, amount_minor
        INTO v_payment, v_refund_amount
        FROM bursar.billing_refunds
        WHERE id = NEW.refund_id
        FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'refund missing' USING ERRCODE = '23503';
        END IF;

        SELECT payment_id, configured_credits * quantity
        INTO v_grant_payment, v_grant_credits
        FROM bursar.billing_credit_grants
        WHERE id = NEW.grant_id
        FOR UPDATE;

        IF NOT FOUND OR v_grant_payment IS DISTINCT FROM v_payment THEN
            RAISE EXCEPTION 'refund grant payment mismatch'
                USING ERRCODE = '23514';
        END IF;

        SELECT amount_minor
        INTO v_original
        FROM bursar.billing_payments
        WHERE id = v_payment;

        SELECT sum(configured_credits * quantity)
        INTO v_total_credits
        FROM bursar.billing_credit_grants
        WHERE payment_id = v_payment;

        IF v_original <= 0
           OR v_total_credits IS NULL
           OR v_total_credits <= 0
        THEN
            RAISE EXCEPTION 'refund credit rate unavailable'
                USING ERRCODE = '23514';
        END IF;

        SELECT COALESCE(sum(amount_minor), 0)
        INTO v_refunded
        FROM bursar.billing_refund_grants
        WHERE refund_id = NEW.refund_id
          AND grant_id <> NEW.grant_id;

        IF v_refunded + NEW.amount_minor > v_refund_amount THEN
            RAISE EXCEPTION 'refund grant exceeds refund'
                USING ERRCODE = '23514';
        END IF;

        NEW.credit_amount := round(
            NEW.amount_minor::numeric * v_total_credits / v_original,
            6
        );

        IF NEW.credit_amount <= 0 THEN
            RAISE EXCEPTION 'refund credit amount rounds to zero'
                USING ERRCODE = '23514';
        END IF;

        SELECT COALESCE(sum(credit_amount), 0)
        INTO v_clawed_back
        FROM bursar.billing_refund_grants
        WHERE grant_id = NEW.grant_id
          AND NOT (
              refund_id = NEW.refund_id
              AND grant_id = NEW.grant_id
          );

        IF v_clawed_back + NEW.credit_amount > v_grant_credits THEN
            RAISE EXCEPTION 'refund grant exceeds original grant'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END
$$;

-- Snapshot the original relationship graph, then replace every relationship
-- between tenant-owned tables with a tenant-prefixed composite foreign key.
-- The original single-tenant foreign keys are removed after the composite
-- constraints have been installed.
--
-- Subject identifiers are the only host-supplied IDs and may repeat between
-- tenants. Build their final key up front so the relationship rewrite can use
-- it directly instead of leaving a duplicate unique index behind.
CREATE UNIQUE INDEX subjects_tenant_id_id_uidx
ON bursar.subjects (tenant_id, id);

CREATE TEMPORARY TABLE bursar_multitenant_foreign_keys
ON COMMIT DROP
AS
SELECT
    constraint_info.oid,
    constraint_info.conname,
    constraint_info.conrelid,
    constraint_info.confrelid,
    constraint_info.conkey,
    constraint_info.confkey,
    constraint_info.confupdtype,
    constraint_info.confdeltype,
    constraint_info.condeferrable,
    constraint_info.condeferred,
    child_table.relname AS child_table_name,
    parent_table.relname AS parent_table_name
FROM pg_constraint AS constraint_info
INNER JOIN pg_class AS child_table
    ON constraint_info.conrelid = child_table.oid
INNER JOIN pg_class AS parent_table
    ON constraint_info.confrelid = parent_table.oid
WHERE
    constraint_info.contype = 'f'
    AND constraint_info.connamespace = 'bursar'::regnamespace
    AND constraint_info.conparentid = 0
    AND NOT child_table.relispartition
    AND NOT parent_table.relispartition
    AND EXISTS (
        SELECT 1
        FROM pg_attribute AS child_tenant
        WHERE
            child_tenant.attrelid = constraint_info.conrelid
            AND child_tenant.attname = 'tenant_id'
            AND NOT child_tenant.attisdropped
    )
    AND EXISTS (
        SELECT 1
        FROM pg_attribute AS parent_tenant
        WHERE
            parent_tenant.attrelid = constraint_info.confrelid
            AND parent_tenant.attname = 'tenant_id'
            AND NOT parent_tenant.attisdropped
    );

DO $$
DECLARE
    v_fk record;
    v_child_columns text;
    v_child_column_names text;
    v_parent_columns text;
    v_parent_column_names text;
    v_child_key smallint [];
    v_parent_key smallint [];
    v_child_tenant_attnum smallint;
    v_parent_tenant_attnum smallint;
    v_child_nullable_predicate text;
    v_parent_index_name text;
    v_child_index_name text;
    v_constraint_name text;
    v_name_seed text;
    v_update_action text;
    v_delete_action text;
    v_deferrability text;
BEGIN
    FOR v_fk IN
        SELECT * FROM bursar_multitenant_foreign_keys ORDER BY oid
    LOOP
        SELECT
            string_agg(
                format('%I', attribute_info.attname),
                ', ' ORDER BY key_info.ordinality
            ),
            string_agg(
                attribute_info.attname,
                '_' ORDER BY key_info.ordinality
            ),
            string_agg(
                format('%I IS NOT NULL', attribute_info.attname),
                ' AND ' ORDER BY key_info.ordinality
            ) FILTER (WHERE NOT attribute_info.attnotnull)
        INTO
            v_child_columns,
            v_child_column_names,
            v_child_nullable_predicate
        FROM unnest(v_fk.conkey) WITH ORDINALITY AS key_info(attnum, ordinality)
        JOIN pg_attribute AS attribute_info
          ON attribute_info.attrelid = v_fk.conrelid
         AND attribute_info.attnum = key_info.attnum;

        SELECT
            string_agg(
                format('%I', attribute_info.attname),
                ', ' ORDER BY key_info.ordinality
            ),
            string_agg(
                attribute_info.attname,
                '_' ORDER BY key_info.ordinality
            )
        INTO v_parent_columns, v_parent_column_names
        FROM unnest(v_fk.confkey) WITH ORDINALITY AS key_info(attnum, ordinality)
        JOIN pg_attribute AS attribute_info
          ON attribute_info.attrelid = v_fk.confrelid
         AND attribute_info.attnum = key_info.attnum;

        SELECT attribute_info.attnum
        INTO v_child_tenant_attnum
        FROM pg_attribute AS attribute_info
        WHERE attribute_info.attrelid = v_fk.conrelid
          AND attribute_info.attname = 'tenant_id'
          AND NOT attribute_info.attisdropped;

        SELECT attribute_info.attnum
        INTO v_parent_tenant_attnum
        FROM pg_attribute AS attribute_info
        WHERE attribute_info.attrelid = v_fk.confrelid
          AND attribute_info.attname = 'tenant_id'
          AND NOT attribute_info.attisdropped;

        v_child_key := array_prepend(v_child_tenant_attnum, v_fk.conkey);
        v_parent_key := array_prepend(v_parent_tenant_attnum, v_fk.confkey);

        v_name_seed := v_fk.parent_table_name
            || '_tenant_id_'
            || v_parent_column_names
            || '_key';
        v_parent_index_name := CASE
            WHEN length(v_name_seed) <= 63 THEN v_name_seed
            ELSE left(v_name_seed, 54)
                || '_'
                || substr(md5(v_name_seed), 1, 8)
        END;

        v_name_seed := v_fk.child_table_name
            || '_tenant_id_'
            || v_child_column_names
            || '_idx';
        v_child_index_name := CASE
            WHEN length(v_name_seed) <= 63 THEN v_name_seed
            ELSE left(v_name_seed, 54)
                || '_'
                || substr(md5(v_name_seed), 1, 8)
        END;

        v_name_seed := v_fk.child_table_name
            || '_tenant_id_'
            || v_child_column_names
            || '_'
            || v_fk.parent_table_name
            || '_fkey';
        v_constraint_name := CASE
            WHEN length(v_name_seed) <= 63 THEN v_name_seed
            ELSE left(v_name_seed, 54)
                || '_'
                || substr(md5(v_name_seed), 1, 8)
        END;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_index AS index_info
            WHERE index_info.indrelid = v_fk.confrelid
              AND index_info.indisunique
              AND index_info.indisvalid
              AND index_info.indpred IS NULL
              AND index_info.indnkeyatts = cardinality(v_parent_key)
              AND (
                  regexp_split_to_array(
                      trim(index_info.indkey::text),
                      ' +'
                  )
              )[1:cardinality(v_parent_key)]::smallint[] = v_parent_key
        ) THEN
            EXECUTE format(
                'CREATE UNIQUE INDEX %I ON %s (tenant_id, %s)',
                v_parent_index_name,
                v_fk.confrelid::regclass,
                v_parent_columns
            );
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_index AS index_info
            WHERE index_info.indrelid = v_fk.conrelid
              AND index_info.indisvalid
              AND index_info.indnkeyatts >= cardinality(v_child_key)
              AND (
                  regexp_split_to_array(
                      trim(index_info.indkey::text),
                      ' +'
                  )
              )[1:cardinality(v_child_key)]::smallint[] = v_child_key
              AND (
                  index_info.indpred IS NULL
                  OR (
                      v_child_nullable_predicate IS NOT NULL
                      AND pg_get_expr(
                          index_info.indpred,
                          index_info.indrelid
                      ) = v_child_nullable_predicate
                  )
              )
        ) THEN
            EXECUTE format(
                'CREATE INDEX %I ON %s (tenant_id, %s)%s',
                v_child_index_name,
                v_fk.conrelid::regclass,
                v_child_columns,
                CASE
                    WHEN v_child_nullable_predicate IS NULL THEN ''
                    ELSE ' WHERE ' || v_child_nullable_predicate
                END
            );
        END IF;

        v_update_action := CASE v_fk.confupdtype
            WHEN 'r' THEN ' ON UPDATE RESTRICT'
            WHEN 'c' THEN ' ON UPDATE CASCADE'
            WHEN 'n' THEN ' ON UPDATE SET NULL'
            WHEN 'd' THEN ' ON UPDATE SET DEFAULT'
            ELSE ''
        END;
        v_delete_action := CASE v_fk.confdeltype
            WHEN 'r' THEN ' ON DELETE RESTRICT'
            WHEN 'c' THEN ' ON DELETE CASCADE'
            WHEN 'n' THEN ' ON DELETE SET NULL'
            WHEN 'd' THEN ' ON DELETE SET DEFAULT'
            ELSE ''
        END;
        v_deferrability := CASE
            WHEN v_fk.condeferrable AND v_fk.condeferred
            THEN ' DEFERRABLE INITIALLY DEFERRED'
            WHEN v_fk.condeferrable
            THEN ' DEFERRABLE INITIALLY IMMEDIATE'
            ELSE ''
        END;

        EXECUTE format(
            'ALTER TABLE %s ADD CONSTRAINT %I '
            'FOREIGN KEY (tenant_id, %s) '
            'REFERENCES %s (tenant_id, %s)%s%s%s',
            v_fk.conrelid::regclass,
            v_constraint_name,
            v_child_columns,
            v_fk.confrelid::regclass,
            v_parent_columns,
            v_update_action,
            v_delete_action,
            v_deferrability
        );
    END LOOP;

    FOR v_fk IN
        SELECT * FROM bursar_multitenant_foreign_keys ORDER BY oid
    LOOP
        EXECUTE format(
            'ALTER TABLE %s DROP CONSTRAINT %I',
            v_fk.conrelid::regclass,
            v_fk.conname
        );
    END LOOP;
END
$$;

-- Subject IDs are supplied by host applications and may legitimately be the
-- same UUID in two tenants. Their primary key must therefore include tenancy.
ALTER TABLE bursar.subjects DROP CONSTRAINT subjects_pkey;
ALTER TABLE bursar.subjects
ADD CONSTRAINT subjects_pkey
PRIMARY KEY USING INDEX subjects_tenant_id_id_uidx;
