-- PostgreSQL-first storage lifecycle and optional-export RPCs.

CREATE FUNCTION bursar.get_storage_settings()
RETURNS bursar.storage_settings
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT settings
    FROM bursar.storage_settings AS settings
    WHERE settings.singleton
$$;

CREATE FUNCTION bursar.configure_storage(
    p_usage_payload_retention_days integer DEFAULT NULL,
    p_quota_event_retention_days integer DEFAULT NULL,
    p_quota_max_lateness_seconds integer DEFAULT NULL,
    p_quota_correction_window_days integer DEFAULT NULL,
    p_quota_retention_safety_days integer DEFAULT NULL,
    p_billing_payload_retention_days integer DEFAULT NULL,
    p_quota_notification_retention_days integer DEFAULT NULL,
    p_terminal_lease_payload_retention_days integer DEFAULT NULL,
    p_usage_rollup_retention_days integer DEFAULT NULL,
    p_outbox_delivered_retention_days integer DEFAULT NULL,
    p_outbox_max_retention_days integer DEFAULT NULL,
    p_maintenance_interval_seconds integer DEFAULT NULL,
    p_maintenance_batch_size integer DEFAULT NULL,
    p_maintenance_lock_timeout_ms integer DEFAULT NULL
)
RETURNS bursar.storage_settings
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_current bursar.storage_settings;
    v_result bursar.storage_settings;
    v_required_quota_seconds bigint;
    v_retention_seconds bigint;
    v_lateness_seconds integer;
    v_correction_days integer;
    v_safety_days integer;
BEGIN
    SELECT *
    INTO v_current
    FROM bursar.storage_settings
    WHERE singleton
    FOR UPDATE;

    v_retention_seconds :=
        COALESCE(
            p_quota_event_retention_days,
            v_current.quota_event_retention_days
        ) * 86400::bigint;
    v_lateness_seconds :=
        COALESCE(
            p_quota_max_lateness_seconds,
            v_current.quota_max_lateness_seconds
        );
    v_correction_days :=
        COALESCE(
            p_quota_correction_window_days,
            v_current.quota_correction_window_days
        );
    v_safety_days :=
        COALESCE(
            p_quota_retention_safety_days,
            v_current.quota_retention_safety_days
        );

    SELECT COALESCE(max(
        bursar.policy_duration_seconds(quota.window_policy)
    ), 0)
    INTO v_required_quota_seconds
    FROM bursar.catalog_plan_quotas AS quota;

    v_required_quota_seconds :=
        v_required_quota_seconds
        + v_lateness_seconds
        + v_correction_days * 86400::bigint
        + v_safety_days * 86400::bigint;

    IF v_required_quota_seconds > v_retention_seconds THEN
        RAISE EXCEPTION
            'configured quota retention is shorter than published rolling windows'
            USING ERRCODE = '23514',
                  DETAIL = format(
                      'required=%s seconds configured=%s seconds',
                      v_required_quota_seconds,
                      v_retention_seconds
                  );
    END IF;

    UPDATE bursar.storage_settings
    SET usage_payload_retention_days = COALESCE(
            p_usage_payload_retention_days,
            usage_payload_retention_days
        ),
        quota_event_retention_days = COALESCE(
            p_quota_event_retention_days,
            quota_event_retention_days
        ),
        quota_max_lateness_seconds = COALESCE(
            p_quota_max_lateness_seconds,
            quota_max_lateness_seconds
        ),
        quota_correction_window_days = COALESCE(
            p_quota_correction_window_days,
            quota_correction_window_days
        ),
        quota_retention_safety_days = COALESCE(
            p_quota_retention_safety_days,
            quota_retention_safety_days
        ),
        billing_payload_retention_days = COALESCE(
            p_billing_payload_retention_days,
            billing_payload_retention_days
        ),
        quota_notification_retention_days = COALESCE(
            p_quota_notification_retention_days,
            quota_notification_retention_days
        ),
        terminal_lease_payload_retention_days = COALESCE(
            p_terminal_lease_payload_retention_days,
            terminal_lease_payload_retention_days
        ),
        usage_rollup_retention_days = COALESCE(
            p_usage_rollup_retention_days,
            usage_rollup_retention_days
        ),
        outbox_delivered_retention_days = COALESCE(
            p_outbox_delivered_retention_days,
            outbox_delivered_retention_days
        ),
        outbox_max_retention_days = COALESCE(
            p_outbox_max_retention_days,
            outbox_max_retention_days
        ),
        maintenance_interval_seconds = COALESCE(
            p_maintenance_interval_seconds,
            maintenance_interval_seconds
        ),
        maintenance_batch_size = COALESCE(
            p_maintenance_batch_size,
            maintenance_batch_size
        ),
        maintenance_lock_timeout_ms = COALESCE(
            p_maintenance_lock_timeout_ms,
            maintenance_lock_timeout_ms
        )
    WHERE singleton
    RETURNING * INTO v_result;

    RETURN v_result;
END
$$;

CREATE FUNCTION bursar.claim_outbox_events(
    p_limit integer DEFAULT 100,
    p_lease_seconds integer DEFAULT 60,
    p_topics text [] DEFAULT NULL
)
RETURNS TABLE (
    event_id bigint,
    tenant_id uuid,
    topic text,
    aggregate_type text,
    aggregate_id uuid,
    payload_version smallint,
    payload jsonb,
    claim_token uuid,
    attempt_count integer,
    created_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_token uuid := gen_random_uuid();
BEGIN
    IF p_limit IS NULL
       OR p_lease_seconds IS NULL
       OR p_limit NOT BETWEEN 1 AND 1000
       OR p_lease_seconds NOT BETWEEN 1 AND 3600
       OR (
           p_topics IS NOT NULL
           AND (
               cardinality(p_topics) NOT BETWEEN 1 AND 64
               OR EXISTS (
                   SELECT 1
                   FROM unnest(p_topics) AS requested(topic)
                   WHERE requested.topic IS NULL
                      OR NOT bursar.is_nonempty_text(requested.topic)
                      OR NOT bursar.is_bounded_text(requested.topic, 255)
               )
           )
       )
    THEN
        RAISE EXCEPTION 'invalid outbox claim request'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH claimed AS (
        SELECT outbox.id
        FROM bursar.event_outbox AS outbox
        WHERE outbox.status IN ('pending', 'processing')
          AND CASE outbox.status
              WHEN 'pending' THEN outbox.available_at
              WHEN 'processing' THEN outbox.claim_expires_at
          END <= now()
          AND (p_topics IS NULL OR outbox.topic = ANY(p_topics))
        ORDER BY
            CASE outbox.status
                WHEN 'pending' THEN outbox.available_at
                WHEN 'processing' THEN outbox.claim_expires_at
            END,
            outbox.created_at,
            outbox.id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE bursar.event_outbox AS outbox
    SET status = 'processing',
        claim_token = v_token,
        claim_expires_at = now() + make_interval(secs => p_lease_seconds),
        attempt_count = outbox.attempt_count + 1,
        last_error = NULL
    FROM claimed
    WHERE outbox.id = claimed.id
    RETURNING
        outbox.id,
        outbox.tenant_id,
        outbox.topic,
        outbox.aggregate_type,
        outbox.aggregate_id,
        outbox.payload_version,
        outbox.payload,
        outbox.claim_token,
        outbox.attempt_count,
        outbox.created_at;
END
$$;

CREATE FUNCTION bursar.claim_outbox_events(
    p_tenant_id uuid,
    p_limit integer DEFAULT 100,
    p_lease_seconds integer DEFAULT 60,
    p_topics text [] DEFAULT NULL
)
RETURNS TABLE (
    event_id bigint,
    tenant_id uuid,
    topic text,
    aggregate_type text,
    aggregate_id uuid,
    payload_version smallint,
    payload jsonb,
    claim_token uuid,
    attempt_count integer,
    created_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_token uuid := gen_random_uuid();
BEGIN
    IF p_tenant_id IS NULL
       OR p_limit IS NULL
       OR p_lease_seconds IS NULL
       OR p_limit NOT BETWEEN 1 AND 1000
       OR p_lease_seconds NOT BETWEEN 1 AND 3600
       OR (
           p_topics IS NOT NULL
           AND (
               cardinality(p_topics) NOT BETWEEN 1 AND 64
               OR EXISTS (
                   SELECT 1
                   FROM unnest(p_topics) AS requested(topic)
                   WHERE requested.topic IS NULL
                      OR NOT bursar.is_nonempty_text(requested.topic)
                      OR NOT bursar.is_bounded_text(requested.topic, 255)
               )
           )
       )
    THEN
        RAISE EXCEPTION 'invalid tenant outbox claim request'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH claimed AS (
        SELECT outbox.id
        FROM bursar.event_outbox AS outbox
        JOIN bursar.tenants AS tenant
          ON tenant.id = outbox.tenant_id
         AND tenant.status = 'active'
        WHERE outbox.tenant_id = p_tenant_id
          AND outbox.status IN ('pending', 'processing')
          AND CASE outbox.status
              WHEN 'pending' THEN outbox.available_at
              WHEN 'processing' THEN outbox.claim_expires_at
          END <= now()
          AND (p_topics IS NULL OR outbox.topic = ANY(p_topics))
        ORDER BY
            CASE outbox.status
                WHEN 'pending' THEN outbox.available_at
                WHEN 'processing' THEN outbox.claim_expires_at
            END,
            outbox.created_at,
            outbox.id
        FOR UPDATE OF outbox SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE bursar.event_outbox AS outbox
    SET status = 'processing',
        claim_token = v_token,
        claim_expires_at = now() + make_interval(secs => p_lease_seconds),
        attempt_count = outbox.attempt_count + 1,
        last_error = NULL
    FROM claimed
    WHERE outbox.id = claimed.id
    RETURNING
        outbox.id,
        outbox.tenant_id,
        outbox.topic,
        outbox.aggregate_type,
        outbox.aggregate_id,
        outbox.payload_version,
        outbox.payload,
        outbox.claim_token,
        outbox.attempt_count,
        outbox.created_at;
END
$$;

CREATE FUNCTION bursar.export_usage_charge(p_charge_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        (
            to_jsonb(charge)
            - 'requested'
            - 'charged'
            - 'allowance_requested'
            - 'allowance_covered'
        )
        || jsonb_build_object(
            'tenant_id', charge.tenant_id,
            'charge_id', charge.id,
            'subject_id', account.subject_id,
            'requested', charge.requested::text,
            'charged', charge.charged::text,
            'allowance_requested', charge.allowance_requested::text,
            'allowance_covered', charge.allowance_covered::text,
            'payload_available', payload.charge_id IS NOT NULL,
            'measures', payload.measures,
            'feature', payload.feature,
            'model', payload.model,
            'region', payload.region,
            'dimensions', payload.dimensions,
            'metadata', payload.metadata,
            'pricing_snapshot', payload.pricing_snapshot
        )
    FROM bursar.credit_usage_charges AS charge
    JOIN bursar.credit_accounts AS account
      ON account.id = charge.account_id
     AND account.tenant_id = charge.tenant_id
    LEFT JOIN bursar.usage_charge_payloads AS payload
      ON payload.charge_id = charge.id
     AND payload.event_at = charge.event_at
     AND payload.tenant_id = charge.tenant_id
    WHERE charge.id = p_charge_id
$$;

CREATE FUNCTION bursar.export_billing_event_payload(p_event_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT jsonb_build_object(
        'tenant_id', event.tenant_id,
        'event_id', event.id,
        'provider', event.provider,
        'provider_environment', event.provider_environment,
        'provider_event_id', event.provider_event_id,
        'event_type', event.event_type,
        'status', event.status,
        'received_at', event.payload_received_at,
        'completed_at', event.completed_at,
        'envelope', payload.envelope,
        'object_key', event.payload_object_key,
        'object_version', event.payload_object_version,
        'archived_at', event.payload_archived_at
    )
    FROM bursar.billing_events AS event
    LEFT JOIN bursar.billing_event_payloads AS payload
      ON payload.event_id = event.id
     AND payload.received_at = event.payload_received_at
     AND payload.tenant_id = event.tenant_id
    WHERE event.id = p_event_id
$$;

CREATE FUNCTION bursar.complete_outbox_event(
    p_event_id bigint,
    p_claim_token uuid
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    UPDATE bursar.event_outbox
    SET status = 'delivered',
        claim_token = NULL,
        claim_expires_at = NULL,
        delivered_at = now()
    WHERE id = p_event_id
      AND status = 'processing'
      AND claim_token = p_claim_token
      AND claim_expires_at > now()
    RETURNING true
$$;

CREATE FUNCTION bursar.archive_billing_event_payload(
    p_event_id uuid,
    p_object_key text,
    p_object_version text DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_event bursar.billing_events;
BEGIN
    IF p_event_id IS NULL
       OR NOT bursar.is_nonempty_bounded_text(p_object_key, 2048)
       OR (
           p_object_version IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_object_version, 1024)
       )
    THEN
        RAISE EXCEPTION 'invalid billing payload archive request'
            USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO v_event
    FROM bursar.billing_events
    WHERE id = p_event_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    UPDATE bursar.billing_events
    SET payload_archived_at = COALESCE(payload_archived_at, now()),
        payload_object_key = p_object_key,
        payload_object_version = p_object_version
    WHERE id = p_event_id;

    DELETE FROM bursar.billing_event_payloads
    WHERE event_id = p_event_id
      AND received_at = v_event.payload_received_at;

    RETURN true;
END
$$;

CREATE FUNCTION bursar.fail_outbox_event(
    p_event_id bigint,
    p_claim_token uuid,
    p_error text,
    p_retry_delay_seconds integer DEFAULT 30,
    p_attempt_limit integer DEFAULT 10
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_updated boolean;
BEGIN
    IF p_event_id IS NULL
       OR p_claim_token IS NULL
       OR p_retry_delay_seconds IS NULL
       OR p_attempt_limit IS NULL
       OR p_retry_delay_seconds NOT BETWEEN 0 AND 86400
       OR p_attempt_limit NOT BETWEEN 1 AND 100
       OR NOT bursar.is_nonempty_bounded_text(p_error, 8192)
    THEN
        RAISE EXCEPTION 'invalid outbox failure request'
            USING ERRCODE = '22023';
    END IF;

    UPDATE bursar.event_outbox
    SET status = CASE
            WHEN attempt_count >= p_attempt_limit THEN 'dead_letter'
            ELSE 'pending'
        END,
        claim_token = NULL,
        claim_expires_at = NULL,
        available_at = now() + make_interval(secs => p_retry_delay_seconds),
        last_error = p_error
    WHERE id = p_event_id
      AND status = 'processing'
      AND claim_token = p_claim_token
      AND claim_expires_at > now()
    RETURNING true INTO v_updated;

    RETURN COALESCE(v_updated, false);
END
$$;

CREATE FUNCTION bursar.run_storage_partition_maintenance(
    p_parent_table text,
    p_now timestamptz DEFAULT now()
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
SET timezone TO 'UTC'
AS $$
DECLARE
    v_settings bursar.storage_settings;
    v_parent regclass;
    v_parent_name text;
    v_retention interval;
    v_partitions_before integer := 0;
    v_partitions_after_create integer := 0;
    v_partitions_created integer := 0;
    v_partitions_dropped integer := 0;
    v_lock_timeouts integer := 0;
    v_previous_lock_timeout text;
    v_partition regclass;
    v_default_partition regclass;
    v_default_has_rows boolean := false;
BEGIN
    IF p_parent_table IS NULL
       OR p_parent_table NOT IN (
        'usage_charge_payloads',
        'billing_event_payloads'
    )
       OR p_now IS NULL
    THEN
        RAISE EXCEPTION 'invalid storage partition maintenance request'
            USING ERRCODE = '22023';
    END IF;

    IF NOT pg_try_advisory_xact_lock(
        hashtextextended(
            'bursar.storage.partition.' || p_parent_table,
            0
        )
    ) THEN
        RETURN jsonb_build_object('status', 'busy');
    END IF;

    v_parent_name := format('bursar.%I', p_parent_table);
    v_parent := v_parent_name::regclass;

    IF NOT EXISTS (
        SELECT 1
        FROM partman.part_config AS config
        WHERE config.parent_table = v_parent_name
          AND config.control = CASE p_parent_table
              WHEN 'usage_charge_payloads' THEN 'event_at'
              WHEN 'billing_event_payloads' THEN 'received_at'
          END
          AND config.partition_interval::interval = interval '1 month'
          AND config.partition_type = 'range'
          AND config.premake = 4
          AND config.automatic_maintenance = 'off'
          AND config.retention IS NULL
          AND config.retention_schema IS NULL
          AND NOT config.retention_keep_table
          AND NOT config.retention_keep_index
          AND config.infinite_time_partitions
          AND config.ignore_default_data
          AND NOT config.inherit_privileges
          AND NOT config.jobmon
    ) THEN
        RAISE EXCEPTION 'pg_partman storage configuration drift for %',
            v_parent_name
            USING ERRCODE = '55000';
    END IF;

    SELECT *
    INTO v_settings
    FROM bursar.storage_settings
    WHERE singleton;

    v_retention := make_interval(
        days => CASE p_parent_table
            WHEN 'usage_charge_payloads'
                THEN v_settings.usage_payload_retention_days
            WHEN 'billing_event_payloads'
                THEN v_settings.billing_payload_retention_days
        END
    );

    SELECT count(*)::integer
    INTO v_partitions_before
    FROM pg_inherits AS inheritance
    JOIN pg_class AS child
      ON child.oid = inheritance.inhrelid
    WHERE inheritance.inhparent = v_parent
      AND pg_get_expr(child.relpartbound, child.oid) <> 'DEFAULT';

    v_previous_lock_timeout := current_setting('lock_timeout');
    PERFORM set_config(
        'lock_timeout',
        v_settings.maintenance_lock_timeout_ms::text || 'ms',
        true
    );

    -- pg_partman pre-creates the configured horizon. Retention remains an
    -- explicit second call so p_now is deterministic and Bursar's settings
    -- remain the single public policy surface.
    BEGIN
        PERFORM partman.run_maintenance(
            v_parent_name,
            false,
            false
        );
    EXCEPTION
        WHEN OTHERS THEN
            -- pg_partman 5.3 re-raises lock_not_available as P0001 while
            -- preserving PostgreSQL's exact message. Do not hide any other
            -- extension failure.
            IF SQLSTATE = '55P03'
               OR (
                   SQLSTATE = 'P0001'
                   AND SQLERRM LIKE 'canceling statement due to lock timeout%'
               )
            THEN
                v_lock_timeouts := v_lock_timeouts + 1;
            ELSE
                RAISE;
            END IF;
    END;

    SELECT count(*)::integer
    INTO v_partitions_after_create
    FROM pg_inherits AS inheritance
    JOIN pg_class AS child
      ON child.oid = inheritance.inhrelid
    WHERE inheritance.inhparent = v_parent
      AND pg_get_expr(child.relpartbound, child.oid) <> 'DEFAULT';

    v_partitions_created := greatest(
        v_partitions_after_create - v_partitions_before,
        0
    );

    BEGIN
        SELECT partman.drop_partition_time(
            v_parent_name,
            v_retention,
            false,
            false,
            NULL,
            p_now
        )
        INTO v_partitions_dropped;
    EXCEPTION
        WHEN OTHERS THEN
            IF SQLSTATE = '55P03'
               OR (
                   SQLSTATE = 'P0001'
                   AND SQLERRM LIKE 'canceling statement due to lock timeout%'
               )
            THEN
                v_lock_timeouts := v_lock_timeouts + 1;
            ELSE
                RAISE;
            END IF;
    END;

    -- pg_partman deliberately does not know Bursar's tenant policy. Harden
    -- every new child before returning from operator maintenance.
    IF to_regprocedure(
        'bursar.secure_tenant_partition(regclass)'
    ) IS NOT NULL THEN
        FOR v_partition IN
            SELECT child.oid::regclass
            FROM pg_inherits AS inheritance
            JOIN pg_class AS child
              ON child.oid = inheritance.inhrelid
            WHERE inheritance.inhparent = v_parent
              AND (
                  NOT child.relrowsecurity
                  OR NOT child.relforcerowsecurity
                  OR NOT EXISTS (
                      SELECT 1
                      FROM pg_policy AS policy
                      WHERE policy.polrelid = child.oid
                        AND policy.polname LIKE 'tenant_isolation_%'
                  )
              )
            ORDER BY child.oid
        LOOP
            -- Security is fail-closed: never commit a newly visible child
            -- unless its forced-RLS policy and direct-access revokes succeed.
            EXECUTE
                'SELECT bursar.secure_tenant_partition($1)'
                USING v_partition;
        END LOOP;
    END IF;

    SELECT child.oid::regclass
    INTO v_default_partition
    FROM pg_inherits AS inheritance
    JOIN pg_class AS child
      ON child.oid = inheritance.inhrelid
    WHERE inheritance.inhparent = v_parent
      AND pg_get_expr(child.relpartbound, child.oid) = 'DEFAULT';

    IF v_default_partition IS NOT NULL THEN
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %s LIMIT 1)',
            v_default_partition
        )
        INTO v_default_has_rows;
    END IF;

    PERFORM set_config('lock_timeout', v_previous_lock_timeout, true);

    RETURN jsonb_build_object(
        'status', 'completed',
        'parent_table', p_parent_table,
        'partitions_created', v_partitions_created,
        'partitions_dropped', v_partitions_dropped,
        'partition_lock_timeouts', v_lock_timeouts,
        'default_partition_has_rows', v_default_has_rows,
        'has_more', v_default_has_rows OR v_lock_timeouts > 0
    );
EXCEPTION
    WHEN OTHERS THEN
        IF v_previous_lock_timeout IS NOT NULL THEN
            PERFORM set_config(
                'lock_timeout',
                v_previous_lock_timeout,
                true
            );
        END IF;
        RAISE;
END
$$;

CREATE FUNCTION bursar.run_storage_maintenance(
    p_now timestamptz DEFAULT now()
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_settings bursar.storage_settings;
    v_usage_cutoff timestamptz;
    v_billing_cutoff timestamptz;
    v_usage_payloads integer := 0;
    v_record_only_usage integer := 0;
    v_billing_payloads integer := 0;
    v_quota_events integer := 0;
    v_quota_notifications integer := 0;
    v_terminal_leases integer := 0;
    v_rollups integer := 0;
    v_outbox integer := 0;
    v_outbox_hard_limit integer := 0;
    v_outbox_remaining integer := 0;
    v_has_more boolean := false;
BEGIN
    IF p_now IS NULL THEN
        RAISE EXCEPTION 'maintenance timestamp is required'
            USING ERRCODE = '22023';
    END IF;

    IF NOT pg_try_advisory_xact_lock(
        hashtextextended('bursar.storage.maintenance', 0)
    ) THEN
        RETURN jsonb_build_object('status', 'busy');
    END IF;

    SELECT *
    INTO v_settings
    FROM bursar.storage_settings
    WHERE singleton
    FOR UPDATE;

    v_usage_cutoff :=
        p_now - make_interval(
            days => v_settings.usage_payload_retention_days
        );
    v_billing_cutoff :=
        p_now - make_interval(
            days => v_settings.billing_payload_retention_days
        );

    PERFORM set_config('bursar.mutation_context', 'internal', true);

    -- Only the partial boundary month needs row deletion. Fully expired
    -- monthly partitions above were removed in constant catalog work.
    WITH candidates AS MATERIALIZED (
        SELECT payload.event_at, payload.charge_id
        FROM bursar.usage_charge_payloads AS payload
        WHERE payload.event_at < v_usage_cutoff
        ORDER BY payload.event_at, payload.charge_id
        LIMIT v_settings.maintenance_batch_size
        FOR UPDATE SKIP LOCKED
    ),
    deleted AS (
        DELETE FROM bursar.usage_charge_payloads AS payload
        USING candidates
        WHERE payload.event_at = candidates.event_at
          AND payload.charge_id = candidates.charge_id
        RETURNING payload.charge_id
    )
    SELECT count(*)::integer INTO v_usage_payloads FROM deleted;

    -- Billable receipts are permanent accounting evidence. Record-only
    -- workflow telemetry uses the same retention horizon as its detail
    -- payload and is removed in bounded batches.
    WITH candidates AS MATERIALIZED (
        SELECT charge.event_at, charge.id
        FROM bursar.credit_usage_charges AS charge
        WHERE charge.billing_disposition = 'record_only'
          AND charge.event_at < v_usage_cutoff
        ORDER BY charge.event_at, charge.id
        LIMIT v_settings.maintenance_batch_size
        FOR UPDATE SKIP LOCKED
    ),
    deleted AS (
        DELETE FROM bursar.credit_usage_charges AS charge
        USING candidates
        WHERE charge.event_at = candidates.event_at
          AND charge.id = candidates.id
        RETURNING charge.id
    )
    SELECT count(*)::integer INTO v_record_only_usage FROM deleted;

    WITH candidates AS MATERIALIZED (
        SELECT payload.received_at, payload.event_id
        FROM bursar.billing_event_payloads AS payload
        WHERE payload.received_at < v_billing_cutoff
        ORDER BY payload.received_at, payload.event_id
        LIMIT v_settings.maintenance_batch_size
        FOR UPDATE SKIP LOCKED
    ),
    deleted AS (
        DELETE FROM bursar.billing_event_payloads AS payload
        USING candidates
        WHERE payload.received_at = candidates.received_at
          AND payload.event_id = candidates.event_id
        RETURNING payload.event_id
    )
    SELECT count(*)::integer INTO v_billing_payloads FROM deleted;

    WITH candidates AS MATERIALIZED (
        SELECT event.id
        FROM bursar.quota_events AS event
        WHERE event.created_at
              < p_now - make_interval(
                  days => v_settings.quota_notification_retention_days
              )
        ORDER BY event.created_at, event.id
        LIMIT v_settings.maintenance_batch_size
        FOR UPDATE SKIP LOCKED
    ),
    deleted AS (
        DELETE FROM bursar.quota_events AS event
        USING candidates
        WHERE event.id = candidates.id
        RETURNING event.id
    )
    SELECT count(*)::integer INTO v_quota_notifications FROM deleted;

    -- Corrections are selected before originals. An original still referenced
    -- by a correction becomes eligible on a later bounded pass.
    WITH candidates AS MATERIALIZED (
        SELECT event.id
        FROM bursar.quota_usage_events AS event
        WHERE event.event_at
              < p_now - make_interval(
                  days => v_settings.quota_event_retention_days
              )
          AND (
              event.correction_of_event_id IS NOT NULL
              OR NOT EXISTS (
                  SELECT 1
                  FROM bursar.quota_usage_events AS correction
                  WHERE correction.correction_of_event_id = event.id
              )
          )
        ORDER BY
            (event.correction_of_event_id IS NULL),
            event.event_at,
            event.id
        LIMIT v_settings.maintenance_batch_size
        FOR UPDATE SKIP LOCKED
    ),
    deleted AS (
        DELETE FROM bursar.quota_usage_events AS event
        USING candidates
        WHERE event.id = candidates.id
        RETURNING event.id
    )
    SELECT count(*)::integer INTO v_quota_events FROM deleted;

    WITH candidates AS MATERIALIZED (
        SELECT lease.id
        FROM bursar.credit_leases AS lease
        WHERE lease.status IN ('settled', 'released', 'expired')
          AND lease.updated_at
              < p_now - make_interval(
                  days =>
                      v_settings.terminal_lease_payload_retention_days
              )
          AND (
              lease.dimensions <> '{}'::jsonb
              OR lease.metadata <> '{}'::jsonb
          )
        ORDER BY lease.updated_at, lease.id
        LIMIT v_settings.maintenance_batch_size
        FOR UPDATE SKIP LOCKED
    ),
    compacted AS (
        UPDATE bursar.credit_leases AS lease
        SET dimensions = '{}'::jsonb,
            metadata = '{}'::jsonb
        FROM candidates
        WHERE lease.id = candidates.id
        RETURNING lease.id
    )
    SELECT count(*)::integer INTO v_terminal_leases FROM compacted;

    WITH candidates AS MATERIALIZED (
        SELECT
            rollup.usage_day,
            rollup.account_id,
            rollup.operation,
            rollup.model_key,
            rollup.region_key,
            rollup.rollup_shard
        FROM bursar.usage_daily_rollups AS rollup
        WHERE rollup.usage_day
              < (
                  p_now - make_interval(
                      days => v_settings.usage_rollup_retention_days
                  )
              )::date
        ORDER BY
            rollup.usage_day,
            rollup.account_id,
            rollup.operation,
            rollup.model_key,
            rollup.region_key,
            rollup.rollup_shard
        LIMIT v_settings.maintenance_batch_size
        FOR UPDATE SKIP LOCKED
    ),
    deleted AS (
        DELETE FROM bursar.usage_daily_rollups AS rollup
        USING candidates
        WHERE rollup.usage_day = candidates.usage_day
          AND rollup.account_id = candidates.account_id
          AND rollup.operation = candidates.operation
          AND rollup.model_key = candidates.model_key
          AND rollup.region_key = candidates.region_key
          AND rollup.rollup_shard = candidates.rollup_shard
        RETURNING rollup.usage_day
    )
    SELECT count(*)::integer INTO v_rollups FROM deleted;

    -- Undelivered payloads are retained for replay. Operators explicitly
    -- abandon dead letters after confirming the external archive is complete;
    -- automatic retention must not silently destroy the only external copy.
    WITH candidates AS MATERIALIZED (
        SELECT outbox.id
        FROM bursar.event_outbox AS outbox
        WHERE (
                outbox.status = 'delivered'
                OR outbox.payload->>'delivery_required' IS DISTINCT FROM 'true'
              )
          AND outbox.created_at
              < p_now - make_interval(
                  days => v_settings.outbox_max_retention_days
              )
        ORDER BY outbox.created_at, outbox.id
        LIMIT v_settings.maintenance_batch_size
        FOR UPDATE SKIP LOCKED
    ),
    deleted AS (
        DELETE FROM bursar.event_outbox AS outbox
        USING candidates
        WHERE outbox.id = candidates.id
        RETURNING outbox.id
    )
    SELECT count(*)::integer INTO v_outbox_hard_limit FROM deleted;

    v_outbox_remaining :=
        v_settings.maintenance_batch_size - v_outbox_hard_limit;

    WITH candidates AS MATERIALIZED (
        SELECT outbox.id
        FROM bursar.event_outbox AS outbox
        WHERE outbox.status = 'delivered'
          AND outbox.delivered_at
              < p_now - make_interval(
                  days => v_settings.outbox_delivered_retention_days
              )
        ORDER BY outbox.delivered_at, outbox.id
        LIMIT v_outbox_remaining
        FOR UPDATE SKIP LOCKED
    ),
    deleted AS (
        DELETE FROM bursar.event_outbox AS outbox
        USING candidates
        WHERE outbox.id = candidates.id
        RETURNING outbox.id
    )
    SELECT v_outbox_hard_limit + count(*)::integer
    INTO v_outbox
    FROM deleted;

    -- Equality is a conservative backlog signal. It can cause one harmless
    -- extra pass when a table had exactly one full batch.
    v_has_more :=
        v_usage_payloads = v_settings.maintenance_batch_size
        OR v_record_only_usage = v_settings.maintenance_batch_size
        OR v_billing_payloads = v_settings.maintenance_batch_size
        OR v_quota_events = v_settings.maintenance_batch_size
        OR v_quota_notifications = v_settings.maintenance_batch_size
        OR v_terminal_leases = v_settings.maintenance_batch_size
        OR v_rollups = v_settings.maintenance_batch_size
        OR v_outbox = v_settings.maintenance_batch_size;

    UPDATE bursar.storage_settings
    SET last_maintenance_at = p_now
    WHERE singleton;

    RETURN jsonb_build_object(
        'status', 'completed',
        'batch_size', v_settings.maintenance_batch_size,
        'has_more', v_has_more,
        'usage_payloads_purged', v_usage_payloads,
        'record_only_usage_purged', v_record_only_usage,
        'billing_payloads_purged', v_billing_payloads,
        'quota_usage_events_purged', v_quota_events,
        'quota_notifications_purged', v_quota_notifications,
        'terminal_leases_compacted', v_terminal_leases,
        'usage_rollups_purged', v_rollups,
        'outbox_events_purged', v_outbox
    );
END
$$;

CREATE FUNCTION bursar.maybe_run_storage_maintenance(
    p_now timestamptz DEFAULT now()
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_settings bursar.storage_settings;
BEGIN
    IF p_now IS NULL THEN
        RAISE EXCEPTION 'maintenance timestamp is required'
            USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO v_settings
    FROM bursar.storage_settings
    WHERE singleton;

    IF v_settings.last_maintenance_at IS NOT NULL
       AND p_now
           < v_settings.last_maintenance_at
             + make_interval(
                 secs => v_settings.maintenance_interval_seconds
             )
    THEN
        RETURN jsonb_build_object(
            'status', 'not_due',
            'last_maintenance_at', v_settings.last_maintenance_at,
            'next_maintenance_at',
                v_settings.last_maintenance_at
                + make_interval(
                    secs => v_settings.maintenance_interval_seconds
                )
        );
    END IF;

    RETURN bursar.run_storage_maintenance(p_now);
END
$$;

-- Register the two native partitioned parents with pg_partman. Version 5.4
-- renamed create_parent() to create_partition(); the named arguments used by
-- Bursar are otherwise common to the audited 5.x APIs.
DO $$
DECLARE
    v_partition_set record;
    v_create_function text;
BEGIN
    PERFORM set_config('TimeZone', 'UTC', true);

    SELECT CASE
        WHEN EXISTS (
            SELECT 1
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            WHERE namespace_info.nspname = 'partman'
              AND function_info.proname = 'create_partition'
        ) THEN 'create_partition'
        WHEN EXISTS (
            SELECT 1
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            WHERE namespace_info.nspname = 'partman'
              AND function_info.proname = 'create_parent'
        ) THEN 'create_parent'
    END
    INTO v_create_function;

    IF v_create_function IS NULL THEN
        RAISE EXCEPTION 'unsupported pg_partman 5.x creation API'
            USING ERRCODE = '0A000';
    END IF;

    FOR v_partition_set IN
        SELECT *
        FROM (
            VALUES
                ('bursar.usage_charge_payloads', 'event_at'),
                ('bursar.billing_event_payloads', 'received_at')
        ) AS partition_set(parent_table, control_column)
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM partman.part_config AS config
            WHERE config.parent_table = v_partition_set.parent_table
        ) THEN
            EXECUTE format(
                'SELECT partman.%I('
                'p_parent_table := $1, '
                'p_control := $2, '
                'p_interval := ''1 month'', '
                'p_type := ''range'', '
                'p_premake := 4, '
                'p_default_table := true, '
                'p_automatic_maintenance := ''off'', '
                'p_jobmon := false, '
                'p_date_trunc_interval := ''month'''
                ')',
                v_create_function
            )
            USING
                v_partition_set.parent_table,
                v_partition_set.control_column;
        END IF;

        UPDATE partman.part_config
        SET premake = 4,
            automatic_maintenance = 'off',
            retention = NULL,
            retention_schema = NULL,
            retention_keep_table = false,
            retention_keep_index = false,
            infinite_time_partitions = true,
            ignore_default_data = true,
            inherit_privileges = false,
            jobmon = false
        WHERE parent_table = v_partition_set.parent_table
          AND control = v_partition_set.control_column
          AND partition_interval::interval = interval '1 month'
          AND partition_type = 'range';

        IF NOT FOUND THEN
            RAISE EXCEPTION 'pg_partman storage configuration drift for %',
                v_partition_set.parent_table
                USING ERRCODE = '55000';
        END IF;
    END LOOP;
END
$$;

COMMENT ON FUNCTION bursar.get_storage_settings()
IS 'Return PostgreSQL hot-storage retention and maintenance settings.';
COMMENT ON FUNCTION bursar.configure_storage(
    integer, integer, integer, integer, integer, integer,
    integer, integer, integer, integer, integer, integer,
    integer, integer
)
IS 'Configure bounded PostgreSQL event retention and maintenance work budgets while preserving quota correctness.';
COMMENT ON FUNCTION bursar.claim_outbox_events(integer, integer, text [])
IS 'Claim a bounded cross-tenant batch and return each event tenant UUID.';
COMMENT ON FUNCTION bursar.claim_outbox_events(
    uuid, integer, integer, text []
)
IS 'Claim a bounded outbox batch for one active tenant.';
COMMENT ON FUNCTION bursar.export_usage_charge(uuid)
IS 'Return one usage charge projection for an external analytics sink.';
COMMENT ON FUNCTION bursar.export_billing_event_payload(uuid)
IS 'Return one billing event envelope and its current archive pointer.';
COMMENT ON FUNCTION bursar.complete_outbox_event(bigint, uuid)
IS 'Acknowledge one claimed outbox event as delivered.';
COMMENT ON FUNCTION bursar.archive_billing_event_payload(
    uuid, text, text
)
IS 'Record an external webhook-envelope object and purge its PostgreSQL payload.';
COMMENT ON FUNCTION bursar.fail_outbox_event(bigint, uuid, text, integer, integer)
IS 'Release or dead-letter one claimed outbox event after delivery failure.';
COMMENT ON FUNCTION bursar.run_storage_partition_maintenance(
    text, timestamptz
)
IS 'Run pg_partman creation and retention for one Bursar payload partition set.';
COMMENT ON FUNCTION bursar.run_storage_maintenance(timestamptz)
IS 'Perform one bounded row-retention pass, including record-only usage telemetry, without partition DDL.';
COMMENT ON FUNCTION bursar.maybe_run_storage_maintenance(timestamptz)
IS 'Run one bounded retention pass only when the configured interval has elapsed.';

REVOKE ALL ON FUNCTION bursar.get_storage_settings() FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.configure_storage(
    integer, integer, integer, integer, integer, integer,
    integer, integer, integer, integer, integer, integer,
    integer, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.claim_outbox_events(integer, integer, text []) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.claim_outbox_events(
    uuid, integer, integer, text []
) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.export_usage_charge(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.export_billing_event_payload(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.complete_outbox_event(bigint, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.archive_billing_event_payload(
    uuid, text, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.fail_outbox_event(
    bigint, uuid, text, integer, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.run_storage_partition_maintenance(
    text, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.run_storage_maintenance(timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.maybe_run_storage_maintenance(timestamptz)
FROM PUBLIC;
