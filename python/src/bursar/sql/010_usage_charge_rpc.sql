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
    p_allowance_requested numeric DEFAULT NULL,
    p_catalog_revision_id uuid DEFAULT NULL,
    p_plan_id uuid DEFAULT NULL,
    p_rate_card_key text DEFAULT NULL,
    p_minimum_balance numeric DEFAULT NULL,
    p_event_at timestamptz DEFAULT now(),
    p_measures jsonb DEFAULT '{}'::jsonb,
    p_dimensions jsonb DEFAULT '{}'::jsonb
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
 v_revision uuid;
 v_plan uuid;
 v_rate_card text;

BEGIN
  v_account:=bursar.account_for_subject(p_subject_id);

    p_allowance_requested:=COALESCE(p_allowance_requested,p_allowance);

    IF NOT bursar.is_finite_numeric(p_requested)
       OR NOT bursar.is_finite_numeric(p_allowance)
       OR NOT bursar.is_finite_numeric(p_allowance_requested)
       OR NOT bursar.is_nonempty_text(p_operation)
       OR p_event_at IS NULL
       OR (p_plan_id IS NOT NULL AND p_catalog_revision_id IS NULL)
       OR (p_plan_id IS NULL AND p_rate_card_key IS NOT NULL)
       OR jsonb_typeof(COALESCE(p_metadata,'{}'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_measures,'{}'::jsonb)) <> 'object'
       OR jsonb_typeof(COALESCE(p_dimensions,'{}'::jsonb)) <> 'object'
    THEN
        RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'invalid_request';
        RETURN;
    END IF;

    v_digest:=extensions.digest(convert_to(jsonb_build_object('operation',p_operation,'requested',bursar.digest_numeric_text(p_requested),'feature',p_feature,'model',p_model,'region',p_region,'allowance_requested',bursar.digest_numeric_text(p_allowance_requested),'allowance_covered',bursar.digest_numeric_text(p_allowance),'catalog_revision_id',p_catalog_revision_id,'plan_id',p_plan_id,'rate_card_key',p_rate_card_key,'minimum_balance',bursar.digest_numeric_text(p_minimum_balance),'measures',COALESCE(p_measures,'{}'::jsonb),'dimensions',COALESCE(p_dimensions,'{}'::jsonb),'metadata',p_metadata)::text,'UTF8'),'sha256');

  PERFORM 1 FROM bursar.credit_accounts WHERE id=v_account FOR UPDATE;

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

    IF p_catalog_revision_id IS NOT NULL
       AND p_plan_id IS NOT NULL
    THEN
        SELECT
            plan.catalog_revision_id,
            plan.id,
            plan.rate_card
        INTO v_revision, v_plan, v_rate_card
        FROM bursar.catalog_plans AS plan
        WHERE plan.catalog_revision_id = p_catalog_revision_id
          AND plan.id = p_plan_id;

        IF NOT FOUND
           OR (
               p_rate_card_key IS NOT NULL
               AND p_rate_card_key <> v_rate_card
           )
        THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                NULL::uuid,
                0::numeric,
                0::numeric,
                false,
                'invalid_plan_context';
            RETURN;
        END IF;
    ELSIF p_catalog_revision_id IS NOT NULL THEN
        SELECT revision.id
        INTO v_revision
        FROM bursar.catalog_revisions AS revision
        WHERE revision.id = p_catalog_revision_id;

        IF NOT FOUND THEN
            RETURN QUERY
            SELECT
                NULL::uuid,
                NULL::uuid,
                0::numeric,
                0::numeric,
                false,
                'invalid_plan_context';
            RETURN;
        END IF;
    ELSE
        SELECT
            assignment.catalog_revision_id,
            assignment.plan_id,
            plan.rate_card
        INTO v_revision,v_plan,v_rate_card
        FROM bursar.account_plan_assignments AS assignment
        JOIN bursar.catalog_plans AS plan
          ON plan.id=assignment.plan_id
         AND plan.catalog_revision_id=assignment.catalog_revision_id
        WHERE assignment.account_id=v_account
          AND assignment.starts_at<=now()
          AND (assignment.ends_at IS NULL OR assignment.ends_at>now());
    END IF;

    IF p_requested-p_allowance > 0 THEN
        SELECT *
        INTO v_post
        FROM bursar.post_credit(
            p_subject_id,
            'usage',
            -(p_requested-p_allowance),
            p_operation,
            p_idempotency_key||':ledger',
            p_metadata,
            'default',
            v_revision,
            NULL,
            p_minimum_balance
        );

        IF v_post.error_code IS NOT NULL THEN RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,v_post.error_code;
 RETURN;
 END IF;

        v_ledger_entry:=v_post.entry_id;

    END IF;

    PERFORM set_config('bursar.mutation_context','internal',true);

    INSERT INTO bursar.credit_usage_charges(
        account_id,operation,event_at,measures,dimensions,feature,model,region,requested,charged,
        allowance_requested,allowance_covered,catalog_revision_id,plan_id,
        rate_card_key,pricing_snapshot,ledger_entry_id,metadata,idempotency_key,
        request_digest
    )
    VALUES(
        v_account,p_operation,p_event_at,
        COALESCE(p_measures,'{}'::jsonb),
        COALESCE(p_dimensions,'{}'::jsonb)
            || jsonb_strip_nulls(
                jsonb_build_object('model',p_model,'region',p_region)
            ),
        p_feature,p_model,p_region,p_requested,p_requested-p_allowance,
        p_allowance_requested,p_allowance,v_revision,v_plan,v_rate_card,
        jsonb_build_object(
            'requested',bursar.digest_numeric_text(p_requested),
            'allowance_covered',bursar.digest_numeric_text(p_allowance),
            'charged',bursar.digest_numeric_text(p_requested-p_allowance)
        ),
        v_ledger_entry,COALESCE(p_metadata,'{}'::jsonb),p_idempotency_key,v_digest
    )
    RETURNING id INTO v_id;

    RETURN QUERY SELECT v_id,v_ledger_entry,p_requested-p_allowance,p_allowance,false,NULL::text;

END $$;
