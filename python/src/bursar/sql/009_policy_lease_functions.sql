CREATE FUNCTION bursar.create_lease(p_subject_id uuid,p_operation text,p_estimate numeric,p_idempotency_key text,p_feature text DEFAULT NULL,p_reserved_calls integer DEFAULT 0,p_minimum_balance numeric DEFAULT NULL,p_ttl interval DEFAULT interval '10 minutes',p_policy_snapshot jsonb DEFAULT '{}'::jsonb,p_metadata jsonb DEFAULT '{}'::jsonb)
RETURNS TABLE(lease_id uuid,status bursar.lease_status,reserved_amount numeric,reserved_calls integer,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid; v_existing bursar.credit_leases; v_digest bytea; v_balance numeric; v_expired numeric; v_reserved numeric; v_id uuid; v_rev uuid; v_window_start timestamptz; v_window_end timestamptz; v_window_limit integer; v_policy record; v_max_concurrent integer;
BEGIN
    IF p_estimate <= 0 OR p_reserved_calls < 0 OR p_ttl <= interval '0'
       OR p_idempotency_key IS NULL OR p_idempotency_key = ''
       OR (p_reserved_calls > 0 AND p_feature IS NULL)
    THEN RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'invalid_request'; RETURN; END IF;
    v_account:=bursar.account_for_subject(p_subject_id);
    SELECT * INTO v_policy FROM bursar.effective_subject_policy(p_subject_id,p_operation,p_feature);
    IF FOUND THEN
        IF p_minimum_balance IS NOT NULL AND p_minimum_balance <> v_policy.minimum_balance THEN
            RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'policy_mismatch'; RETURN;
        END IF;
        p_minimum_balance := v_policy.minimum_balance;
        v_max_concurrent := v_policy.max_concurrent;
    ELSE
        p_minimum_balance := COALESCE(p_minimum_balance, 0);
    END IF;
    v_digest:=extensions.digest(convert_to(jsonb_build_object('operation',p_operation,'estimate',p_estimate,'feature',p_feature,'calls',p_reserved_calls,'minimum_balance',p_minimum_balance,'policy',p_policy_snapshot,'metadata',p_metadata)::text,'UTF8'),'sha256');
 SELECT * INTO v_existing FROM bursar.credit_leases WHERE account_id=v_account AND idempotency_key=p_idempotency_key FOR UPDATE;
 IF FOUND THEN IF v_existing.request_digest<>v_digest THEN RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'idempotency_conflict'; ELSE RETURN QUERY SELECT v_existing.id,v_existing.status,v_existing.reserved_amount,v_existing.reserved_calls,NULL::text; END IF; RETURN; END IF;
 SELECT balance INTO v_balance FROM bursar.credit_accounts WHERE id=v_account FOR UPDATE;
 SELECT COALESCE(SUM(granted-consumed),0) INTO v_expired
 FROM bursar.credit_lots
 WHERE account_id=v_account AND consumed<granted AND expires_at<=now();
 v_balance := v_balance - v_expired;
 SELECT COALESCE(SUM(cl.reserved_amount),0) INTO v_reserved FROM bursar.credit_leases cl WHERE cl.account_id=v_account AND cl.status='active' AND cl.expires_at > now();
    IF v_balance-v_reserved-p_estimate < p_minimum_balance THEN RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'insufficient_headroom'; RETURN; END IF;
    IF v_max_concurrent IS NOT NULL AND (SELECT count(*) FROM bursar.credit_leases cl WHERE cl.account_id=v_account AND cl.operation=p_operation AND cl.status='active' AND cl.expires_at>now()) >= v_max_concurrent THEN
        RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'max_concurrent_reached'; RETURN;
    END IF;
 IF p_feature IS NOT NULL AND p_reserved_calls>0 THEN
        SELECT window_start,window_end,limit_value INTO v_window_start,v_window_end,v_window_limit FROM bursar.feature_call_windows WHERE account_id=v_account AND feature=p_feature AND window_start<=now() AND window_end>now() ORDER BY window_start DESC LIMIT 1 FOR UPDATE;
  IF v_window_start IS NULL THEN RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'missing_feature_window'; RETURN; END IF;
        IF v_window_limit IS NOT NULL AND (SELECT admitted FROM bursar.feature_call_windows WHERE account_id=v_account AND feature=p_feature AND window_start=v_window_start)+p_reserved_calls>v_window_limit THEN RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'limit_exceeded'; RETURN; END IF;
  UPDATE bursar.feature_call_windows SET reserved=reserved+p_reserved_calls,admitted=admitted+p_reserved_calls WHERE account_id=v_account AND feature=p_feature AND window_start=v_window_start;
 END IF;
 SELECT cr.id INTO v_rev FROM bursar.catalog_revisions cr WHERE cr.status='active';
    INSERT INTO bursar.credit_leases(account_id,operation,feature,policy_snapshot,metadata,catalog_revision_id,reserved_amount,minimum_balance,max_concurrent,reserved_calls,feature_window_start,feature_window_end,expires_at,idempotency_key,request_digest) VALUES(v_account,p_operation,p_feature,p_policy_snapshot,p_metadata,v_rev,p_estimate,p_minimum_balance,v_max_concurrent,p_reserved_calls,v_window_start,v_window_end,now()+p_ttl,p_idempotency_key,v_digest) RETURNING id INTO v_id;
 RETURN QUERY SELECT v_id,'active'::bursar.lease_status,p_estimate,p_reserved_calls,NULL::text;
END $$;

CREATE FUNCTION bursar.settle_lease(p_subject_id uuid,p_lease_id uuid,p_actual numeric,p_idempotency_key text)
RETURNS TABLE(ledger_entry_id uuid,settled_amount numeric,replayed boolean,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid; v_lease bursar.credit_leases; v_post record; v_ledger_entry uuid; v_balance numeric; v_other numeric;
BEGIN
 IF p_actual < 0 THEN RETURN QUERY SELECT NULL::uuid,0::numeric,false,'invalid_request'; RETURN; END IF;
 v_account:=bursar.account_for_subject(p_subject_id);
 SELECT * INTO v_lease FROM bursar.credit_leases WHERE id=p_lease_id AND account_id=v_account FOR UPDATE;
 IF NOT FOUND THEN RETURN QUERY SELECT NULL::uuid,0::numeric,false,'missing_lease'; RETURN; END IF;
 IF v_lease.status='settled' THEN RETURN QUERY SELECT v_lease.ledger_entry_id,COALESCE(v_lease.settled_amount,0),true,NULL::text; RETURN; END IF;
 IF v_lease.status='released' THEN RETURN QUERY SELECT NULL::uuid,0::numeric,false,'released_lease'; RETURN; END IF;
 IF v_lease.status='expired' OR v_lease.expires_at <= now() THEN
   IF v_lease.status='active' AND v_lease.feature IS NOT NULL AND v_lease.feature_window_start IS NOT NULL THEN
     PERFORM bursar.release_feature_capacity(p_subject_id,v_lease.feature,v_lease.feature_window_start,v_lease.reserved_calls);
   END IF;
   UPDATE bursar.credit_leases SET status='expired' WHERE id=v_lease.id;
   RETURN QUERY SELECT NULL::uuid,0::numeric,false,'expired_lease'; RETURN;
 END IF;
 IF p_actual > v_lease.reserved_amount THEN
   SELECT balance INTO v_balance FROM bursar.credit_accounts WHERE id=v_account FOR UPDATE;
   SELECT COALESCE(SUM(cl.reserved_amount),0)-v_lease.reserved_amount INTO v_other FROM bursar.credit_leases cl WHERE cl.account_id=v_account AND cl.status='active' AND cl.id<>v_lease.id AND cl.expires_at > now();
   IF v_balance-v_other-p_actual < v_lease.minimum_balance THEN RETURN QUERY SELECT NULL::uuid,0::numeric,false,'insufficient_headroom'; RETURN; END IF;
 END IF;
    IF p_actual > 0 THEN SELECT * INTO v_post FROM bursar.post_credit(p_subject_id,'usage',-p_actual,'lease_settlement',p_idempotency_key,jsonb_build_object('lease_id',p_lease_id),'default',NULL,NULL,v_lease.minimum_balance); IF v_post.error_code IS NOT NULL THEN RETURN QUERY SELECT NULL::uuid,0::numeric,false,v_post.error_code; RETURN; END IF; v_ledger_entry:=v_post.entry_id; END IF;
UPDATE bursar.credit_leases SET status='settled',settled_amount=p_actual,ledger_entry_id=v_ledger_entry WHERE id=v_lease.id;
 IF v_lease.feature IS NOT NULL AND v_lease.feature_window_start IS NOT NULL THEN PERFORM bursar.consume_feature_capacity(p_subject_id,v_lease.feature,v_lease.feature_window_start,v_lease.reserved_calls); END IF;
RETURN QUERY SELECT v_ledger_entry,p_actual,false,NULL::text;
END $$;

CREATE FUNCTION bursar.expire_leases(p_limit integer DEFAULT 100)
RETURNS TABLE(expired integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE r record; v_count integer := 0;
BEGIN
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
    RETURN QUERY SELECT 0; RETURN;
  END IF;
  FOR r IN SELECT id, account_id, feature, feature_window_start, reserved_calls FROM bursar.credit_leases
           WHERE status='active' AND expires_at <= now()
           ORDER BY expires_at, id FOR UPDATE SKIP LOCKED LIMIT p_limit
  LOOP
    IF r.feature IS NOT NULL AND r.feature_window_start IS NOT NULL THEN
      UPDATE bursar.feature_call_windows
         SET reserved=GREATEST(reserved-r.reserved_calls,0), admitted=GREATEST(admitted-r.reserved_calls,0)
       WHERE account_id=r.account_id AND feature=r.feature AND window_start=r.feature_window_start AND reserved >= r.reserved_calls;
    END IF;
    UPDATE bursar.credit_leases SET status='expired' WHERE id=r.id AND status='active';
    v_count := v_count + 1;
  END LOOP;
  RETURN QUERY SELECT v_count;
END $$;

CREATE FUNCTION bursar.release_lease(p_subject_id uuid,p_lease_id uuid) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid; v_status bursar.lease_status; v_feature text; v_window_start timestamptz; v_calls integer;
BEGIN
 v_account:=bursar.account_for_subject(p_subject_id); SELECT status,feature,feature_window_start,reserved_calls INTO v_status,v_feature,v_window_start,v_calls FROM bursar.credit_leases WHERE id=p_lease_id AND account_id=v_account FOR UPDATE;
 IF NOT FOUND THEN RETURN 'missing_lease'; END IF;
 IF v_status='active' THEN UPDATE bursar.credit_leases SET status='released' WHERE id=p_lease_id; IF v_feature IS NOT NULL AND v_window_start IS NOT NULL THEN PERFORM bursar.release_feature_capacity(p_subject_id,v_feature,v_window_start,v_calls); END IF; RETURN 'released'; END IF;
 RETURN v_status::text;
END $$;

CREATE FUNCTION bursar.reserve_feature_capacity(p_subject_id uuid,p_feature text,p_window_start timestamptz,p_window_end timestamptz,p_limit integer,p_calls integer DEFAULT 1)
RETURNS TABLE(admitted boolean,reserved integer,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid; v_window bursar.feature_call_windows;
BEGIN
 IF p_window_end <= p_window_start OR p_calls < 1 OR p_limit < 0 THEN RETURN QUERY SELECT false,0,'invalid_request'; RETURN; END IF;
 v_account:=bursar.account_for_subject(p_subject_id);
 INSERT INTO bursar.feature_call_windows(account_id,feature,window_start,window_end,limit_value) VALUES(v_account,p_feature,p_window_start,p_window_end,p_limit) ON CONFLICT (account_id,feature,window_start) DO UPDATE SET window_end=EXCLUDED.window_end,limit_value=EXCLUDED.limit_value RETURNING * INTO v_window;
    IF v_window.limit_value IS NOT NULL AND v_window.admitted+p_calls > v_window.limit_value THEN RETURN QUERY SELECT false,v_window.reserved,'limit_exceeded'; RETURN; END IF;
 UPDATE bursar.feature_call_windows AS fw SET reserved=fw.reserved+p_calls,admitted=fw.admitted+p_calls WHERE fw.account_id=v_account AND fw.feature=p_feature AND fw.window_start=p_window_start;
 RETURN QUERY SELECT true,p_calls,NULL::text;
END $$;

CREATE FUNCTION bursar.consume_feature_capacity(p_subject_id uuid,p_feature text,p_window_start timestamptz,p_calls integer DEFAULT 1) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid;
BEGIN
    IF p_calls IS NULL OR p_calls < 1 THEN RETURN false; END IF;
    v_account:=bursar.account_for_subject(p_subject_id);
    UPDATE bursar.feature_call_windows AS fw
    SET reserved=fw.reserved-p_calls,consumed=fw.consumed+p_calls
    WHERE fw.account_id=v_account AND fw.feature=p_feature AND fw.window_start=p_window_start AND fw.reserved >= p_calls;
    RETURN FOUND;
END $$;
CREATE FUNCTION bursar.release_feature_capacity(p_subject_id uuid,p_feature text,p_window_start timestamptz,p_calls integer DEFAULT 1) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_account uuid;
BEGIN
    IF p_calls IS NULL OR p_calls < 1 THEN RETURN false; END IF;
    v_account:=bursar.account_for_subject(p_subject_id);
    UPDATE bursar.feature_call_windows AS fw
    SET reserved=fw.reserved-p_calls,admitted=fw.admitted-p_calls
    WHERE fw.account_id=v_account AND fw.feature=p_feature AND fw.window_start=p_window_start AND fw.reserved >= p_calls;
    RETURN FOUND;
END $$;

CREATE FUNCTION bursar.create_lease_with_feature_window(p_subject_id uuid,p_operation text,p_estimate numeric,p_idempotency_key text,p_feature text,p_window_start timestamptz,p_window_end timestamptz,p_limit integer,p_reserved_calls integer DEFAULT 1)
RETURNS TABLE(lease_id uuid,status bursar.lease_status,reserved_amount numeric,reserved_calls integer,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_capacity record; v_lease record;
BEGIN
 PERFORM bursar.account_for_subject(p_subject_id);
 INSERT INTO bursar.feature_call_windows(account_id,feature,window_start,window_end,limit_value) VALUES((SELECT id FROM bursar.credit_accounts WHERE subject_id=p_subject_id AND account_kind='personal'),p_feature,p_window_start,p_window_end,p_limit) ON CONFLICT (account_id,feature,window_start) DO UPDATE SET window_end=EXCLUDED.window_end,limit_value=EXCLUDED.limit_value;
 SELECT * INTO v_lease FROM bursar.create_lease(p_subject_id,p_operation,p_estimate,p_idempotency_key,p_feature,p_reserved_calls);
 IF v_lease.error_code IS NOT NULL THEN RETURN QUERY SELECT v_lease.lease_id,v_lease.status,v_lease.reserved_amount,v_lease.reserved_calls,v_lease.error_code; RETURN; END IF;
 UPDATE bursar.credit_leases SET feature_window_start=p_window_start,feature_window_end=p_window_end WHERE id=v_lease.lease_id;
 RETURN QUERY SELECT v_lease.lease_id,v_lease.status,v_lease.reserved_amount,v_lease.reserved_calls,v_lease.error_code;
END $$;
CREATE FUNCTION bursar.effective_subject_policy(p_subject_id uuid, p_operation text, p_feature text DEFAULT NULL)
RETURNS TABLE(
    catalog_revision_id uuid,
    plan_id uuid,
    minimum_balance numeric,
    max_concurrent integer,
    feature_max_calls integer,
    feature_action text
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    WITH assignment AS (
        SELECT a.catalog_revision_id, a.plan_id, p.spending, p.limits
        FROM bursar.credit_accounts ca
        JOIN bursar.account_plan_assignments a ON a.account_id = ca.id
        JOIN bursar.catalog_plans p ON p.id = a.plan_id AND p.catalog_revision_id = a.catalog_revision_id
        WHERE ca.subject_id = p_subject_id AND ca.account_kind = 'personal'
          AND a.starts_at <= now() AND (a.ends_at IS NULL OR a.ends_at > now())
    ), policy AS (
        SELECT *, COALESCE(spending->'operations'->p_operation, '{}'::jsonb) AS operation_policy,
               COALESCE(limits->p_feature, '{}'::jsonb) AS feature_policy
        FROM assignment
    )
    SELECT catalog_revision_id,
           plan_id,
           CASE WHEN COALESCE(operation_policy->>'mode', spending->>'mode', 'strict') = 'overdraft'
                THEN -COALESCE(NULLIF(operation_policy->>'overdraft_limit', '')::numeric,
                               NULLIF(spending->>'overdraft_limit', '')::numeric, 0)
                ELSE 0 END,
           COALESCE(NULLIF(operation_policy->>'max_concurrent', '')::integer,
                    NULLIF(spending->>'max_concurrent', '')::integer),
           NULLIF(feature_policy->>'max_calls', '')::integer,
           COALESCE(feature_policy->>'action', 'deny')
FROM policy
$$;

CREATE FUNCTION bursar.policy_period_window(
    p_anchor_at timestamptz,
    p_unit text,
    p_count integer,
    p_anchor text,
    p_timezone text
)
RETURNS TABLE(window_start timestamptz, window_end timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_step interval;
    v_local timestamp;
    v_month integer;
    v_start_month integer;
BEGIN
    IF p_unit NOT IN ('day','week','month','year') OR p_count IS NULL OR p_count < 1
       OR p_anchor NOT IN ('calendar','plan_assignment','rolling')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = p_timezone) THEN
        RAISE EXCEPTION 'invalid policy period' USING ERRCODE = '22023';
    END IF;
    v_step := CASE p_unit WHEN 'day' THEN make_interval(days => p_count)
                          WHEN 'week' THEN make_interval(weeks => p_count)
                          WHEN 'month' THEN make_interval(months => p_count)
                          WHEN 'year' THEN make_interval(years => p_count) END;
    IF p_anchor = 'calendar' THEN
        v_local := now() AT TIME ZONE p_timezone;
        IF p_unit = 'day' THEN
            window_start := date_bin(make_interval(days => p_count), v_local, timestamp '2000-01-01') AT TIME ZONE p_timezone;
        ELSIF p_unit = 'week' THEN
            window_start := date_bin(make_interval(weeks => p_count), v_local, timestamp '2000-01-03') AT TIME ZONE p_timezone;
        ELSE
            v_month := extract(year FROM v_local)::integer * 12 + extract(month FROM v_local)::integer - 1;
            v_start_month := v_month - mod(v_month - 24000, p_count * CASE WHEN p_unit = 'year' THEN 12 ELSE 1 END);
            window_start := make_date(v_start_month / 12, mod(v_start_month, 12) + 1, 1)::timestamp AT TIME ZONE p_timezone;
        END IF;
    ELSE
        window_start := COALESCE(p_anchor_at, now());
        window_end := (window_start AT TIME ZONE p_timezone + v_step) AT TIME ZONE p_timezone;
        WHILE window_end <= now() LOOP
            window_start := window_end;
            window_end := (window_start AT TIME ZONE p_timezone + v_step) AT TIME ZONE p_timezone;
        END LOOP;
        RETURN NEXT;
        RETURN;
    END IF;
    window_end := (window_start AT TIME ZONE p_timezone + v_step) AT TIME ZONE p_timezone;
    RETURN NEXT;
END $$;

CREATE FUNCTION bursar.charge_usage_for_operation(
    p_subject_id uuid,
    p_operation text,
    p_requested numeric,
    p_idempotency_key text,
    p_feature text DEFAULT NULL,
    p_model text DEFAULT NULL,
    p_region text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(charge_id uuid, ledger_entry_id uuid, charged numeric, allowance_covered numeric, replayed boolean, error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_account uuid;
    v_assignment record;
    v_allowance record;
    v_feature_policy jsonb;
    v_feature_start timestamptz;
    v_feature_end timestamptz;
    v_feature_limit integer;
    v_feature_action text;
    v_allowance_start timestamptz;
    v_allowance_end timestamptz;
    v_free numeric := 0;
 v_result record;
 v_existing record;
 v_has_assignment boolean := false;
BEGIN
    IF p_requested < 0 OR p_idempotency_key IS NULL OR p_idempotency_key = '' THEN
        RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'invalid_request'; RETURN;
    END IF;
    v_account := bursar.account_for_subject(p_subject_id);
    -- Serialising per account makes feature admission and allowance consumption one
    -- financial decision, rather than two independently racy SDK calls.
    PERFORM 1 FROM bursar.credit_accounts WHERE id = v_account FOR UPDATE;
 SELECT c.allowance_requested,c.allowance_covered INTO v_existing
 FROM bursar.credit_usage_charges AS c
 WHERE c.account_id=v_account AND c.idempotency_key=p_idempotency_key;
 IF FOUND THEN
 SELECT * INTO v_result FROM bursar.charge_usage(p_subject_id,p_operation,p_requested,p_idempotency_key,p_feature,p_model,p_region,v_existing.allowance_covered,p_metadata,v_existing.allowance_requested);
        RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;
        RETURN;
    END IF;
    SELECT a.plan_id,a.catalog_revision_id,a.starts_at,p.included_credits,p.included_credits_reset_unit,
           p.included_credits_reset_count,p.included_credits_reset_anchor,p.included_credits_reset_timezone,p.limits
    INTO v_assignment
    FROM bursar.account_plan_assignments a
    JOIN bursar.catalog_plans p ON p.id=a.plan_id AND p.catalog_revision_id=a.catalog_revision_id
 WHERE a.account_id=v_account AND a.starts_at<=now() AND (a.ends_at IS NULL OR a.ends_at>now());
 v_has_assignment := FOUND;

 IF v_has_assignment AND p_feature IS NOT NULL THEN
        v_feature_policy := COALESCE(v_assignment.limits->p_feature, '{}'::jsonb);
        v_feature_limit := NULLIF(v_feature_policy->>'max_calls','')::integer;
        v_feature_action := COALESCE(v_feature_policy->>'action','deny');
        IF v_feature_limit IS NOT NULL THEN
            SELECT window_start,window_end INTO v_feature_start,v_feature_end FROM bursar.policy_period_window(
                v_assignment.starts_at, v_feature_policy #>> '{period,unit}',
                COALESCE((v_feature_policy #>> '{period,count}')::integer,1),
                COALESCE(v_feature_policy #>> '{period,anchor}','calendar'),
                COALESCE(v_feature_policy #>> '{period,timezone}','UTC')
            );
            INSERT INTO bursar.feature_call_windows(account_id,feature,window_start,window_end,limit_value)
            VALUES(v_account,p_feature,v_feature_start,v_feature_end,v_feature_limit)
            ON CONFLICT (account_id,feature,window_start) DO NOTHING;
            IF EXISTS (
                SELECT 1 FROM bursar.feature_call_windows
                WHERE account_id=v_account AND feature=p_feature AND window_start=v_feature_start AND admitted>=v_feature_limit
            ) THEN
                IF v_feature_action='deny' THEN
                    RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'feature_limit_reached'; RETURN;
                END IF;
                INSERT INTO bursar.feature_limit_events(account_id,feature,window_start,action,idempotency_key)
                VALUES(v_account,p_feature,v_feature_start,v_feature_action,p_idempotency_key)
                ON CONFLICT DO NOTHING;
            END IF;
        END IF;
    END IF;

 IF v_has_assignment AND v_assignment.included_credits IS NOT NULL THEN
        SELECT window_start,window_end INTO v_allowance_start,v_allowance_end FROM bursar.policy_period_window(
            v_assignment.starts_at, v_assignment.included_credits_reset_unit,
            v_assignment.included_credits_reset_count, v_assignment.included_credits_reset_anchor,
            v_assignment.included_credits_reset_timezone
        );
        INSERT INTO bursar.allowance_windows(account_id,plan_id,catalog_revision_id,feature,window_start,window_end,period_unit,period_count,period_anchor,period_timezone,allowance)
        VALUES(v_account,v_assignment.plan_id,v_assignment.catalog_revision_id,'__included_credits__',v_allowance_start,v_allowance_end,
               v_assignment.included_credits_reset_unit,v_assignment.included_credits_reset_count,v_assignment.included_credits_reset_anchor,v_assignment.included_credits_reset_timezone,v_assignment.included_credits)
        ON CONFLICT (account_id,plan_id,catalog_revision_id,feature,window_start,window_end) DO NOTHING;
        SELECT LEAST(p_requested,GREATEST(allowance-reserved-consumed,0)) INTO v_free
        FROM bursar.allowance_windows WHERE account_id=v_account AND plan_id=v_assignment.plan_id
          AND feature='__included_credits__' AND window_start=v_allowance_start AND window_end=v_allowance_end FOR UPDATE;
    END IF;

    IF v_free > 0 THEN
        SELECT * INTO v_result FROM bursar.charge_usage_with_window(
            p_subject_id,p_operation,p_requested,p_idempotency_key,'__included_credits__',v_allowance_start,v_allowance_end,v_free,
            p_model,p_region,p_metadata,p_feature
        );
    ELSE
        SELECT * INTO v_result FROM bursar.charge_usage(p_subject_id,p_operation,p_requested,p_idempotency_key,p_feature,p_model,p_region,0,p_metadata,0);
    END IF;
    IF v_result.error_code IS NOT NULL THEN
        RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;
        RETURN;
    END IF;
    IF v_feature_limit IS NOT NULL THEN
        UPDATE bursar.feature_call_windows
        SET consumed=consumed+1, admitted=admitted+1
        WHERE account_id=v_account AND feature=p_feature AND window_start=v_feature_start
          AND admitted < v_feature_limit;
    END IF;
    RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;
END $$;

CREATE FUNCTION bursar.create_lease_for_operation(
    p_subject_id uuid,
    p_operation text,
    p_estimate numeric,
    p_idempotency_key text,
    p_feature text DEFAULT NULL,
    p_reserved_calls integer DEFAULT 1,
    p_ttl interval DEFAULT interval '10 minutes',
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(lease_id uuid,status bursar.lease_status,reserved_amount numeric,reserved_calls integer,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_account uuid;
    v_assignment record;
    v_policy jsonb;
    v_start timestamptz;
    v_end timestamptz;
    v_limit integer;
    v_reserved_calls integer := 0;
    v_result record;
BEGIN
    IF p_feature IS NOT NULL AND p_reserved_calls < 1 THEN
        RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'invalid_request'; RETURN;
    END IF;
    v_account := bursar.account_for_subject(p_subject_id);
    IF p_feature IS NOT NULL THEN
        SELECT a.starts_at,p.limits INTO v_assignment
        FROM bursar.account_plan_assignments a
        JOIN bursar.catalog_plans p ON p.id=a.plan_id AND p.catalog_revision_id=a.catalog_revision_id
        WHERE a.account_id=v_account AND a.starts_at<=now() AND (a.ends_at IS NULL OR a.ends_at>now());
        IF FOUND THEN
            v_policy := COALESCE(v_assignment.limits->p_feature, '{}'::jsonb);
            v_limit := NULLIF(v_policy->>'max_calls','')::integer;
            IF v_limit IS NOT NULL THEN
                v_reserved_calls := COALESCE(p_reserved_calls,1);
                SELECT window_start,window_end INTO v_start,v_end FROM bursar.policy_period_window(
                    v_assignment.starts_at, v_policy #>> '{period,unit}',
                    COALESCE((v_policy #>> '{period,count}')::integer,1),
                    COALESCE(v_policy #>> '{period,anchor}','calendar'),
                    COALESCE(v_policy #>> '{period,timezone}','UTC')
                );
                INSERT INTO bursar.feature_call_windows(account_id,feature,window_start,window_end,limit_value)
                VALUES(v_account,p_feature,v_start,v_end,v_limit)
                ON CONFLICT (account_id,feature,window_start) DO NOTHING;
            END IF;
        END IF;
    END IF;
    SELECT * INTO v_result FROM bursar.create_lease(
        p_subject_id,p_operation,p_estimate,p_idempotency_key,p_feature,
        v_reserved_calls,NULL,p_ttl,'{}'::jsonb,p_metadata
    );
    RETURN QUERY SELECT v_result.lease_id,v_result.status,v_result.reserved_amount,v_result.reserved_calls,v_result.error_code;
END $$;
