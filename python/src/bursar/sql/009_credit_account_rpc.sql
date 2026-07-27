-- Account creation and credit posting RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.account_for_subject(
    p_subject_id uuid,
    p_kind text DEFAULT 'personal'
)
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
    p_minimum_balance numeric DEFAULT 0
)
RETURNS TABLE(
    entry_id uuid,
    balance_after numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid;
 v_old bursar.credit_ledger_entries;
 v_entry uuid;
 v_balance numeric;
 v_digest bytea;
 v_available numeric;
 v_remaining numeric;
 v_take numeric;
 v_lot record;
 v_revision uuid;
 v_effective_expiry timestamptz;
 v_bucket_key text;

BEGIN
    IF p_amount = 0 OR p_idempotency_key IS NULL OR p_idempotency_key = '' OR p_minimum_balance IS NULL THEN
    RETURN QUERY SELECT NULL::uuid, NULL::numeric, false, 'invalid_request';
 RETURN;

  END IF;

  v_account := bursar.account_for_subject(p_subject_id);

    v_digest := extensions.digest(convert_to(jsonb_build_object('amount',p_amount,'kind',p_kind::text,'operation',p_operation,'bucket',p_bucket_key,'catalog_revision_id',p_catalog_revision_id,'expires_at',p_expires_at,'minimum_balance',p_minimum_balance,'request',p_request)::text,'UTF8'),'sha256');

  SELECT * INTO v_old FROM bursar.credit_ledger_entries WHERE account_id = v_account AND idempotency_key = p_idempotency_key FOR UPDATE;

    IF FOUND THEN
        IF v_old.request_digest <> v_digest THEN RETURN QUERY SELECT NULL::uuid,NULL::numeric,false,'idempotency_conflict';

        ELSE RETURN QUERY SELECT v_old.id,v_old.balance_after,true,NULL::text;
 END IF;
 RETURN;

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
            RETURN QUERY SELECT NULL::uuid,v_balance,false,'insufficient_credits';
 RETURN;

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
