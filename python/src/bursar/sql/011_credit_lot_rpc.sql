-- Targeted lot mutation, expiry, revocation, and source-preserving refunds.

CREATE FUNCTION bursar.targeted_lot_debit(
    p_lot_id uuid,
    p_kind bursar.ledger_entry_kind,
    p_amount numeric,
    p_idempotency_key text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_lot bursar.credit_lots;
    v_account_id uuid;
    v_balance numeric;
    v_entry uuid;
    v_old bursar.credit_ledger_entries;
    v_digest bytea;
    v_allocation_id uuid;
    v_source record;
    v_source_remaining numeric;
    v_source_take numeric;
BEGIN
    IF p_kind NOT IN ('expiry', 'revocation', 'refund_clawback')
       OR NOT bursar.is_finite_numeric(p_amount)
       OR p_amount <= 0
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
    THEN
        RAISE EXCEPTION 'invalid_targeted_debit'
            USING ERRCODE = '22023';
    END IF;

    SELECT account_id
    INTO v_account_id
    FROM bursar.credit_lots
    WHERE id = p_lot_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'lot_unavailable' USING ERRCODE = '22023';
    END IF;

    SELECT balance
    INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_account_id
    FOR UPDATE;

    SELECT *
    INTO v_lot
    FROM bursar.credit_lots
    WHERE id = p_lot_id
      AND account_id = v_account_id
    FOR UPDATE;

    IF NOT FOUND OR v_lot.granted - v_lot.consumed < p_amount THEN
        RAISE EXCEPTION 'lot_unavailable' USING ERRCODE = '22023';
    END IF;

    v_digest := extensions.digest(
        convert_to(
            jsonb_build_object(
                'lot_id', p_lot_id,
                'kind', p_kind::text,
                'amount', bursar.digest_numeric_text(p_amount)
            )::text,
            'UTF8'
        ),
        'sha256'
    );

    SELECT *
    INTO v_old
    FROM bursar.credit_ledger_entries
    WHERE account_id = v_lot.account_id
      AND idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_old.request_digest <> v_digest THEN
            RAISE EXCEPTION 'idempotency_conflict'
                USING ERRCODE = '23505';
        END IF;
        RETURN v_old.id;
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
        v_lot.account_id,
        p_kind,
        -p_amount,
        v_balance - p_amount,
        v_lot.catalog_revision_id,
        p_idempotency_key,
        v_digest,
        p_kind::text,
        jsonb_build_object('lot_id', p_lot_id)
    )
    RETURNING id INTO v_entry;

    UPDATE bursar.credit_accounts
    SET balance = balance - p_amount,
        version = version + 1
    WHERE id = v_lot.account_id;

    UPDATE bursar.credit_lots
    SET consumed = consumed + p_amount
    WHERE id = p_lot_id;

    INSERT INTO bursar.credit_lot_allocations(
        debit_entry_id,
        lot_id,
        amount,
        allocation_kind
    )
    VALUES (
        v_entry,
        p_lot_id,
        p_amount,
        CASE p_kind
            WHEN 'refund_clawback' THEN 'clawback'
            ELSE p_kind::text
        END
    )
    RETURNING id INTO v_allocation_id;

    v_source_remaining := p_amount;

    FOR v_source IN
        SELECT
            source.id,
            source.amount
                - COALESCE(allocated.amount, 0)
                + COALESCE(restored.amount, 0) AS available
        FROM bursar.credit_lot_sources AS source
        LEFT JOIN LATERAL (
            SELECT sum(source_allocation.amount) AS amount
            FROM bursar.credit_lot_source_allocations AS source_allocation
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
        WHERE source.lot_id = p_lot_id
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

        v_source_remaining := v_source_remaining - v_source_take;
        EXIT WHEN v_source_remaining <= 0;
    END LOOP;

    IF v_source_remaining > 0 THEN
        RAISE EXCEPTION 'credit lot source balance mismatch'
            USING ERRCODE = '23514';
    END IF;

    RETURN v_entry;
END
$$;

CREATE FUNCTION bursar.clawback_credit_source(
    p_source_entry_id uuid,
    p_amount numeric,
    p_idempotency_key text,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
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
    v_source_entry bursar.credit_ledger_entries;
    v_existing bursar.credit_ledger_entries;
    v_lot_source bursar.credit_lot_sources;
    v_balance numeric;
    v_digest bytea;
    v_entry uuid;
    v_previously_clawed_back numeric;
    v_source_consumed numeric;
    v_source_available numeric := 0;
    v_lot_available numeric := 0;
    v_take numeric := 0;
    v_remaining numeric;
    v_allocation_id uuid;
BEGIN
    IF NOT bursar.is_finite_numeric(p_amount)
       OR p_amount <= 0
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
    THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::numeric, false, 'invalid_request';
        RETURN;
    END IF;

    SELECT *
    INTO v_source_entry
    FROM bursar.credit_ledger_entries
    WHERE id = p_source_entry_id
      AND amount > 0
      AND kind IN ('grant', 'purchase', 'refund', 'adjustment');

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::numeric, false, 'missing_credit_source';
        RETURN;
    END IF;

    SELECT balance
    INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_source_entry.account_id
    FOR UPDATE;

    v_digest := extensions.digest(
        convert_to(
            jsonb_build_object(
                'source_entry_id', p_source_entry_id,
                'amount', bursar.digest_numeric_text(p_amount),
                'metadata', COALESCE(p_metadata, '{}'::jsonb)
            )::text,
            'UTF8'
        ),
        'sha256'
    );

    SELECT *
    INTO v_existing
    FROM bursar.credit_ledger_entries
    WHERE account_id = v_source_entry.account_id
      AND idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_existing.kind = 'refund_clawback'
           AND v_existing.reference_entry_id = p_source_entry_id
           AND v_existing.request_digest = v_digest
        THEN
            RETURN QUERY
            SELECT
                v_existing.id,
                v_existing.balance_after,
                true,
                NULL::text;
        ELSE
            RETURN QUERY
            SELECT NULL::uuid, NULL::numeric, false, 'idempotency_conflict';
        END IF;
        RETURN;
    END IF;

    SELECT COALESCE(sum(-entry.amount), 0)
    INTO v_previously_clawed_back
    FROM bursar.credit_ledger_entries AS entry
    WHERE entry.reference_entry_id = p_source_entry_id
      AND entry.kind = 'refund_clawback';

    IF v_previously_clawed_back + p_amount > v_source_entry.amount THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            v_balance,
            false,
            'clawback_exceeds_credit_source';
        RETURN;
    END IF;

    SELECT source.*
    INTO v_lot_source
    FROM bursar.credit_lot_sources AS source
    WHERE source.ledger_entry_id = p_source_entry_id
    FOR UPDATE;

    IF FOUND THEN
        PERFORM 1
        FROM bursar.credit_lots
        WHERE id = v_lot_source.lot_id
        FOR UPDATE;

        -- Avoid multiplying allocations when they have multiple restoration
        -- rows by calculating the two aggregates independently.
        SELECT
            COALESCE((
                SELECT sum(source_allocation.amount)
                FROM bursar.credit_lot_source_allocations
                    AS source_allocation
                WHERE source_allocation.lot_source_id = v_lot_source.id
            ), 0)
            - COALESCE((
                SELECT sum(source_restoration.amount)
                FROM bursar.credit_lot_source_restorations
                    AS source_restoration
                JOIN bursar.credit_lot_source_allocations
                    AS source_allocation
                  ON source_allocation.id =
                     source_restoration.source_allocation_id
                WHERE source_allocation.lot_source_id = v_lot_source.id
            ), 0)
        INTO v_source_consumed;

        SELECT granted - consumed
        INTO v_lot_available
        FROM bursar.credit_lots
        WHERE id = v_lot_source.lot_id;

        v_source_available := GREATEST(
            v_lot_source.amount - v_source_consumed,
            0
        );
        v_take := LEAST(
            p_amount,
            v_source_available,
            v_lot_available
        );
    END IF;

    PERFORM set_config('bursar.mutation_context', 'internal', true);

    INSERT INTO bursar.credit_ledger_entries(
        account_id,
        kind,
        amount,
        balance_after,
        reference_entry_id,
        catalog_revision_id,
        idempotency_key,
        request_digest,
        operation,
        metadata
    )
    VALUES (
        v_source_entry.account_id,
        'refund_clawback',
        -p_amount,
        v_balance - p_amount,
        p_source_entry_id,
        v_source_entry.catalog_revision_id,
        p_idempotency_key,
        v_digest,
        'billing_refund_clawback',
        COALESCE(p_metadata, '{}'::jsonb)
    )
    RETURNING id, credit_ledger_entries.balance_after
    INTO v_entry, v_balance;

    UPDATE bursar.credit_accounts
    SET balance = v_balance,
        version = version + 1
    WHERE id = v_source_entry.account_id;

    IF v_take > 0 THEN
        UPDATE bursar.credit_lots
        SET consumed = consumed + v_take
        WHERE id = v_lot_source.lot_id;

        INSERT INTO bursar.credit_lot_allocations(
            debit_entry_id,
            lot_id,
            amount,
            allocation_kind
        )
        VALUES (
            v_entry,
            v_lot_source.lot_id,
            v_take,
            'clawback'
        )
        RETURNING id INTO v_allocation_id;

        INSERT INTO bursar.credit_lot_source_allocations(
            lot_allocation_id,
            lot_source_id,
            amount
        )
        VALUES (
            v_allocation_id,
            v_lot_source.id,
            v_take
        );
    END IF;

    v_remaining := p_amount - v_take;

    IF v_remaining > 0 THEN
        INSERT INTO bursar.credit_unallocated_debits(
            ledger_entry_id,
            account_id,
            amount,
            reason
        )
        VALUES (
            v_entry,
            v_source_entry.account_id,
            v_remaining,
            'refund_debt'
        );
    END IF;

    RETURN QUERY
    SELECT v_entry, v_balance, false, NULL::text;
END
$$;

CREATE FUNCTION bursar.sweep_expired_lots(
    p_limit integer DEFAULT 100,
    p_subject_id uuid DEFAULT NULL,
    p_dry_run boolean DEFAULT false
)
RETURNS TABLE(
    expired_count integer,
    expired_amount numeric,
    expired_by_bucket jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_count integer := 0;
    v_amount numeric := 0;
    v_by_bucket jsonb := '{}'::jsonb;
    lot_row record;
    v_expiry_amount numeric;
BEGIN
    IF p_limit IS NULL
       OR p_limit < 1
       OR p_limit > 1000
       OR p_dry_run IS NULL
    THEN
        RAISE EXCEPTION 'invalid_sweep_request'
            USING ERRCODE = '22023';
    END IF;

    IF p_dry_run THEN
        WITH candidates AS (
            SELECT
                lot.bucket_key,
                lot.granted - lot.consumed AS amount
            FROM bursar.credit_lots AS lot
            JOIN bursar.credit_accounts AS account
              ON account.id = lot.account_id
            WHERE lot.expires_at <= now()
              AND lot.consumed < lot.granted
              AND (
                  p_subject_id IS NULL
                  OR (
                      account.subject_id = p_subject_id
                      AND account.account_kind = 'personal'
                  )
              )
            ORDER BY lot.expires_at, lot.id
            LIMIT p_limit
        ),
        bucket_totals AS (
            SELECT bucket_key, sum(amount) AS amount
            FROM candidates
            GROUP BY bucket_key
        )
        SELECT
            (SELECT count(*)::integer FROM candidates),
            COALESCE((SELECT sum(amount) FROM candidates), 0),
            COALESCE(
                (
                    SELECT jsonb_object_agg(
                        bucket_key,
                        amount
                        ORDER BY bucket_key
                    )
                    FROM bucket_totals
                ),
                '{}'::jsonb
            )
        INTO v_count, v_amount, v_by_bucket;

        RETURN QUERY SELECT v_count, v_amount, v_by_bucket;
        RETURN;
    END IF;

    FOR lot_row IN
        SELECT
            lot.id,
            lot.account_id,
            lot.bucket_key
        FROM bursar.credit_lots AS lot
        JOIN bursar.credit_accounts AS account
          ON account.id = lot.account_id
        WHERE lot.expires_at <= now()
          AND lot.consumed < lot.granted
          AND (
              p_subject_id IS NULL
              OR (
                  account.subject_id = p_subject_id
                  AND account.account_kind = 'personal'
              )
          )
        ORDER BY lot.expires_at, lot.id
        LIMIT p_limit
    LOOP
        PERFORM 1
        FROM bursar.credit_accounts
        WHERE id = lot_row.account_id
        FOR UPDATE SKIP LOCKED;

        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        SELECT granted - consumed
        INTO v_expiry_amount
        FROM bursar.credit_lots
        WHERE id = lot_row.id
          AND expires_at <= now()
          AND consumed < granted;

        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        PERFORM bursar.targeted_lot_debit(
            lot_row.id,
            'expiry',
            v_expiry_amount,
            'expiry:' || lot_row.id::text
        );

        v_count := v_count + 1;
        v_amount := v_amount + v_expiry_amount;
        v_by_bucket := jsonb_set(
            v_by_bucket,
            ARRAY[lot_row.bucket_key],
            to_jsonb(
                COALESCE(
                    (v_by_bucket->>lot_row.bucket_key)::numeric,
                    0
                ) + v_expiry_amount
            ),
            true
        );
    END LOOP;

    RETURN QUERY SELECT v_count, v_amount, v_by_bucket;
END
$$;

CREATE FUNCTION bursar.revoke_lot(
    p_lot_id uuid,
    p_amount numeric,
    p_idempotency_key text
)
RETURNS TABLE(
    entry_id uuid,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        bursar.targeted_lot_debit(
            p_lot_id,
            'revocation',
            p_amount,
            p_idempotency_key
        ),
        NULL::text;
EXCEPTION
    WHEN unique_violation THEN
        RETURN QUERY SELECT NULL::uuid, 'idempotency_conflict';
    WHEN invalid_parameter_value THEN
        RETURN QUERY SELECT NULL::uuid, 'lot_unavailable';
    WHEN check_violation THEN
        RETURN QUERY SELECT NULL::uuid, 'balance_lot_mismatch';
END
$$;

CREATE FUNCTION bursar.refund_credit(
    p_subject_id uuid,
    p_amount numeric,
    p_idempotency_key text,
    p_original_entry_id uuid
)
RETURNS TABLE(
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
    v_original bursar.credit_ledger_entries;
    v_existing bursar.credit_ledger_entries;
    v_digest bytea;
    v_balance numeric;
    v_entry uuid;
    v_refunded numeric;
    v_remaining numeric;
    v_take numeric;
    v_restored numeric;
    allocation_row record;
    expired_row record;
    v_revision uuid;
    v_bucket bursar.catalog_buckets;
    v_expiry timestamptz;
    v_debt_repayment numeric;
    v_new_lot uuid;
    v_lot_restoration_id uuid;
    source_allocation_row record;
    v_source_restored numeric;
    v_source_restore numeric;
    v_restore_remaining numeric;
BEGIN
    IF NOT bursar.is_finite_numeric(p_amount)
       OR p_amount <= 0
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
    THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::numeric, false, 'invalid_request';
        RETURN;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    SELECT balance
    INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    SELECT *
    INTO v_original
    FROM bursar.credit_ledger_entries
    WHERE id = p_original_entry_id
      AND account_id = v_account
      AND amount < 0
      AND kind IN ('usage', 'reservation', 'adjustment')
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::numeric, false, 'missing_original';
        RETURN;
    END IF;

    v_digest := extensions.digest(
        convert_to(
            jsonb_build_object(
                'amount', bursar.digest_numeric_text(p_amount),
                'original_entry_id', p_original_entry_id
            )::text,
            'UTF8'
        ),
        'sha256'
    );

    SELECT *
    INTO v_existing
    FROM bursar.credit_ledger_entries
    WHERE account_id = v_account
      AND idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_existing.kind = 'refund'
           AND v_existing.request_digest = v_digest
           AND v_existing.reference_entry_id = p_original_entry_id
        THEN
            RETURN QUERY
            SELECT
                v_existing.id,
                v_existing.balance_after,
                true,
                NULL::text;
        ELSE
            RETURN QUERY
            SELECT NULL::uuid, NULL::numeric, false, 'idempotency_conflict';
        END IF;
        RETURN;
    END IF;

    SELECT COALESCE(sum(amount), 0)
    INTO v_refunded
    FROM bursar.credit_ledger_entries
    WHERE reference_entry_id = p_original_entry_id
      AND kind = 'refund';

    IF v_refunded + p_amount > -v_original.amount THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::numeric, false, 'refund_exceeds_original';
        RETURN;
    END IF;

    SELECT bucket.*
    INTO v_bucket
    FROM bursar.catalog_buckets AS bucket
    JOIN bursar.catalog_revisions AS revision
      ON revision.id = bucket.catalog_revision_id
     AND revision.status = 'active'
    WHERE bucket.is_default;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::numeric, false, 'missing_catalog_bucket';
        RETURN;
    END IF;

    v_revision := v_bucket.catalog_revision_id;

    PERFORM set_config('bursar.mutation_context', 'internal', true);

    INSERT INTO bursar.credit_ledger_entries(
        account_id,
        kind,
        amount,
        balance_after,
        reference_entry_id,
        catalog_revision_id,
        idempotency_key,
        request_digest,
        operation,
        metadata
    )
    VALUES (
        v_account,
        'refund',
        p_amount,
        v_balance + p_amount,
        p_original_entry_id,
        v_original.catalog_revision_id,
        p_idempotency_key,
        v_digest,
        'refund',
        jsonb_build_object('original_entry_id', p_original_entry_id)
    )
    RETURNING id, credit_ledger_entries.balance_after
    INTO v_entry, v_balance;

    UPDATE bursar.credit_accounts
    SET balance = v_balance,
        version = version + 1
    WHERE id = v_account;

    v_remaining := p_amount;

    FOR allocation_row IN
        SELECT
            allocation.id,
            allocation.lot_id,
            allocation.amount,
            lot.expires_at
        FROM bursar.credit_lot_allocations AS allocation
        JOIN bursar.credit_lots AS lot
          ON lot.id = allocation.lot_id
        WHERE allocation.debit_entry_id = p_original_entry_id
        ORDER BY allocation.created_at DESC, allocation.id DESC
        FOR UPDATE OF lot
    LOOP
        SELECT COALESCE(sum(amount), 0)
        INTO v_restored
        FROM bursar.credit_lot_restorations
        WHERE original_allocation_id = allocation_row.id;

        v_take := LEAST(
            v_remaining,
            allocation_row.amount - v_restored
        );

        IF v_take <= 0 THEN
            CONTINUE;
        END IF;

        UPDATE bursar.credit_lots
        SET consumed = consumed - v_take
        WHERE id = allocation_row.lot_id
          AND consumed >= v_take;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'lot restoration state mismatch'
                USING ERRCODE = '23514';
        END IF;

        INSERT INTO bursar.credit_lot_restorations(
            refund_entry_id,
            original_allocation_id,
            lot_id,
            amount
        )
        VALUES (
            v_entry,
            allocation_row.id,
            allocation_row.lot_id,
            v_take
        )
        RETURNING id INTO v_lot_restoration_id;

        v_restore_remaining := v_take;

        FOR source_allocation_row IN
            SELECT
                source_allocation.id,
                source_allocation.amount
            FROM bursar.credit_lot_source_allocations
                AS source_allocation
            WHERE source_allocation.lot_allocation_id = allocation_row.id
            ORDER BY source_allocation.created_at DESC,
                     source_allocation.id DESC
            FOR UPDATE
        LOOP
            SELECT COALESCE(sum(amount), 0)
            INTO v_source_restored
            FROM bursar.credit_lot_source_restorations
            WHERE source_allocation_id = source_allocation_row.id;

            v_source_restore := LEAST(
                v_restore_remaining,
                source_allocation_row.amount - v_source_restored
            );

            IF v_source_restore <= 0 THEN
                CONTINUE;
            END IF;

            INSERT INTO bursar.credit_lot_source_restorations(
                lot_restoration_id,
                source_allocation_id,
                amount
            )
            VALUES (
                v_lot_restoration_id,
                source_allocation_row.id,
                v_source_restore
            );

            v_restore_remaining :=
                v_restore_remaining - v_source_restore;
            EXIT WHEN v_restore_remaining <= 0;
        END LOOP;

        IF v_restore_remaining > 0 THEN
            RAISE EXCEPTION 'lot source restoration state mismatch'
                USING ERRCODE = '23514';
        END IF;

        v_remaining := v_remaining - v_take;
        EXIT WHEN v_remaining <= 0;
    END LOOP;

    -- The unallocated part of the original debit was credit-line debt. A
    -- refund first repays any current debt; if that debt has since been paid,
    -- the excess becomes a normal default-bucket lot.
    IF v_remaining > 0 THEN
        v_debt_repayment := LEAST(
            v_remaining,
            GREATEST(-(v_balance - p_amount), 0)
        );

        IF v_debt_repayment > 0 THEN
            INSERT INTO bursar.credit_debt_repayments(
                ledger_entry_id,
                account_id,
                amount
            )
            VALUES (v_entry, v_account, v_debt_repayment);

            v_remaining := v_remaining - v_debt_repayment;
        END IF;
    END IF;

    IF v_remaining > 0 THEN
        v_expiry := bursar.expiry_policy_at(
            p_subject_id,
            v_revision,
            v_bucket.expiry_policy,
            now(),
            NULL
        );

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
            v_bucket.bucket_key,
            v_bucket.priority,
            v_remaining,
            v_expiry,
            v_bucket.expiry_policy,
            'refund'
        )
        RETURNING id INTO v_new_lot;

        INSERT INTO bursar.credit_lot_sources(
            lot_id,ledger_entry_id,amount,source_type
        )
        VALUES(v_new_lot,v_entry,v_remaining,'refund');
    END IF;

    -- Restored credits retain the original lot expiry. If that expiry has
    -- already passed, expire the restored portion in this transaction.
    FOR expired_row IN
        SELECT
            restoration.lot_id,
            sum(restoration.amount) AS amount
        FROM bursar.credit_lot_restorations AS restoration
        JOIN bursar.credit_lots AS lot ON lot.id = restoration.lot_id
        WHERE restoration.refund_entry_id = v_entry
          AND lot.expires_at <= now()
        GROUP BY restoration.lot_id
    LOOP
        PERFORM bursar.targeted_lot_debit(
            expired_row.lot_id,
            'expiry',
            expired_row.amount,
            concat_ws(
                ':',
                'refund-expiry',
                v_entry,
                expired_row.lot_id
            )
        );
    END LOOP;

    SELECT balance
    INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_account;

    RETURN QUERY
    SELECT v_entry, v_balance, false, NULL::text;
END
$$;

CREATE FUNCTION bursar.refund_credit_by_entry(
    p_original_entry_id uuid,
    p_amount numeric,
    p_idempotency_key text,
    p_reason text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
    entry_id uuid,
    subject_id uuid,
    amount numeric,
    balance_after numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_original bursar.credit_ledger_entries;
    v_subject_id uuid;
    v_refund_amount numeric;
    v_refunded numeric;
    v_existing bursar.credit_ledger_entries;
    v_result record;
    v_quota_event record;
    v_correction_event_id uuid;
BEGIN
    IF p_original_entry_id IS NULL
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR (p_reason IS NOT NULL AND NOT bursar.is_nonempty_text(p_reason))
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
       OR (p_amount IS NOT NULL AND (
           NOT bursar.is_finite_numeric(p_amount) OR p_amount <= 0
       ))
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            NULL::numeric,
            false,
            'invalid_request';
        RETURN;
    END IF;

    SELECT entry.*
    INTO v_original
    FROM bursar.credit_ledger_entries AS entry
    JOIN bursar.credit_accounts AS account ON account.id = entry.account_id
    WHERE entry.id = p_original_entry_id
      AND account.account_kind = 'personal'
      AND entry.amount < 0
      AND entry.kind IN ('usage', 'reservation', 'adjustment');

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            NULL::numeric,
            NULL::numeric,
            false,
            'missing_original';
        RETURN;
    END IF;

    SELECT account.subject_id
    INTO v_subject_id
    FROM bursar.credit_accounts AS account
    WHERE account.id = v_original.account_id;

    SELECT *
    INTO v_existing
    FROM bursar.credit_ledger_entries
    WHERE account_id = v_original.account_id
      AND idempotency_key = p_idempotency_key;

    -- An omitted amount means "refund the currently remaining amount". Check
    -- replay before recalculating it so a retried full refund is idempotent
    -- instead of becoming an invalid zero-amount request.
    IF p_amount IS NULL
       AND FOUND
       AND v_existing.kind = 'refund'
       AND v_existing.reference_entry_id = p_original_entry_id
    THEN
        RETURN QUERY
        SELECT
            v_existing.id,
            v_subject_id,
            v_existing.amount,
            v_existing.balance_after,
            true,
            NULL::text;
        RETURN;
    END IF;

    SELECT COALESCE(sum(refund.amount), 0)
    INTO v_refunded
    FROM bursar.credit_ledger_entries AS refund
    WHERE refund.reference_entry_id = p_original_entry_id
      AND refund.kind = 'refund';

    v_refund_amount := COALESCE(p_amount, -v_original.amount - v_refunded);

    IF v_refund_amount <= 0 THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            v_subject_id,
            0::numeric,
            (
                SELECT account.balance
                FROM bursar.credit_accounts AS account
                WHERE account.id = v_original.account_id
            ),
            false,
            'nothing_to_refund';
        RETURN;
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.refund_credit(
        v_subject_id,
        v_refund_amount,
        p_idempotency_key,
        p_original_entry_id
    );

    IF v_result.error_code IS NULL AND NOT v_result.replayed THEN
        PERFORM set_config('bursar.mutation_context', 'internal', true);

        UPDATE bursar.credit_ledger_entries
        SET metadata = jsonb_strip_nulls(
            COALESCE(p_metadata, '{}'::jsonb)
            || jsonb_build_object(
                'original_entry_id', p_original_entry_id,
                'reason', p_reason
            )
        )
        WHERE id = v_result.entry_id;

        -- Quota usage measures the work represented by a usage charge, not
        -- its monetary value. Reverse that usage only once the charge has
        -- been refunded in full; a partial credit refund must not erase an
        -- entire invocation from quota accounting.
        IF v_refunded + v_refund_amount = -v_original.amount THEN
            FOR v_quota_event IN
                SELECT
                    event.*,
                    event.amount + COALESCE(sum(correction.amount), 0) AS remaining_amount
                FROM bursar.quota_usage_events AS event
                JOIN bursar.credit_usage_charges AS charge
                  ON charge.id = event.usage_charge_id
                LEFT JOIN bursar.quota_usage_events AS correction
                  ON correction.correction_of_event_id = event.id
                WHERE charge.ledger_entry_id = p_original_entry_id
                  AND event.correction_of_event_id IS NULL
                GROUP BY event.id
                HAVING event.amount + COALESCE(sum(correction.amount), 0) > 0
            LOOP
                v_correction_event_id := NULL;

                INSERT INTO bursar.quota_usage_events(
                    account_id,
                    plan_id,
                    catalog_revision_id,
                    catalog_quota_id,
                    quota_key,
                    operation_key,
                    measure_key,
                    amount,
                    event_at,
                    usage_charge_id,
                    correction_of_event_id,
                    idempotency_key,
                    request_digest,
                    metadata
                )
                VALUES(
                    v_quota_event.account_id,
                    v_quota_event.plan_id,
                    v_quota_event.catalog_revision_id,
                    v_quota_event.catalog_quota_id,
                    v_quota_event.quota_key,
                    v_quota_event.operation_key,
                    v_quota_event.measure_key,
                    -v_quota_event.remaining_amount,
                    now(),
                    v_quota_event.usage_charge_id,
                    v_quota_event.id,
                    concat_ws(
                        ':',
                        'refund-quota',
                        v_result.entry_id,
                        v_quota_event.id
                    ),
                    extensions.digest(
                        convert_to(
                            jsonb_build_object(
                                'refund_entry_id', v_result.entry_id,
                                'quota_event_id', v_quota_event.id,
                                'amount', bursar.digest_numeric_text(
                                    -v_quota_event.remaining_amount
                                )
                            )::text,
                            'UTF8'
                        ),
                        'sha256'
                    ),
                    jsonb_strip_nulls(
                        COALESCE(p_metadata, '{}'::jsonb)
                        || jsonb_build_object(
                            'original_entry_id', p_original_entry_id,
                            'refund_entry_id', v_result.entry_id,
                            'reason', p_reason
                        )
                    )
                )
                ON CONFLICT (
                    account_id,
                    catalog_quota_id,
                    idempotency_key
                ) DO NOTHING
                RETURNING id INTO v_correction_event_id;

                -- Calendar quotas use quota_windows as their admission cache.
                -- Rolling quotas read the immutable event stream directly.
                IF v_correction_event_id IS NOT NULL THEN
                    UPDATE bursar.quota_windows
                    SET consumed = greatest(
                        0,
                        consumed - v_quota_event.remaining_amount
                    )
                    WHERE account_id = v_quota_event.account_id
                      AND plan_id = v_quota_event.plan_id
                      AND catalog_revision_id = v_quota_event.catalog_revision_id
                      AND quota_key = v_quota_event.quota_key
                      AND v_quota_event.event_at >= window_start
                      AND v_quota_event.event_at < window_end;
                END IF;
            END LOOP;
        END IF;
    END IF;

    RETURN QUERY
    SELECT
        v_result.entry_id,
        v_subject_id,
        v_refund_amount,
        v_result.balance_after,
        v_result.replayed,
        v_result.error_code;
END
$$;

CREATE FUNCTION bursar.revoke_subject_credits_by_operation(
    p_subject_id uuid,
    p_operation text
)
RETURNS TABLE(
    revoked numeric,
    balance_after numeric,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account uuid;
    v_revoked numeric := 0;
    v_result record;
    lot_row record;
BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_nonempty_text(p_operation)
    THEN
        RETURN QUERY
        SELECT 0::numeric, NULL::numeric, 'invalid_request';
        RETURN;
    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    PERFORM 1
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    FOR lot_row IN
        SELECT
            lot.id,
            lot.granted - lot.consumed AS amount
        FROM bursar.credit_lots AS lot
        JOIN bursar.credit_ledger_entries AS source_entry
          ON source_entry.id = lot.source_entry_id
        WHERE lot.account_id = v_account
          AND lot.consumed < lot.granted
          AND source_entry.operation = p_operation
        ORDER BY
            lot.priority,
            lot.expires_at NULLS LAST,
            lot.created_at,
            lot.id
        FOR UPDATE OF lot
    LOOP
        SELECT *
        INTO v_result
        FROM bursar.revoke_lot(
            lot_row.id,
            lot_row.amount,
            'operation-revocation:'
                || p_operation
                || ':'
                || lot_row.id::text
        );

        IF v_result.error_code IS NOT NULL THEN
            RETURN QUERY
            SELECT
                v_revoked,
                (
                    SELECT account.balance
                    FROM bursar.credit_accounts AS account
                    WHERE account.id = v_account
                ),
                v_result.error_code;
            RETURN;
        END IF;

        v_revoked := v_revoked + lot_row.amount;
    END LOOP;

    RETURN QUERY
    SELECT
        v_revoked,
        account.balance,
        NULL::text
    FROM bursar.credit_accounts AS account
    WHERE account.id = v_account;
END
$$;
