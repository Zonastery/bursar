-- Account creation, account-created grant programs, and credit posting.

CREATE FUNCTION bursar.account_for_subject(
    p_subject_id uuid,
    p_kind text DEFAULT 'personal'
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_id uuid;
    v_created boolean := false;
    v_revision uuid;
    v_program_key text;
BEGIN
    -- Canonicalize short ISO-like region codes once. The raw-size guard keeps
    -- pathological inputs out of upper()/btrim() while still accepting modest
    -- surrounding whitespace from HTTP clients.
    v_region := CASE
        WHEN p_region IS NULL OR octet_length(p_region) > 16 THEN NULL
        ELSE upper(btrim(p_region))
    END;

    IF p_subject_id IS NULL
       OR p_kind IS NULL
       OR p_kind NOT IN ('personal', 'team')
    THEN
        RAISE EXCEPTION 'invalid account subject or kind'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO bursar.subjects(id)
    VALUES (p_subject_id)
    ON CONFLICT (tenant_id, id) DO NOTHING;

    IF bursar.is_subject_pseudonymized(p_subject_id) THEN
        RAISE EXCEPTION 'subject is pseudonymized'
            USING ERRCODE = '55000';
    END IF;

    INSERT INTO bursar.credit_accounts(subject_id, account_kind)
    VALUES (p_subject_id, p_kind)
    ON CONFLICT (tenant_id, subject_id, account_kind) DO NOTHING
    RETURNING id INTO v_id;

    v_created := FOUND;

    IF NOT v_created THEN
        SELECT id
        INTO v_id
        FROM bursar.credit_accounts
        WHERE subject_id = p_subject_id
          AND account_kind = p_kind
        FOR UPDATE;
    END IF;

    IF NOT v_created OR p_kind <> 'personal' THEN
        RETURN v_id;
    END IF;

    SELECT id
    INTO v_revision
    FROM bursar.catalog_revisions
    WHERE status = 'active';

    IF v_revision IS NULL THEN
        RETURN v_id;
    END IF;

    FOR v_program_key IN
        SELECT program.program_key
        FROM bursar.catalog_grant_programs AS program
        WHERE program.catalog_revision_id = v_revision
          AND program.trigger_type = 'account_created'
        ORDER BY program.program_key, program.id
    LOOP
        PERFORM result.grant_event_id
        FROM bursar.execute_grant_program(
            'account_created',
            v_program_key,
            p_subject_id,
            p_subject_id::text,
            NULL,
            NULL,
            jsonb_build_object('source', 'account_provisioning')
        ) AS result;
    END LOOP;

    RETURN v_id;
END
$$;

CREATE FUNCTION bursar.execute_grant_program(
    p_trigger_type text,
    p_program_key text,
    p_subject_id uuid,
    p_event_key text,
    p_referrer_subject_id uuid DEFAULT NULL,
    p_region text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    grant_event_id uuid,
    grant_award_id uuid,
    recipient_subject_id uuid,
    ledger_entry_id uuid,
    amount numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_revision uuid;
    v_program bursar.catalog_grant_programs;
    v_event bursar.grant_program_events;
    v_award record;
    v_post record;
    v_account uuid;
    v_recipient uuid;
    v_plan_key text;
    v_idempotency_key text;
    v_award_count integer;
    v_expiry_policy jsonb;
    v_expires_at timestamptz;
    v_region text;
    v_event_metadata jsonb;
BEGIN
    IF p_subject_id IS NULL
       OR p_trigger_type IS NULL
       OR p_trigger_type NOT IN (
           'account_created',
           'referral_completed',
           'promo_code_redeemed',
           'manual'
       )
       OR NOT bursar.is_nonempty_text(p_program_key)
       OR NOT bursar.is_nonempty_text(p_event_key)
       OR NOT bursar.is_bounded_json_object(
           COALESCE(p_metadata, '{}'::jsonb),
           16384
       )
       OR (
           p_region IS NOT NULL
           AND (
               octet_length(p_region) > 16
               OR v_region !~ '^[A-Z]{2,3}$'
           )
       )
       OR (
           p_trigger_type = 'referral_completed'
           AND p_referrer_subject_id IS NULL
       )
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            false,
            'invalid_request';
        RETURN;
    END IF;

    v_event_metadata := COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_build_object('region', v_region);
    IF NOT bursar.is_bounded_json_object(v_event_metadata, 16384) THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            false,
            'invalid_request';
        RETURN;
    END IF;

    SELECT id
    INTO v_revision
    FROM bursar.catalog_revisions
    WHERE status = 'active';

    IF v_revision IS NULL THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            false,
            'missing_active_catalog';
        RETURN;
    END IF;

    SELECT program.*
    INTO v_program
    FROM bursar.catalog_grant_programs AS program
    WHERE program.catalog_revision_id = v_revision
      AND program.program_key = p_program_key
      AND program.trigger_type = p_trigger_type;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            false,
            'unknown_grant_program';
        RETURN;
    END IF;

    IF (
        v_program.availability->>'starts_at' IS NOT NULL
        AND (v_program.availability->>'starts_at')::timestamptz > now()
    ) OR (
        v_program.availability->>'ends_at' IS NOT NULL
        AND (v_program.availability->>'ends_at')::timestamptz <= now()
    ) OR (
        jsonb_array_length(
            COALESCE(v_program.availability->'regions', '[]'::jsonb)
        ) > 0
        AND (
            v_region IS NULL
            OR NOT (
                v_program.availability->'regions' ? v_region
            )
        )
    )
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            false,
            'grant_unavailable';
        RETURN;
    END IF;

    INSERT INTO bursar.subjects(id)
    VALUES (p_subject_id)
    ON CONFLICT (tenant_id, id) DO NOTHING;

    -- Account locking serializes the award-limit check, event insertion, and
    -- all resulting ledger mutations for a recipient.
    v_account := bursar.account_for_subject(p_subject_id);

    PERFORM 1
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    SELECT assignment.plan_key
    INTO v_plan_key
    FROM bursar.account_plan_assignments AS assignment
    WHERE assignment.account_id = v_account
      AND assignment.starts_at <= now()
      AND (
          assignment.ends_at IS NULL
          OR assignment.ends_at > now()
      );

    IF (
        jsonb_array_length(
            COALESCE(v_program.eligibility->'plans', '[]'::jsonb)
        ) > 0
        AND (
            v_plan_key IS NULL
            OR NOT (v_program.eligibility->'plans' ? v_plan_key)
        )
    ) OR (
        jsonb_array_length(
            COALESCE(v_program.eligibility->'regions', '[]'::jsonb)
        ) > 0
        AND (
            v_region IS NULL
            OR NOT (
                v_program.eligibility->'regions' ? v_region
            )
        )
    )
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            false,
            'grant_ineligible';
        RETURN;
    END IF;

    v_idempotency_key := CASE v_program.idempotency_scope
        WHEN 'subject' THEN p_subject_id::text
        ELSE p_event_key
    END;

    SELECT event.*
    INTO v_event
    FROM bursar.grant_program_events AS event
    WHERE event.program_key = v_program.program_key
      AND event.subject_id = p_subject_id
      AND event.idempotency_key = v_idempotency_key;

    IF FOUND THEN
        IF v_program.idempotency_scope = 'event'
           AND (
               v_event.event_key IS DISTINCT FROM p_event_key
               OR v_event.referrer_subject_id
                    IS DISTINCT FROM p_referrer_subject_id
               OR v_event.metadata IS DISTINCT FROM v_event_metadata
           )
        THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                NULL::uuid,
                NULL::uuid,
                NULL::uuid,
                NULL::numeric,
                false,
                'idempotency_conflict';
            RETURN;
        END IF;

        RETURN QUERY
        SELECT
            v_event.id,
            execution.catalog_grant_award_id,
            execution.recipient_subject_id,
            execution.ledger_entry_id,
            entry.amount,
            true,
            NULL::text
        FROM bursar.grant_award_executions AS execution
        JOIN bursar.credit_ledger_entries AS entry
          ON entry.id = execution.ledger_entry_id
        WHERE execution.grant_event_id = v_event.id
        ORDER BY execution.id;
        RETURN;
    END IF;

    SELECT count(*)::integer
    INTO v_award_count
    FROM bursar.grant_program_events AS event
    WHERE event.program_key = v_program.program_key
      AND event.subject_id = p_subject_id;

    IF v_award_count >= v_program.max_awards_per_subject THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            false,
            'grant_award_limit_reached';
        RETURN;
    END IF;

    -- Defer referrer provisioning until after an event-scope retry has been
    -- proven to match. A conflicting retry must not create unrelated state.
    IF p_referrer_subject_id IS NOT NULL THEN
        INSERT INTO bursar.subjects(id)
        VALUES (p_referrer_subject_id)
        ON CONFLICT (tenant_id, id) DO NOTHING;
    END IF;

    INSERT INTO bursar.grant_program_events(
        catalog_revision_id,
        grant_program_id,
        program_key,
        subject_id,
        event_key,
        idempotency_scope,
        idempotency_key,
        referrer_subject_id,
        metadata
    )
    VALUES (
        v_revision,
        v_program.id,
        v_program.program_key,
        p_subject_id,
        p_event_key,
        v_program.idempotency_scope,
        v_idempotency_key,
        p_referrer_subject_id,
        v_event_metadata
    )
    RETURNING * INTO v_event;

    FOR v_award IN
        SELECT award.*
        FROM bursar.catalog_grant_awards AS award
        WHERE award.grant_program_id = v_program.id
        ORDER BY award.award_index
    LOOP
        v_recipient := CASE v_award.recipient
            WHEN 'referrer' THEN p_referrer_subject_id
            ELSE p_subject_id
        END;

        IF v_recipient IS NULL THEN
            RAISE EXCEPTION
                'grant award requires a referrer recipient'
                USING ERRCODE = '22023';
        END IF;

        SELECT COALESCE(
            v_award.expiry_policy,
            bucket.expiry_policy
        )
        INTO v_expiry_policy
        FROM bursar.catalog_buckets AS bucket
        WHERE bucket.catalog_revision_id = v_revision
          AND bucket.bucket_key = v_award.bucket_key;

        v_expires_at := bursar.expiry_policy_at(
            v_recipient,
            v_revision,
            v_expiry_policy,
            now(),
            NULL
        );

        SELECT *
        INTO v_post
        FROM bursar.post_credit(
            v_recipient,
            'grant',
            v_award.amount,
            -- Catalog keys may consume their full 255-character budget. Use
            -- a stable digest so the derived ledger operation stays bounded
            -- without conflating long keys that share a prefix.
            'grant_program:' || encode(
                extensions.digest(
                    convert_to(v_program.program_key, 'UTF8'),
                    'sha256'
                ),
                'hex'
            ),
            concat_ws(
                ':',
                'grant-award',
                v_event.id,
                v_award.id,
                v_recipient
            ),
            v_event_metadata
                || jsonb_build_object(
                    'grant_event_id', v_event.id,
                    'grant_program_id', v_program.id,
                    'grant_award_id', v_award.id,
                    'trigger', p_trigger_type
                ),
            v_award.bucket_key,
            v_revision,
            v_expires_at,
            NULL
        );

        IF v_post.error_code IS NOT NULL THEN
            RAISE EXCEPTION 'grant program posting failed: %',
                v_post.error_code
                USING ERRCODE = '23514';
        END IF;

        INSERT INTO bursar.grant_award_executions(
            grant_event_id,
            catalog_grant_award_id,
            catalog_revision_id,
            recipient_subject_id,
            ledger_entry_id
        )
        VALUES (
            v_event.id,
            v_award.id,
            v_revision,
            v_recipient,
            v_post.entry_id
        );

        RETURN QUERY
        SELECT
            v_event.id,
            v_award.id,
            v_recipient,
            v_post.entry_id,
            v_award.amount,
            false,
            NULL::text;
    END LOOP;
END
$$;

-- Resolve the host-configured tenant before the runtime trigger binds RLS
-- context. This narrowly scoped helper retains the migration owner in the
-- multitenancy security step and is executable only by bursar_runtime.
CREATE FUNCTION bursar.resolve_active_tenant_for_trigger(
    p_tenant_slug text
)
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_tenant_id uuid;
BEGIN
    IF p_tenant_slug IS NULL
       OR length(btrim(p_tenant_slug)) NOT BETWEEN 1 AND 100
       OR lower(btrim(p_tenant_slug))
          !~ '^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$'
    THEN
        RAISE EXCEPTION 'invalid Bursar tenant slug'
            USING ERRCODE = '22023';
    END IF;

    SELECT id
    INTO v_tenant_id
    FROM bursar.tenants
    WHERE slug = lower(btrim(p_tenant_slug))
      AND status = 'active';

    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'Bursar tenant is not provisioned or active'
            USING ERRCODE = '55000';
    END IF;

    RETURN v_tenant_id;
END
$$;

COMMENT ON FUNCTION bursar.resolve_active_tenant_for_trigger(text) IS
'Internal resolver used by the host-table trigger before tenant RLS context is bound.';

-- Host applications attach this hook to their own principal table and pass
-- their provisioned tenant slug as the sole trigger argument. Bursar owns
-- tenant lookup, context binding, default-plan assignment, and signup grants.
CREATE FUNCTION bursar.provision_subject_account_on_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_tenant_id uuid;
    v_plan_id uuid;
BEGIN
    IF TG_NARGS <> 1 THEN
        RAISE EXCEPTION
            'Bursar signup trigger requires exactly one tenant slug'
            USING ERRCODE = '22023';
    END IF;

    v_tenant_id :=
        bursar.resolve_active_tenant_for_trigger(TG_ARGV[0]);

    PERFORM set_config(
        'bursar.tenant_id',
        v_tenant_id::text,
        true
    );

    -- Serialize signup with catalog activation so a new assignment is either
    -- included in the activation rollout or sees the newly active revision.
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'bursar.tenant:' || v_tenant_id::text || ':catalog.active',
            0
        )
    );

    -- A configured default is authoritative. Catalogs without one fall back
    -- to deterministic rank/key ordering.
    SELECT plan.id
    INTO v_plan_id
    FROM bursar.catalog_revisions AS revision
    JOIN bursar.catalog_plans AS plan
      ON plan.catalog_revision_id = revision.id
     AND plan.tenant_id = v_tenant_id
    WHERE revision.tenant_id = v_tenant_id
      AND revision.status = 'active'
      AND (
          NULLIF(
              revision.source_document #>> '{catalog,default_plan}',
              ''
          ) IS NULL
          OR plan.plan_key = NULLIF(
              revision.source_document #>> '{catalog,default_plan}',
              ''
          )
      )
    ORDER BY
        COALESCE((plan.definition ->> 'rank')::numeric, 0),
        plan.plan_key
    LIMIT 1;

    IF v_plan_id IS NULL THEN
        RAISE EXCEPTION 'Bursar catalog has no active default plan'
            USING ERRCODE = '55000';
    END IF;

    IF NOT bursar.assign_plan(NEW.id, v_plan_id) THEN
        RAISE EXCEPTION 'Bursar account provisioning failed'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END
$$;

COMMENT ON FUNCTION bursar.provision_subject_account_on_insert() IS
'Tenant-aware signup hook that assigns the active default plan and runs account_created grants.';

-- Return unreserved, unexpired lot credit that sorts before a configured
-- allowance priority. Account-scoped mutation RPCs hold the credit-account row
-- lock before calling this helper, so the balance and aggregate lease holds are
-- one atomic ordering snapshot.
CREATE FUNCTION bursar.available_credit_before_priority(
    p_account_id uuid,
    p_priority integer,
    p_excluded_lease_id uuid DEFAULT NULL
)
RETURNS numeric
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO ''
AS $$
    WITH available AS (
        SELECT COALESCE(sum(lot.granted - lot.consumed), 0) AS amount
        FROM bursar.credit_lots AS lot
        WHERE lot.account_id = p_account_id
          AND lot.consumed < lot.granted
          AND (lot.expires_at IS NULL OR lot.expires_at > now())
          AND p_priority IS NOT NULL
          AND lot.priority < p_priority
    ),
    holds AS (
        SELECT COALESCE(
            sum(lease.reserved_amount - lease.reserved_allowance),
            0
        ) AS amount
        FROM bursar.credit_leases AS lease
        WHERE lease.account_id = p_account_id
          AND lease.status = 'active'
          AND lease.expires_at > now()
          AND (
              p_excluded_lease_id IS NULL
              OR lease.id <> p_excluded_lease_id
          )
    )
    SELECT greatest(available.amount - holds.amount, 0)
    FROM available
    CROSS JOIN holds
$$;

CREATE FUNCTION bursar.post_credit(
    p_subject_id uuid,
    p_kind bursar.ledger_entry_kind,
    p_amount numeric,
    p_operation text,
    p_idempotency_key text,
    p_request jsonb DEFAULT '{}'::jsonb,
    p_bucket_key text DEFAULT 'default',
    p_catalog_revision_id uuid DEFAULT NULL,
    p_expires_at timestamptz DEFAULT NULL,
    p_minimum_balance numeric DEFAULT NULL
)
RETURNS TABLE (
    entry_id uuid,
    balance_after numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
    v_old bursar.credit_ledger_entries;
    v_entry uuid;
    v_balance numeric;
    v_digest bytea;
    v_available numeric;
    v_reserved numeric := 0;
    v_remaining numeric;
    v_take numeric;
    v_lot record;
    v_revision uuid;
    v_effective_expiry timestamptz;
    v_bucket_key text;
    v_bucket bursar.catalog_buckets;
    v_policy_minimum numeric := 0;
    v_lot_amount numeric;
    v_debt_repayment numeric;
    v_lot_id uuid;
    v_source_type text;
    v_debit_bucket_key text;
    v_allocation_id uuid;
    v_source record;
    v_source_remaining numeric;
    v_source_take numeric;
    v_settling_minimum numeric;
    v_requested_minimum_balance numeric := p_minimum_balance;
BEGIN
    IF p_subject_id IS NULL
       OR p_kind IS NULL
       OR NOT bursar.is_finite_numeric(p_amount)
       OR p_amount = 0
       OR NOT bursar.is_nonempty_text(p_operation)
       OR NOT bursar.is_bounded_text(p_operation, 255)
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR NOT bursar.is_bounded_text(p_idempotency_key, 255)
       -- Bucket keys are canonical catalog identifiers. NULL retains the
       -- documented default/all-buckets behavior; non-NULL values must
       -- already be trimmed and fit the catalog key budget before hashing.
       OR (
           p_bucket_key IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_bucket_key, 255)
       )
       OR (
           p_minimum_balance IS NOT NULL
           AND NOT bursar.is_finite_numeric(p_minimum_balance)
       )
       OR NOT bursar.is_bounded_json_object(
           COALESCE(p_request, '{}'::jsonb),
           16384
       )
       OR (
           p_amount > 0
           AND p_kind NOT IN (
               'grant', 'purchase', 'refund', 'release', 'adjustment'
           )
       )
       OR (
           p_amount < 0
           AND p_kind NOT IN (
               'usage',
               'expiry',
               'revocation',
               'refund_clawback',
               'reservation',
               'adjustment'
           )
       )
    THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::numeric, false, 'invalid_request';
        RETURN;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    -- This lock serializes both the balance snapshot and the idempotency check.
    -- Checking idempotency before this lock allows two first attempts to race
    -- into the unique index.
    SELECT balance
    INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    -- Idempotency is a property of caller input, not of the plan policy that
    -- happens to be active when a retry arrives. Check it before expiry or
    -- policy work so an exact retry has no new accounting side effects.
    v_digest := extensions.digest(
        convert_to(
            jsonb_build_object(
                'amount', bursar.digest_numeric_text(p_amount),
                'kind', p_kind::text,
                'operation', p_operation,
                'bucket', p_bucket_key,
                'catalog_revision_id', p_catalog_revision_id,
                'expires_at', p_expires_at,
                'minimum_balance',
                    bursar.digest_numeric_text(v_requested_minimum_balance),
                'request', COALESCE(p_request, '{}'::jsonb)
            )::text,
            'UTF8'
        ),
        'sha256'
    );

    SELECT *
    INTO v_old
    FROM bursar.credit_ledger_entries
    WHERE account_id = v_account
      AND idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_old.request_digest <> v_digest THEN
            RETURN QUERY
            SELECT NULL::uuid, NULL::numeric, false, 'idempotency_conflict';
        ELSE
            RETURN QUERY
            SELECT v_old.id, v_old.balance_after, true, NULL::text;
        END IF;
        RETURN;
    END IF;

    -- Expiry is an accounting event, not merely a read filter. Settle this
    -- account's due lots before using its cached balance so a credit line
    -- cannot spend against expired credits and later make expiry impossible.
    FOR v_lot IN
        SELECT id, granted - consumed AS amount
        FROM bursar.credit_lots
        WHERE account_id = v_account
          AND expires_at <= now()
          AND consumed < granted
        ORDER BY expires_at, id
        FOR UPDATE
    LOOP
        PERFORM bursar.targeted_lot_debit(
            v_lot.id,
            'expiry',
            v_lot.amount,
            'expiry:' || v_lot.id::text
        );
    END LOOP;

    SELECT balance
    INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_account;

    -- An in-flight lease is a durable admission decision. Settlement uses the
    -- minimum-balance policy captured at reservation time even if the subject
    -- changed plans while the operation was running. Only settle_lease can
    -- create the transaction-local `settling` state.
    IF p_kind = 'usage'
       AND p_request->>'lease_id'
            ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    THEN
        SELECT lease.minimum_balance
        INTO v_settling_minimum
        FROM bursar.credit_leases AS lease
        WHERE lease.id = (p_request->>'lease_id')::uuid
          AND lease.account_id = v_account
          AND lease.status = 'settling'
        FOR UPDATE;
    END IF;

    IF v_settling_minimum IS NOT NULL THEN
        v_policy_minimum := v_settling_minimum;
    ELSE
        BEGIN
            SELECT minimum_balance
            INTO v_policy_minimum
            FROM bursar.effective_subject_policy(
                p_subject_id,
                p_operation
            );

            v_policy_minimum := COALESCE(v_policy_minimum, 0);
        EXCEPTION
            WHEN undefined_function THEN
                v_policy_minimum := 0;
        END;
    END IF;

    IF p_minimum_balance IS NULL THEN
        p_minimum_balance := v_policy_minimum;
    ELSIF NOT bursar.is_finite_numeric(p_minimum_balance)
          OR (
              v_settling_minimum IS NULL
              AND p_minimum_balance < v_policy_minimum
          )
          OR (
              v_settling_minimum IS NOT NULL
              AND p_minimum_balance <> v_settling_minimum
          )
    THEN
        RETURN QUERY
        SELECT NULL::uuid, v_balance, false, 'policy_mismatch';
        RETURN;
    END IF;

    IF p_amount > 0 THEN
        v_revision := p_catalog_revision_id;

        IF v_revision IS NULL THEN
            SELECT id
            INTO v_revision
            FROM bursar.catalog_revisions
            WHERE status = 'active';
        END IF;

        v_bucket_key := p_bucket_key;

        IF v_bucket_key IS NULL
           OR (
               v_bucket_key = 'default'
               AND NOT EXISTS (
                   SELECT 1
                   FROM bursar.catalog_buckets
                   WHERE catalog_revision_id = v_revision
                     AND bucket_key = 'default'
               )
           )
        THEN
            SELECT bucket_key
            INTO v_bucket_key
            FROM bursar.catalog_buckets
            WHERE catalog_revision_id = v_revision
              AND is_default;
        END IF;

        SELECT *
        INTO v_bucket
        FROM bursar.catalog_buckets
        WHERE catalog_revision_id = v_revision
          AND bucket_key = v_bucket_key;

        IF NOT FOUND THEN
            RETURN QUERY
            SELECT NULL::uuid, NULL::numeric, false, 'missing_catalog_bucket';
            RETURN;
        END IF;

        v_effective_expiry := p_expires_at;

        IF v_effective_expiry IS NULL THEN
            v_effective_expiry := bursar.expiry_policy_at(
                p_subject_id,
                v_revision,
                v_bucket.expiry_policy,
                now(),
                NULL
            );
        ELSIF v_effective_expiry <= now() THEN
            RETURN QUERY
            SELECT NULL::uuid, NULL::numeric, false, 'invalid_expiry';
            RETURN;
        END IF;
    ELSE
        -- Administrative adjustments may intentionally target one bucket
        -- (for example replacing unused subscription-cycle credits). Usage,
        -- expiry, revocation, and clawbacks retain their own allocation rules.
        IF p_kind = 'adjustment' AND p_bucket_key IS NOT NULL THEN
            v_debit_bucket_key := p_bucket_key;
        END IF;

        SELECT COALESCE(sum(granted - consumed), 0)
        INTO v_available
        FROM bursar.credit_lots
        WHERE account_id = v_account
          AND consumed < granted
          AND (expires_at IS NULL OR expires_at > now())
          AND (
              v_debit_bucket_key IS NULL
              OR bucket_key = v_debit_bucket_key
          );

        IF p_kind IN ('usage', 'reservation') THEN
            SELECT COALESCE(
                sum(lease.reserved_amount - lease.reserved_allowance),
                0
            )
            INTO v_reserved
            FROM bursar.credit_leases AS lease
            WHERE lease.account_id = v_account
              AND lease.status = 'active'
              AND lease.expires_at > now();
        END IF;

        IF p_kind <> 'refund_clawback'
           AND (
               v_balance + p_amount - v_reserved < p_minimum_balance
               OR (
                   p_minimum_balance >= 0
                   AND v_available - v_reserved < -p_amount
               )
           )
        THEN
            RETURN QUERY
            SELECT NULL::uuid, v_balance, false, 'insufficient_credits';
            RETURN;
        END IF;

        v_revision := p_catalog_revision_id;

        IF v_revision IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
               FROM bursar.catalog_revisions
               WHERE id = v_revision
           )
        THEN
            RETURN QUERY
            SELECT NULL::uuid, v_balance, false, 'missing_catalog_revision';
            RETURN;
        END IF;

        IF v_revision IS NULL THEN
            SELECT catalog_revision_id
            INTO v_revision
            FROM bursar.account_plan_assignments
            WHERE account_id = v_account
              AND starts_at <= now()
              AND (ends_at IS NULL OR ends_at > now());
        END IF;

        IF v_revision IS NULL THEN
            SELECT id
            INTO v_revision
            FROM bursar.catalog_revisions
            WHERE status = 'active';
        END IF;
    END IF;

    PERFORM set_config('bursar.mutation_context', 'internal', true);

    INSERT INTO bursar.credit_ledger_entries(
        account_id,
        kind,
        amount,
        balance_after,
        catalog_revision_id,
        idempotency_key,
        request_digest,
        operation,
        metadata
    )
    VALUES (
        v_account,
        p_kind,
        p_amount,
        v_balance + p_amount,
        v_revision,
        p_idempotency_key,
        v_digest,
        p_operation,
        COALESCE(p_request, '{}'::jsonb)
    )
    RETURNING
        credit_ledger_entries.id,
        credit_ledger_entries.balance_after
    INTO v_entry, v_balance;

    UPDATE bursar.credit_accounts
    SET balance = v_balance,
        version = version + 1
    WHERE id = v_account;

    IF p_amount < 0 THEN
        v_remaining := -p_amount;

        FOR v_lot IN
            SELECT id, granted - consumed AS available
            FROM bursar.credit_lots
            WHERE account_id = v_account
              AND consumed < granted
              AND (expires_at IS NULL OR expires_at > now())
              AND (
                  v_debit_bucket_key IS NULL
                  OR bucket_key = v_debit_bucket_key
              )
            ORDER BY priority, expires_at NULLS LAST, created_at, id
            FOR UPDATE
        LOOP
            v_take := LEAST(v_remaining, v_lot.available);

            UPDATE bursar.credit_lots
            SET consumed = consumed + v_take
            WHERE id = v_lot.id;

            INSERT INTO bursar.credit_lot_allocations(
                debit_entry_id,
                lot_id,
                amount,
                allocation_kind
            )
            VALUES (
                v_entry,
                v_lot.id,
                v_take,
                CASE p_kind
                    WHEN 'expiry' THEN 'expiry'
                    WHEN 'revocation' THEN 'revocation'
                    WHEN 'refund_clawback' THEN 'clawback'
                    ELSE 'spend'
                END
            )
            RETURNING id INTO v_allocation_id;

            v_source_remaining := v_take;

            FOR v_source IN
                SELECT
                    source.id,
                    source.amount
                        - COALESCE(allocated.amount, 0)
                        + COALESCE(restored.amount, 0) AS available
                FROM bursar.credit_lot_sources AS source
                LEFT JOIN LATERAL (
                    SELECT sum(source_allocation.amount) AS amount
                    FROM bursar.credit_lot_source_allocations
                        AS source_allocation
                    WHERE source_allocation.lot_source_id = source.id
                ) AS allocated ON true
                LEFT JOIN LATERAL (
                    SELECT sum(source_restoration.amount) AS amount
                    FROM bursar.credit_lot_source_restorations
                        AS source_restoration
                    JOIN bursar.credit_lot_source_allocations
                        AS source_allocation
                      ON source_allocation.id =
                         source_restoration.source_allocation_id
                    WHERE source_allocation.lot_source_id = source.id
                ) AS restored ON true
                WHERE source.lot_id = v_lot.id
                  AND source.amount
                        - COALESCE(allocated.amount, 0)
                        + COALESCE(restored.amount, 0) > 0
                ORDER BY source.created_at, source.id
                FOR UPDATE OF source
            LOOP
                v_source_take := LEAST(
                    v_source_remaining,
                    v_source.available
                );

                INSERT INTO bursar.credit_lot_source_allocations(
                    lot_allocation_id,
                    lot_source_id,
                    amount
                )
                VALUES (
                    v_allocation_id,
                    v_source.id,
                    v_source_take
                );

                v_source_remaining :=
                    v_source_remaining - v_source_take;
                EXIT WHEN v_source_remaining <= 0;
            END LOOP;

            IF v_source_remaining > 0 THEN
                RAISE EXCEPTION 'credit lot source balance mismatch'
                    USING ERRCODE = '23514';
            END IF;

            v_remaining := v_remaining - v_take;
            EXIT WHEN v_remaining <= 0;
        END LOOP;

        IF v_remaining > 0 THEN
            INSERT INTO bursar.credit_unallocated_debits(
                ledger_entry_id,
                account_id,
                amount,
                reason
            )
            VALUES (
                v_entry,
                v_account,
                v_remaining,
                CASE p_kind
                    WHEN 'refund_clawback' THEN 'refund_debt'
                    ELSE 'credit_line'
                END
            );
        END IF;
    ELSE
        -- Positive credit first repays an existing negative balance. Only the
        -- portion that increases spendable balance becomes a new lot.
        v_debt_repayment := LEAST(p_amount, GREATEST(-(
            v_balance - p_amount
        ), 0));
        v_lot_amount := p_amount - v_debt_repayment;

        IF v_debt_repayment > 0 THEN
            INSERT INTO bursar.credit_debt_repayments(
                ledger_entry_id,
                account_id,
                amount
            )
            VALUES (v_entry, v_account, v_debt_repayment);
        END IF;

        IF v_lot_amount > 0 THEN
            v_source_type := CASE p_kind
                WHEN 'purchase' THEN 'topup'
                WHEN 'grant' THEN 'grant_program'
                WHEN 'refund' THEN 'refund'
                WHEN 'adjustment' THEN 'adjustment'
                ELSE 'ledger'
            END;

            INSERT INTO bursar.credit_lots(
                account_id,
                source_entry_id,
                catalog_revision_id,
                bucket_key,
                priority,
                granted,
                expires_at,
                expiry_policy_snapshot,
                source_type
            )
            VALUES (
                v_account,
                v_entry,
                v_revision,
                v_bucket_key,
                v_bucket.priority,
                v_lot_amount,
                v_effective_expiry,
                v_bucket.expiry_policy,
                v_source_type
            )
            RETURNING id INTO v_lot_id;

            INSERT INTO bursar.credit_lot_sources(
                lot_id,
                ledger_entry_id,
                amount,
                source_type
            )
            VALUES (
                v_lot_id,
                v_entry,
                v_lot_amount,
                v_source_type
            );
        END IF;
    END IF;

    RETURN QUERY
    SELECT v_entry, v_balance, false, NULL::text;
END
$$;
