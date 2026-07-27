-- Team-account posting and allowance RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.deduct_team(
    p_team_id uuid,
    p_amount numeric,
    p_idempotency_key text,
    p_operation text DEFAULT 'team_usage'
)
RETURNS TABLE(
    entry_id uuid,
    balance_after numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_result record;

BEGIN
    SELECT * INTO v_result
    FROM bursar.post_credit_account(
        (
            SELECT a.id
            FROM bursar.credit_teams t
            JOIN bursar.credit_accounts a
              ON a.subject_id=t.subject_id AND a.account_kind='team'
            WHERE t.id=p_team_id
        ),
        'usage',-p_amount,p_operation,p_idempotency_key
    );

  RETURN QUERY SELECT v_result.entry_id,v_result.balance_after,v_result.replayed,v_result.error_code;

END $$;

CREATE FUNCTION bursar.post_credit_account(
    p_account_id uuid,
    p_kind bursar.ledger_entry_kind,
    p_amount numeric,
    p_operation text,
    p_idempotency_key text
)
RETURNS TABLE(
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

BEGIN
 IF p_account_id IS NULL OR p_amount=0 OR p_idempotency_key IS NULL THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_request';
 RETURN;
 END IF;

    v_digest:=extensions.digest(convert_to(jsonb_build_object('amount',p_amount,'kind',p_kind::text,'operation',p_operation)::text,'UTF8'),'sha256');

 SELECT * INTO v_old FROM bursar.credit_ledger_entries WHERE account_id=p_account_id AND idempotency_key=p_idempotency_key FOR UPDATE;

 IF FOUND THEN IF v_old.request_digest<>v_digest THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'idempotency_conflict';
 ELSE RETURN QUERY SELECT v_old.id,v_old.balance_after,true,NULL::text;
 END IF;
 RETURN;
 END IF;

    SELECT balance,subject_id INTO v_balance,v_subject FROM bursar.credit_accounts WHERE id=p_account_id FOR UPDATE;

 IF p_amount<0 THEN SELECT COALESCE(SUM(granted-consumed),0) INTO v_available FROM bursar.credit_lots WHERE account_id=p_account_id AND consumed<granted AND (expires_at IS NULL OR expires_at>now());
 IF v_available < -p_amount OR v_balance+p_amount<0 THEN RETURN QUERY SELECT NULL::uuid,v_balance,false,'insufficient_credits';
 RETURN;
 END IF;
 END IF;

 PERFORM set_config('bursar.mutation_context','internal',true);

 INSERT INTO bursar.credit_ledger_entries AS e(account_id,kind,amount,balance_after,idempotency_key,request_digest,operation) VALUES(p_account_id,p_kind,p_amount,v_balance+p_amount,p_idempotency_key,v_digest,p_operation) RETURNING e.id,e.balance_after INTO v_entry,v_balance;

 UPDATE bursar.credit_accounts SET balance=v_balance WHERE id=p_account_id;

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

            INSERT INTO bursar.credit_lot_allocations(debit_entry_id,lot_id,amount,allocation_kind)
            VALUES(v_entry,r.id,v_take,'spend');

            v_remaining:=v_remaining-v_take;

            EXIT WHEN v_remaining<=0;

        END LOOP;

    ELSE
        SELECT b.catalog_revision_id,b.bucket_key,b.priority
        INTO v_revision,v_bucket,v_priority
        FROM bursar.catalog_buckets b
        JOIN bursar.catalog_revisions cr ON cr.id=b.catalog_revision_id
        WHERE cr.status='active' AND b.is_default;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'active default bucket missing' USING ERRCODE='23503';

        END IF;

        v_expiry:=bursar.bucket_expiry_at(v_subject,v_revision,v_bucket);

        INSERT INTO bursar.credit_lots(
            account_id,source_entry_id,catalog_revision_id,bucket_key,
            priority,granted,expires_at
        )
        VALUES (
            p_account_id,v_entry,v_revision,v_bucket,v_priority,p_amount,v_expiry
        );

    END IF;

 RETURN QUERY SELECT v_entry,v_balance,false,NULL::text;

END $$;

CREATE FUNCTION bursar.consume_allowance(
    p_subject_id uuid,
    p_feature text,
    p_window_start timestamptz,
    p_window_end timestamptz,
    p_amount numeric
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid;

BEGIN
 IF p_amount<0 OR p_window_end<=p_window_start THEN RETURN false;
 END IF;

 v_account:=bursar.account_for_subject(p_subject_id);

 UPDATE bursar.allowance_windows AS aw SET consumed=aw.consumed+p_amount WHERE aw.account_id=v_account AND aw.feature=p_feature AND aw.window_start=p_window_start AND aw.window_end=p_window_end AND aw.consumed+aw.reserved+p_amount<=aw.allowance;

 RETURN FOUND;

END $$;
