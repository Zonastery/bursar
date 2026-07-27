-- Usage charge and idempotency RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.charge_usage(
    p_subject_id uuid,
    p_operation text,
    p_requested numeric,
    p_idempotency_key text,
    p_feature text DEFAULT NULL,
    p_model text DEFAULT NULL,
    p_region text DEFAULT NULL,
    p_allowance numeric DEFAULT 0,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_allowance_requested numeric DEFAULT NULL
)
RETURNS TABLE(
    charge_id uuid,
    ledger_entry_id uuid,
    charged numeric,
    allowance_covered numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid;
 v_existing bursar.credit_usage_charges;
 v_digest bytea;
 v_post record;
 v_id uuid;
 v_ledger_entry uuid;

BEGIN
  v_account:=bursar.account_for_subject(p_subject_id);

    p_allowance_requested:=COALESCE(p_allowance_requested,p_allowance);

    v_digest:=extensions.digest(convert_to(jsonb_build_object('operation',p_operation,'requested',bursar.digest_numeric_text(p_requested),'feature',p_feature,'model',p_model,'region',p_region,'allowance_requested',bursar.digest_numeric_text(p_allowance_requested),'allowance_covered',bursar.digest_numeric_text(p_allowance),'metadata',p_metadata)::text,'UTF8'),'sha256');

  SELECT * INTO v_existing FROM bursar.credit_usage_charges WHERE account_id=v_account AND idempotency_key=p_idempotency_key FOR UPDATE;

  IF FOUND THEN IF v_existing.request_digest<>v_digest THEN RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'idempotency_conflict';
 ELSE RETURN QUERY SELECT v_existing.id,v_existing.ledger_entry_id,v_existing.charged,v_existing.allowance_covered,true,NULL::text;
 END IF;
 RETURN;
 END IF;

    IF p_requested < 0 OR p_allowance < 0 OR p_allowance > p_requested
       OR p_allowance_requested < p_allowance OR p_allowance_requested > p_requested
       OR p_idempotency_key IS NULL OR p_idempotency_key = ''
    THEN RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'invalid_request';
 RETURN;
 END IF;

    IF p_requested-p_allowance > 0 THEN
        SELECT * INTO v_post FROM bursar.post_credit(p_subject_id,'usage',-(p_requested-p_allowance),p_operation,p_idempotency_key||':ledger',p_metadata);

        IF v_post.error_code IS NOT NULL THEN RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,v_post.error_code;
 RETURN;
 END IF;

        v_ledger_entry:=v_post.entry_id;

    END IF;

    INSERT INTO bursar.credit_usage_charges(account_id,operation,feature,model,region,requested,charged,allowance_requested,allowance_covered,ledger_entry_id,metadata,idempotency_key,request_digest) VALUES(v_account,p_operation,p_feature,p_model,p_region,p_requested,p_requested-p_allowance,p_allowance_requested,p_allowance,v_ledger_entry,p_metadata,p_idempotency_key,v_digest) RETURNING id INTO v_id;

    RETURN QUERY SELECT v_id,v_ledger_entry,p_requested-p_allowance,p_allowance,false,NULL::text;

END $$;
