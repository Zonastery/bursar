-- Lot debit, expiry, revocation, and refund RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.targeted_lot_debit(
    p_lot_id uuid,
    p_kind bursar.ledger_entry_kind,
    p_amount numeric,
    p_idempotency_key text
)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_lot bursar.credit_lots;
 v_balance numeric;
 v_entry uuid;
 v_old bursar.credit_ledger_entries;
 v_digest bytea;

BEGIN
  SELECT * INTO v_lot FROM bursar.credit_lots WHERE id=p_lot_id FOR UPDATE;

    v_digest := extensions.digest(convert_to(jsonb_build_object('lot_id',p_lot_id,'kind',p_kind::text,'amount',p_amount)::text,'UTF8'),'sha256');

  IF v_lot.id IS NULL OR p_amount <= 0 THEN RAISE EXCEPTION 'lot_unavailable';
 END IF;

  SELECT * INTO v_old FROM bursar.credit_ledger_entries WHERE account_id=v_lot.account_id AND idempotency_key=p_idempotency_key FOR UPDATE;

  IF FOUND THEN
    IF v_old.request_digest <> v_digest THEN RAISE EXCEPTION 'idempotency_conflict';
 END IF;

    RETURN v_old.id;

  END IF;

  IF v_lot.granted-v_lot.consumed < p_amount THEN RAISE EXCEPTION 'lot_unavailable';
 END IF;

  SELECT balance INTO v_balance FROM bursar.credit_accounts WHERE id=v_lot.account_id FOR UPDATE;

  IF v_balance < p_amount THEN RAISE EXCEPTION 'insufficient_credits';
 END IF;

  PERFORM set_config('bursar.mutation_context','internal',true);

  INSERT INTO bursar.credit_ledger_entries(account_id,kind,amount,balance_after,idempotency_key,request_digest,operation,metadata) VALUES(v_lot.account_id,p_kind,-p_amount,v_balance-p_amount,p_idempotency_key,v_digest,p_kind::text,jsonb_build_object('lot_id',p_lot_id)) RETURNING id INTO v_entry;

  UPDATE bursar.credit_accounts SET balance=balance-p_amount WHERE id=v_lot.account_id;

  UPDATE bursar.credit_lots SET consumed=consumed+p_amount WHERE id=p_lot_id;

    INSERT INTO bursar.credit_lot_allocations(debit_entry_id,lot_id,amount,allocation_kind)
    VALUES (
        v_entry,p_lot_id,p_amount,
        CASE p_kind
            WHEN 'refund_clawback' THEN 'clawback'
            ELSE p_kind::text
        END
    );

  RETURN v_entry;

END $$;

CREATE FUNCTION bursar.expire_lots(
    p_limit integer DEFAULT 100
) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_count integer:=0;
 r record;

BEGIN
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN RAISE EXCEPTION 'invalid_batch_limit';
 END IF;

  FOR r IN SELECT id,granted-consumed AS amount FROM bursar.credit_lots WHERE expires_at <= now() AND consumed < granted ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT p_limit LOOP
    PERFORM bursar.targeted_lot_debit(r.id,'expiry',r.amount,'expiry:'||r.id::text);
 v_count:=v_count+1;

  END LOOP;
 RETURN v_count;

END $$;

CREATE FUNCTION bursar.revoke_lot(
    p_lot_id uuid,
    p_amount numeric,
    p_idempotency_key text
)
RETURNS TABLE(
    entry_id uuid,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
  RETURN QUERY SELECT bursar.targeted_lot_debit(p_lot_id,'revocation',p_amount,p_idempotency_key),NULL::text;

EXCEPTION WHEN OTHERS THEN
  RETURN QUERY SELECT NULL::uuid,CASE WHEN SQLERRM='idempotency_conflict' THEN 'idempotency_conflict' ELSE 'lot_unavailable' END;

END $$;

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
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_original bursar.credit_ledger_entries;
 v_existing bursar.credit_ledger_entries;
 v_result record;

BEGIN
  IF p_amount <= 0 THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_request';
 RETURN;
 END IF;

 SELECT * INTO v_original
 FROM bursar.credit_ledger_entries
 WHERE id=p_original_entry_id
 AND account_id=bursar.account_for_subject(p_subject_id)
 AND amount < 0
 FOR UPDATE;

 IF NOT FOUND THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'missing_original';
 RETURN;
 END IF;

 SELECT * INTO v_existing
 FROM bursar.credit_ledger_entries
 WHERE account_id=v_original.account_id
 AND idempotency_key=p_idempotency_key
 FOR UPDATE;

 IF FOUND THEN
  IF v_existing.kind='refund'
  AND v_existing.amount=p_amount
  AND v_existing.reference_entry_id=p_original_entry_id THEN
   RETURN QUERY
   SELECT v_existing.id,v_existing.balance_after,true,NULL::text;

  ELSE
   RETURN QUERY
   SELECT NULL::uuid,NULL::numeric,false,'idempotency_conflict';

  END IF;

  RETURN;

 END IF;

 IF COALESCE((
  SELECT sum(amount)
  FROM bursar.credit_ledger_entries
  WHERE reference_entry_id=p_original_entry_id
  AND kind='refund'
 ),0)+p_amount > -v_original.amount THEN
  RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'refund_exceeds_original';

  RETURN;

 END IF;

 SELECT * INTO v_result FROM bursar.post_credit(p_subject_id,'refund',p_amount,'refund',p_idempotency_key,jsonb_build_object('original_entry_id',p_original_entry_id));

 IF v_result.error_code IS NULL THEN
  PERFORM set_config('bursar.mutation_context','internal',true);

  UPDATE bursar.credit_ledger_entries
  SET reference_entry_id=p_original_entry_id
  WHERE id=v_result.entry_id
  AND reference_entry_id IS NULL;

 END IF;

  RETURN QUERY SELECT v_result.entry_id,v_result.balance_after,v_result.replayed,v_result.error_code;

END $$;
