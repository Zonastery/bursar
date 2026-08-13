-- Migration: 012_team_credit_rpc.sql
-- Purpose: Post team and trusted account credit mutations.
-- Depends on: Team/accounting schema through 007_indexes.sql,
--   008_catalog_rpc.sql, and 009_credit_account_rpc.sql.
-- Security: SECURITY DEFINER RPCs remain tenant-scoped; team debits lock the team
--   before membership and account state, and direct account posting is trusted-only.

-- Post team and trusted account credit mutations.

-- Lock the tenant team before membership and its team account, enforce the
-- member spend cap, and hash the exact numeric request for stable replay. The
-- resulting debit and attribution row commit together; divergent retries return
-- idempotency_conflict without consuming team credit.

CREATE FUNCTION bursar.deduct_team(
    p_team_id uuid,
    p_subject_id uuid,
    p_amount numeric,
    p_idempotency_key text,
    p_operation text DEFAULT 'team_usage',
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    entry_id uuid,
    team_id uuid,
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
    v_member bursar.credit_team_members;
    v_existing bursar.credit_team_usage_charges;
    v_result record;
    v_spent numeric;
    v_digest bytea;
BEGIN
    p_amount := round(p_amount, 6);

    IF p_team_id IS NULL
       OR p_subject_id IS NULL
       OR p_amount IS NULL
       OR NOT bursar.is_credit_numeric(p_amount)
       OR p_amount <= 0
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR NOT bursar.is_bounded_text(p_idempotency_key, 255)
       OR NOT bursar.is_nonempty_text(p_operation)
       OR NOT bursar.is_bounded_text(p_operation, 255)
       OR NOT bursar.is_bounded_json_object(
           COALESCE(p_metadata, '{}'::jsonb),
           16384
       )
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            p_team_id,
            p_subject_id,
            0::numeric,
            NULL::numeric,
            false,
            'invalid_request';
        RETURN;
    END IF;

    v_digest := extensions.digest(
        convert_to(
            jsonb_build_object(
                'subject_id', p_subject_id,
                'amount', bursar.digest_numeric_text(p_amount),
                'operation', p_operation,
                'metadata', COALESCE(p_metadata, '{}'::jsonb)
            )::text,
            'UTF8'
        ),
        'sha256'
    );

    -- Team membership mutations take this lock before touching a membership.
    -- Match that order so removal/reactivation and a new charge have one clear
    -- serialization point. Replays are resolved while holding the same lock.
    PERFORM 1
    FROM bursar.credit_teams AS team
    WHERE team.id = p_team_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            p_team_id,
            p_subject_id,
            0::numeric,
            NULL::numeric,
            false,
            'not_team_member';
        RETURN;
    END IF;

    SELECT *
    INTO v_existing
    FROM bursar.credit_team_usage_charges AS existing_usage
    WHERE existing_usage.team_id = p_team_id
      AND existing_usage.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_existing.request_digest <> v_digest THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                p_team_id,
                p_subject_id,
                0::numeric,
                NULL::numeric,
                false,
                'idempotency_conflict';
        ELSE
            RETURN QUERY
            SELECT
                v_existing.ledger_entry_id,
                p_team_id,
                v_existing.subject_id,
                v_existing.amount::numeric,
                ledger.balance_after::numeric,
                true,
                NULL::text
            FROM bursar.credit_ledger_entries AS ledger
            WHERE ledger.id = v_existing.ledger_entry_id;
        END IF;
        RETURN;
    END IF;

    SELECT *
    INTO v_member
    FROM bursar.credit_team_members AS member
    WHERE member.team_id = p_team_id
      AND member.subject_id = p_subject_id
      AND member.left_at IS NULL
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            p_team_id,
            p_subject_id,
            0::numeric,
            NULL::numeric,
            false,
            'not_team_member';
        RETURN;
    END IF;

    IF v_member.spend_cap IS NOT NULL THEN
        SELECT COALESCE(sum(team_usage.amount), 0)
        INTO v_spent
        FROM bursar.credit_team_usage_charges AS team_usage
        WHERE team_usage.team_id = p_team_id
          AND team_usage.subject_id = p_subject_id;

        IF v_spent + p_amount > v_member.spend_cap THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                p_team_id,
                p_subject_id,
                0::numeric,
                NULL::numeric,
                false,
                'member_spend_cap_exceeded';
            RETURN;
        END IF;
    END IF;

    SELECT *
    INTO v_result
    FROM bursar.post_credit_account(
        (
            SELECT account.id
            FROM bursar.credit_teams AS team
            JOIN bursar.credit_accounts AS account
              ON account.subject_id = team.subject_id
             AND account.account_kind = 'team'
            WHERE team.id = p_team_id
        ),
        'usage',
        -p_amount,
        p_operation,
        p_idempotency_key,
        COALESCE(p_metadata, '{}'::jsonb)
            || jsonb_build_object('actor_subject_id', p_subject_id)
    );

    IF v_result.error_code IS NOT NULL THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            p_team_id,
            p_subject_id,
            0::numeric,
            v_result.balance_after,
            false,
            v_result.error_code;
        RETURN;
    END IF;

    INSERT INTO bursar.credit_team_usage_charges(
        team_id,
        subject_id,
        ledger_entry_id,
        operation,
        amount,
        metadata,
        idempotency_key,
        request_digest
    )
    VALUES(
        p_team_id,
        p_subject_id,
        v_result.entry_id,
        p_operation,
        p_amount,
        COALESCE(p_metadata, '{}'::jsonb),
        p_idempotency_key,
        v_digest
    );

    RETURN QUERY
    SELECT
        v_result.entry_id,
        p_team_id,
        p_subject_id,
        p_amount,
        v_result.balance_after,
        v_result.replayed,
        NULL::text;
END
$$;

-- 2. Trusted account posting

-- Post exact credit directly to a tenant-visible account under its row lock.
-- This SECURITY DEFINER helper accepts an account identifier and is therefore a
-- trusted internal boundary: it hashes caller input, opens mutation context only
-- transaction-locally, and atomically updates ledger, lots, and source allocations.
CREATE FUNCTION bursar.post_credit_account(
    p_account_id uuid,
    p_kind bursar.ledger_entry_kind,
    p_amount numeric,
    p_operation text,
    p_idempotency_key text,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    entry_id uuid,
    balance_after numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_old bursar.credit_ledger_entries;
 v_balance numeric;
 v_entry uuid;
 v_digest bytea;
 v_available numeric;
 r record;
 v_remaining numeric;
 v_take numeric;
 v_subject uuid;
 v_revision uuid;
 v_bucket text;
 v_expiry timestamptz;
 v_priority integer;
 v_expiry_policy jsonb;
 v_lot_id uuid;
 v_allocation_id uuid;
 v_source record;
 v_source_remaining numeric;
 v_source_take numeric;

BEGIN
 p_amount := round(p_amount, 6);

 IF p_account_id IS NULL
    OR p_kind IS NULL
    OR p_amount IS NULL
    OR NOT bursar.is_credit_numeric(p_amount)
    OR p_amount=0
    OR NOT bursar.is_nonempty_text(p_idempotency_key)
    OR NOT bursar.is_bounded_text(p_idempotency_key, 255)
    OR NOT bursar.is_nonempty_text(p_operation)
    OR NOT bursar.is_bounded_text(p_operation, 255)
    OR NOT bursar.is_bounded_json_object(
        COALESCE(p_metadata, '{}'::jsonb),
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
 THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_request';
 RETURN;
 END IF;

 SELECT balance,subject_id
 INTO v_balance,v_subject
 FROM bursar.credit_accounts
 WHERE id=p_account_id
 FOR UPDATE;

 IF NOT FOUND THEN
     RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_account';
     RETURN;
 END IF;

    v_digest:=extensions.digest(
        convert_to(
            jsonb_build_object(
                'amount', bursar.digest_numeric_text(p_amount),
                'kind', p_kind::text,
                'operation', p_operation,
                'metadata', COALESCE(p_metadata, '{}'::jsonb)
            )::text,
            'UTF8'
        ),
        'sha256'
    );

 SELECT * INTO v_old FROM bursar.credit_ledger_entries WHERE account_id=p_account_id AND idempotency_key=p_idempotency_key;

 IF FOUND THEN IF v_old.request_digest<>v_digest THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'idempotency_conflict';
 ELSE RETURN QUERY SELECT v_old.id,v_old.balance_after::numeric,true,NULL::text;
 END IF;
 RETURN;
 END IF;

 IF p_amount<0 THEN SELECT COALESCE(SUM(granted-consumed),0) INTO v_available FROM bursar.credit_lots WHERE account_id=p_account_id AND consumed<granted AND (expires_at IS NULL OR expires_at>now());
 IF v_available < -p_amount OR v_balance+p_amount<0 THEN RETURN QUERY SELECT NULL::uuid,v_balance,false,'insufficient_credits';
 RETURN;
 END IF;
 END IF;

 IF NOT bursar.is_credit_numeric(v_balance + p_amount) THEN
     RETURN QUERY
     SELECT NULL::uuid, v_balance, false, 'balance_out_of_range';
     RETURN;
 END IF;

 PERFORM set_config('bursar.mutation_context','internal',true);

 INSERT INTO bursar.credit_ledger_entries AS e(
     account_id,
     kind,
     amount,
     balance_after,
     idempotency_key,
     request_digest,
     operation,
     metadata
 )
 VALUES(
     p_account_id,
     p_kind,
     p_amount,
     v_balance+p_amount,
     p_idempotency_key,
     v_digest,
     p_operation,
     COALESCE(p_metadata, '{}'::jsonb)
 )
 RETURNING e.id,e.balance_after INTO v_entry,v_balance;

 UPDATE bursar.credit_accounts
 SET balance=v_balance,version=version+1
 WHERE id=p_account_id;

    IF p_amount<0 THEN
        v_remaining:=-p_amount;

        FOR r IN
            SELECT id,granted-consumed AS available
            FROM bursar.credit_lots
            WHERE account_id=p_account_id AND consumed<granted
              AND (expires_at IS NULL OR expires_at>now())
            ORDER BY priority,expires_at NULLS LAST,created_at,id
            FOR UPDATE
        LOOP
            v_take:=LEAST(v_remaining,r.available);

            UPDATE bursar.credit_lots SET consumed=consumed+v_take WHERE id=r.id;

            INSERT INTO bursar.credit_lot_allocations(
                debit_entry_id,
                lot_id,
                amount,
                allocation_kind
            )
            VALUES(v_entry,r.id,v_take,'spend')
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
                WHERE source.lot_id = r.id
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

            v_remaining:=v_remaining-v_take;

            EXIT WHEN v_remaining<=0;

        END LOOP;

    ELSE
        SELECT b.catalog_revision_id,b.bucket_key,b.priority,b.expiry_policy
        INTO v_revision,v_bucket,v_priority,v_expiry_policy
        FROM bursar.catalog_buckets b
        JOIN bursar.catalog_revisions cr ON cr.id=b.catalog_revision_id
        WHERE cr.status='active' AND b.is_default;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'active default bucket missing' USING ERRCODE='23503';

        END IF;

        v_expiry:=bursar.bucket_expiry_at(v_subject,v_revision,v_bucket);

        INSERT INTO bursar.credit_lots(
            account_id,source_entry_id,catalog_revision_id,bucket_key,
            priority,granted,expires_at,expiry_policy_snapshot,source_type
        )
        VALUES (
            p_account_id,v_entry,v_revision,v_bucket,v_priority,p_amount,
            v_expiry,v_expiry_policy,'ledger'
        )
        RETURNING id INTO v_lot_id;

        INSERT INTO bursar.credit_lot_sources(
            lot_id,ledger_entry_id,amount,source_type
        )
        VALUES(v_lot_id,v_entry,p_amount,'ledger');

    END IF;

 RETURN QUERY SELECT v_entry,v_balance,false,NULL::text;

END $$;
