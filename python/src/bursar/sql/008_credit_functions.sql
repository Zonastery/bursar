CREATE FUNCTION bursar.account_for_subject(p_subject_id uuid, p_kind text DEFAULT 'personal')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_id uuid;
    v_created boolean := false;
    v_signup record;
    v_post record;
BEGIN
    IF p_subject_id IS NULL OR p_kind NOT IN ('personal', 'team') THEN
        RAISE EXCEPTION 'invalid account subject or kind' USING ERRCODE = '22023';
    END IF;
    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;
    INSERT INTO bursar.credit_accounts(subject_id, account_kind)
    VALUES (p_subject_id, p_kind)
    ON CONFLICT (subject_id, account_kind) DO NOTHING
    RETURNING id INTO v_id;
    v_created := FOUND;
    IF NOT v_created THEN
        SELECT id INTO v_id FROM bursar.credit_accounts
        WHERE subject_id = p_subject_id AND account_kind = p_kind FOR UPDATE;
    END IF;

    -- A signup grant is a one-time entitlement for a personal subject.  Keeping
    -- an explicit row makes retries and later catalogue changes harmless.
    IF v_created AND p_kind = 'personal' THEN
        SELECT sg.catalog_revision_id, sg.amount, sg.bucket_key
        INTO v_signup
        FROM bursar.catalog_signup_grants sg
        JOIN bursar.catalog_revisions cr ON cr.id = sg.catalog_revision_id
        WHERE cr.status = 'active';
        IF FOUND THEN
            SELECT * INTO v_post FROM bursar.post_credit(
                p_subject_id, 'grant', v_signup.amount, 'signup_grant',
                'signup:' || p_subject_id::text,
                jsonb_build_object('catalog_revision_id', v_signup.catalog_revision_id),
                v_signup.bucket_key, v_signup.catalog_revision_id
            );
            IF v_post.error_code IS NOT NULL THEN
                RAISE EXCEPTION 'signup grant failed: %', v_post.error_code USING ERRCODE = '23514';
            END IF;
            INSERT INTO bursar.signup_credit_grants(subject_id, catalog_revision_id, ledger_entry_id)
            VALUES (p_subject_id, v_signup.catalog_revision_id, v_post.entry_id);
        END IF;
    END IF;
    RETURN v_id;
END $$;

CREATE FUNCTION bursar.post_credit(p_subject_id uuid, p_kind bursar.ledger_entry_kind, p_amount numeric, p_operation text, p_idempotency_key text, p_request jsonb DEFAULT '{}'::jsonb, p_bucket_key text DEFAULT 'default', p_catalog_revision_id uuid DEFAULT NULL, p_expires_at timestamptz DEFAULT NULL, p_minimum_balance numeric DEFAULT 0)
RETURNS TABLE(entry_id uuid, balance_after numeric, replayed boolean, error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid; v_old bursar.credit_ledger_entries; v_entry uuid; v_balance numeric; v_digest bytea; v_available numeric; v_remaining numeric; v_take numeric; v_lot record; v_revision uuid; v_effective_expiry timestamptz; v_bucket_key text;
BEGIN
    IF p_amount = 0 OR p_idempotency_key IS NULL OR p_idempotency_key = '' OR p_minimum_balance IS NULL THEN
    RETURN QUERY SELECT NULL::uuid, NULL::numeric, false, 'invalid_request'; RETURN;
  END IF;
  v_account := bursar.account_for_subject(p_subject_id);
    v_digest := extensions.digest(convert_to(jsonb_build_object('amount',p_amount,'kind',p_kind::text,'operation',p_operation,'bucket',p_bucket_key,'catalog_revision_id',p_catalog_revision_id,'expires_at',p_expires_at,'minimum_balance',p_minimum_balance,'request',p_request)::text,'UTF8'),'sha256');
  SELECT * INTO v_old FROM bursar.credit_ledger_entries WHERE account_id = v_account AND idempotency_key = p_idempotency_key FOR UPDATE;
    IF FOUND THEN
        IF v_old.request_digest <> v_digest THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'idempotency_conflict';
        ELSE RETURN QUERY SELECT v_old.id,v_old.balance_after,true,NULL::text; END IF; RETURN;
    END IF;
    IF p_amount>0 THEN
        v_revision:=p_catalog_revision_id;
        IF v_revision IS NULL THEN
            SELECT id INTO v_revision
 FROM bursar.catalog_revisions
 WHERE status='active';
 END IF;
 v_bucket_key:=p_bucket_key;
 IF v_bucket_key IS NULL OR (
  v_bucket_key='default'
  AND NOT EXISTS (
   SELECT 1 FROM bursar.catalog_buckets
   WHERE catalog_revision_id=v_revision AND bucket_key='default'
  )
 ) THEN
  SELECT bucket_key INTO v_bucket_key
  FROM bursar.catalog_buckets
  WHERE catalog_revision_id=v_revision AND is_default;
 END IF;
        IF v_revision IS NULL OR NOT EXISTS (
            SELECT 1 FROM bursar.catalog_buckets
 WHERE catalog_revision_id=v_revision AND bucket_key=v_bucket_key
        ) THEN
            RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'missing_catalog_bucket';
            RETURN;
        END IF;
        v_effective_expiry:=p_expires_at;
        IF v_effective_expiry IS NULL THEN
            BEGIN
                v_effective_expiry:=bursar.bucket_expiry_at(
 p_subject_id,v_revision,v_bucket_key
                );
            EXCEPTION WHEN invalid_parameter_value THEN
                RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'expiry_context_required';
                RETURN;
            END;
        ELSIF v_effective_expiry<=now() THEN
            RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_expiry';
            RETURN;
        END IF;
    END IF;
  SELECT balance INTO v_balance FROM bursar.credit_accounts WHERE id = v_account FOR UPDATE;
    IF p_amount < 0 AND p_kind <> 'refund_clawback' THEN
        SELECT COALESCE(SUM(granted-consumed),0) INTO v_available FROM bursar.credit_lots WHERE account_id=v_account AND (expires_at IS NULL OR expires_at > now());
        IF v_balance + p_amount < p_minimum_balance
           OR (p_minimum_balance >= 0 AND v_available < -p_amount) THEN
            RETURN QUERY SELECT NULL::uuid,v_balance,false,'insufficient_credits'; RETURN;
        END IF;
    END IF;
  PERFORM set_config('bursar.mutation_context','internal',true);
  INSERT INTO bursar.credit_ledger_entries(account_id,kind,amount,balance_after,idempotency_key,request_digest,operation,metadata)
  VALUES(v_account,p_kind,p_amount,v_balance+p_amount,p_idempotency_key,v_digest,p_operation,p_request) RETURNING credit_ledger_entries.id,credit_ledger_entries.balance_after INTO v_entry,v_balance;
  UPDATE bursar.credit_accounts SET balance=v_balance WHERE id=v_account;
  IF p_amount < 0 THEN
    v_remaining := -p_amount;
    FOR v_lot IN
      SELECT id, granted-consumed AS available FROM bursar.credit_lots
      WHERE account_id=v_account AND consumed < granted AND (expires_at IS NULL OR expires_at > now())
      ORDER BY priority, expires_at NULLS LAST, created_at, id FOR UPDATE
    LOOP
      v_take := LEAST(v_remaining, v_lot.available);
      UPDATE bursar.credit_lots SET consumed=consumed+v_take WHERE id=v_lot.id;
        INSERT INTO bursar.credit_lot_allocations(debit_entry_id,lot_id,amount,allocation_kind)
        VALUES (
            v_entry,v_lot.id,v_take,
            CASE p_kind
                WHEN 'expiry' THEN 'expiry'
                WHEN 'revocation' THEN 'revocation'
                WHEN 'refund_clawback' THEN 'clawback'
                ELSE 'spend'
            END
        );
      v_remaining := v_remaining-v_take;
      EXIT WHEN v_remaining <= 0;
    END LOOP;
  END IF;
  IF p_amount > 0 THEN
    INSERT INTO bursar.credit_lots(account_id,source_entry_id,catalog_revision_id,bucket_key,priority,granted,expires_at)
 SELECT v_account,v_entry,v_revision,v_bucket_key,
               (SELECT priority FROM bursar.catalog_buckets
 WHERE catalog_revision_id=v_revision AND bucket_key=v_bucket_key),
               p_amount,v_effective_expiry;
  END IF;
  RETURN QUERY SELECT v_entry,v_balance,false,NULL::text;
END $$;

CREATE FUNCTION bursar.charge_usage(p_subject_id uuid,p_operation text,p_requested numeric,p_idempotency_key text,p_feature text DEFAULT NULL,p_model text DEFAULT NULL,p_region text DEFAULT NULL,p_allowance numeric DEFAULT 0,p_metadata jsonb DEFAULT '{}'::jsonb,p_allowance_requested numeric DEFAULT NULL)
RETURNS TABLE(charge_id uuid,ledger_entry_id uuid,charged numeric,allowance_covered numeric,replayed boolean,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid; v_existing bursar.credit_usage_charges; v_digest bytea; v_post record; v_id uuid; v_ledger_entry uuid;
BEGIN
  v_account:=bursar.account_for_subject(p_subject_id);
    p_allowance_requested:=COALESCE(p_allowance_requested,p_allowance);
    v_digest:=extensions.digest(convert_to(jsonb_build_object('operation',p_operation,'requested',bursar.digest_numeric_text(p_requested),'feature',p_feature,'model',p_model,'region',p_region,'allowance_requested',bursar.digest_numeric_text(p_allowance_requested),'allowance_covered',bursar.digest_numeric_text(p_allowance),'metadata',p_metadata)::text,'UTF8'),'sha256');
  SELECT * INTO v_existing FROM bursar.credit_usage_charges WHERE account_id=v_account AND idempotency_key=p_idempotency_key FOR UPDATE;
  IF FOUND THEN IF v_existing.request_digest<>v_digest THEN RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'idempotency_conflict'; ELSE RETURN QUERY SELECT v_existing.id,v_existing.ledger_entry_id,v_existing.charged,v_existing.allowance_covered,true,NULL::text; END IF; RETURN; END IF;
    IF p_requested < 0 OR p_allowance < 0 OR p_allowance > p_requested
       OR p_allowance_requested < p_allowance OR p_allowance_requested > p_requested
       OR p_idempotency_key IS NULL OR p_idempotency_key = ''
    THEN RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'invalid_request'; RETURN; END IF;
    IF p_requested-p_allowance > 0 THEN
        SELECT * INTO v_post FROM bursar.post_credit(p_subject_id,'usage',-(p_requested-p_allowance),p_operation,p_idempotency_key||':ledger',p_metadata);
        IF v_post.error_code IS NOT NULL THEN RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,v_post.error_code; RETURN; END IF;
        v_ledger_entry:=v_post.entry_id;
    END IF;
    INSERT INTO bursar.credit_usage_charges(account_id,operation,feature,model,region,requested,charged,allowance_requested,allowance_covered,ledger_entry_id,metadata,idempotency_key,request_digest) VALUES(v_account,p_operation,p_feature,p_model,p_region,p_requested,p_requested-p_allowance,p_allowance_requested,p_allowance,v_ledger_entry,p_metadata,p_idempotency_key,v_digest) RETURNING id INTO v_id;
    RETURN QUERY SELECT v_id,v_ledger_entry,p_requested-p_allowance,p_allowance,false,NULL::text;
END $$;

CREATE FUNCTION bursar.targeted_lot_debit(p_lot_id uuid,p_kind bursar.ledger_entry_kind,p_amount numeric,p_idempotency_key text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_lot bursar.credit_lots; v_balance numeric; v_entry uuid; v_old bursar.credit_ledger_entries; v_digest bytea;
BEGIN
  SELECT * INTO v_lot FROM bursar.credit_lots WHERE id=p_lot_id FOR UPDATE;
    v_digest := extensions.digest(convert_to(jsonb_build_object('lot_id',p_lot_id,'kind',p_kind::text,'amount',p_amount)::text,'UTF8'),'sha256');
  IF v_lot.id IS NULL OR p_amount <= 0 THEN RAISE EXCEPTION 'lot_unavailable'; END IF;
  SELECT * INTO v_old FROM bursar.credit_ledger_entries WHERE account_id=v_lot.account_id AND idempotency_key=p_idempotency_key FOR UPDATE;
  IF FOUND THEN
    IF v_old.request_digest <> v_digest THEN RAISE EXCEPTION 'idempotency_conflict'; END IF;
    RETURN v_old.id;
  END IF;
  IF v_lot.granted-v_lot.consumed < p_amount THEN RAISE EXCEPTION 'lot_unavailable'; END IF;
  SELECT balance INTO v_balance FROM bursar.credit_accounts WHERE id=v_lot.account_id FOR UPDATE;
  IF v_balance < p_amount THEN RAISE EXCEPTION 'insufficient_credits'; END IF;
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

CREATE FUNCTION bursar.expire_lots(p_limit integer DEFAULT 100) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_count integer:=0; r record;
BEGIN
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN RAISE EXCEPTION 'invalid_batch_limit'; END IF;
  FOR r IN SELECT id,granted-consumed AS amount FROM bursar.credit_lots WHERE expires_at <= now() AND consumed < granted ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT p_limit LOOP
    PERFORM bursar.targeted_lot_debit(r.id,'expiry',r.amount,'expiry:'||r.id::text); v_count:=v_count+1;
  END LOOP; RETURN v_count;
END $$;

CREATE FUNCTION bursar.revoke_lot(p_lot_id uuid,p_amount numeric,p_idempotency_key text)
RETURNS TABLE(entry_id uuid,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
  RETURN QUERY SELECT bursar.targeted_lot_debit(p_lot_id,'revocation',p_amount,p_idempotency_key),NULL::text;
EXCEPTION WHEN OTHERS THEN
  RETURN QUERY SELECT NULL::uuid,CASE WHEN SQLERRM='idempotency_conflict' THEN 'idempotency_conflict' ELSE 'lot_unavailable' END;
END $$;

CREATE FUNCTION bursar.refund_credit(p_subject_id uuid,p_amount numeric,p_idempotency_key text,p_original_entry_id uuid)
RETURNS TABLE(entry_id uuid,balance_after numeric,replayed boolean,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_original bursar.credit_ledger_entries; v_existing bursar.credit_ledger_entries; v_result record;
BEGIN
  IF p_amount <= 0 THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_request'; RETURN; END IF;
 SELECT * INTO v_original
 FROM bursar.credit_ledger_entries
 WHERE id=p_original_entry_id
 AND account_id=bursar.account_for_subject(p_subject_id)
 AND amount < 0
 FOR UPDATE;
 IF NOT FOUND THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'missing_original'; RETURN; END IF;
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

CREATE FUNCTION bursar.deduct_team(p_team_id uuid,p_amount numeric,p_idempotency_key text,p_operation text DEFAULT 'team_usage')
RETURNS TABLE(entry_id uuid,balance_after numeric,replayed boolean,error_code text)
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

CREATE FUNCTION bursar.post_credit_account(p_account_id uuid,p_kind bursar.ledger_entry_kind,p_amount numeric,p_operation text,p_idempotency_key text)
RETURNS TABLE(entry_id uuid,balance_after numeric,replayed boolean,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_old bursar.credit_ledger_entries; v_balance numeric; v_entry uuid; v_digest bytea; v_available numeric; r record; v_remaining numeric; v_take numeric; v_subject uuid; v_revision uuid; v_bucket text; v_expiry timestamptz; v_priority integer;
BEGIN
 IF p_account_id IS NULL OR p_amount=0 OR p_idempotency_key IS NULL THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'invalid_request'; RETURN; END IF;
    v_digest:=extensions.digest(convert_to(jsonb_build_object('amount',p_amount,'kind',p_kind::text,'operation',p_operation)::text,'UTF8'),'sha256');
 SELECT * INTO v_old FROM bursar.credit_ledger_entries WHERE account_id=p_account_id AND idempotency_key=p_idempotency_key FOR UPDATE;
 IF FOUND THEN IF v_old.request_digest<>v_digest THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'idempotency_conflict'; ELSE RETURN QUERY SELECT v_old.id,v_old.balance_after,true,NULL::text; END IF; RETURN; END IF;
    SELECT balance,subject_id INTO v_balance,v_subject FROM bursar.credit_accounts WHERE id=p_account_id FOR UPDATE;
 IF p_amount<0 THEN SELECT COALESCE(SUM(granted-consumed),0) INTO v_available FROM bursar.credit_lots WHERE account_id=p_account_id AND consumed<granted AND (expires_at IS NULL OR expires_at>now()); IF v_available < -p_amount OR v_balance+p_amount<0 THEN RETURN QUERY SELECT NULL::uuid,v_balance,false,'insufficient_credits'; RETURN; END IF; END IF;
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

CREATE FUNCTION bursar.consume_allowance(p_subject_id uuid,p_feature text,p_window_start timestamptz,p_window_end timestamptz,p_amount numeric)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid;
BEGIN
 IF p_amount<0 OR p_window_end<=p_window_start THEN RETURN false; END IF;
 v_account:=bursar.account_for_subject(p_subject_id);
 UPDATE bursar.allowance_windows AS aw SET consumed=aw.consumed+p_amount WHERE aw.account_id=v_account AND aw.feature=p_feature AND aw.window_start=p_window_start AND aw.window_end=p_window_end AND aw.consumed+aw.reserved+p_amount<=aw.allowance;
 RETURN FOUND;
END $$;

CREATE FUNCTION bursar.charge_usage_with_window(p_subject_id uuid,p_operation text,p_requested numeric,p_idempotency_key text,p_feature text,p_window_start timestamptz,p_window_end timestamptz,p_allowance numeric,p_model text DEFAULT NULL,p_region text DEFAULT NULL,p_metadata jsonb DEFAULT '{}'::jsonb,p_charge_feature text DEFAULT NULL)
RETURNS TABLE(charge_id uuid,ledger_entry_id uuid,charged numeric,allowance_covered numeric,replayed boolean,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_result record; v_allowance numeric; v_account uuid; v_consumed boolean:=false;
BEGIN
    v_allowance:=LEAST(p_requested,GREATEST(p_allowance,0));
    v_account:=bursar.account_for_subject(p_subject_id);
    IF EXISTS (SELECT 1 FROM bursar.credit_usage_charges WHERE account_id=v_account AND idempotency_key=p_idempotency_key) THEN
        SELECT * INTO v_result
        FROM bursar.charge_usage(
        p_subject_id,p_operation,p_requested,p_idempotency_key,COALESCE(p_charge_feature,p_feature),p_model,p_region,
        (SELECT c.allowance_covered FROM bursar.credit_usage_charges c WHERE c.account_id=v_account AND c.idempotency_key=p_idempotency_key),
        p_metadata,
        (SELECT c.allowance_requested FROM bursar.credit_usage_charges c WHERE c.account_id=v_account AND c.idempotency_key=p_idempotency_key)
        );
        RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;
        RETURN;
    END IF;
    IF v_allowance>0 AND NOT bursar.consume_allowance(p_subject_id,p_feature,p_window_start,p_window_end,v_allowance) THEN v_allowance:=0; END IF;
    v_consumed:=v_allowance>0;
    SELECT * INTO v_result FROM bursar.charge_usage(p_subject_id,p_operation,p_requested,p_idempotency_key,COALESCE(p_charge_feature,p_feature),p_model,p_region,v_allowance,p_metadata,v_allowance);
    IF v_result.error_code IS NOT NULL AND v_consumed THEN
        UPDATE bursar.allowance_windows AS aw
        SET consumed=aw.consumed-v_allowance
        WHERE aw.account_id=v_account AND aw.feature=p_feature AND aw.window_start=p_window_start
          AND aw.window_end=p_window_end AND aw.consumed>=v_allowance;
    END IF;
    RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;
END $$;
