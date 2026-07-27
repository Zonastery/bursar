-- Relational constraints and invariant checks.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
BEGIN
 NEW.updated_at:=now();

 RETURN NEW;

END $$;

CREATE FUNCTION bursar.require_valid_timezone() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
DECLARE
 v_timezone text:=to_jsonb(NEW)->>TG_ARGV[0];

BEGIN
 IF v_timezone IS NOT NULL
 AND NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_timezone_names
  WHERE name=v_timezone
 ) THEN
  RAISE EXCEPTION 'invalid timezone: %',v_timezone
  USING ERRCODE='22023';

 END IF;

 RETURN NEW;

END $$;

CREATE FUNCTION bursar.rearm_auto_recharge_profile() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
BEGIN
    IF NEW.account_kind='personal' AND NEW.balance>OLD.balance THEN
        UPDATE bursar.billing_auto_recharge_profiles
        SET armed=true
        WHERE subject_id=NEW.subject_id
          AND enabled
          AND NEW.balance>=threshold;

    END IF;

    RETURN NEW;

END $$;

CREATE FUNCTION bursar.require_internal_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
BEGIN
    IF current_setting('bursar.mutation_context',true) IS DISTINCT FROM 'internal' THEN
        RAISE EXCEPTION 'direct mutation not allowed' USING ERRCODE='42501';

    END IF;

    IF TG_OP='DELETE' THEN RETURN OLD;
 END IF;

    RETURN NEW;

END $$;

CREATE FUNCTION bursar.check_ledger_balance() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
DECLARE v_balance numeric;

BEGIN
    SELECT balance INTO v_balance
    FROM bursar.credit_accounts
    WHERE id=NEW.account_id
    FOR UPDATE;

    IF NOT FOUND OR NEW.balance_after<>v_balance+NEW.amount THEN
        RAISE EXCEPTION 'ledger balance invariant violated' USING ERRCODE='23514';

    END IF;

    RETURN NEW;

END $$;

CREATE FUNCTION bursar.check_lot_allocation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
DECLARE
    v_debit numeric;

    v_account uuid;

    v_lot_account uuid;

    v_total numeric;

BEGIN
    SELECT amount,account_id INTO v_debit,v_account
    FROM bursar.credit_ledger_entries
    WHERE id=NEW.debit_entry_id
    FOR UPDATE;

    SELECT account_id INTO v_lot_account
    FROM bursar.credit_lots
    WHERE id=NEW.lot_id
    FOR UPDATE;

    IF v_debit IS NULL OR v_debit>=0 OR v_account<>v_lot_account THEN
        RAISE EXCEPTION 'invalid lot allocation' USING ERRCODE='23514';

    END IF;

    SELECT COALESCE(SUM(amount),0) INTO v_total
    FROM bursar.credit_lot_allocations
    WHERE debit_entry_id=NEW.debit_entry_id;

    IF v_total+NEW.amount>-v_debit THEN
        RAISE EXCEPTION 'lot allocations exceed debit' USING ERRCODE='23514';

    END IF;

    RETURN NEW;

END $$;

CREATE FUNCTION bursar.reject_catalog_projection_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
BEGIN
    RAISE EXCEPTION 'projected catalog rows are immutable' USING ERRCODE='55000';

END $$;

CREATE FUNCTION bursar.validate_catalog_provider_ref() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
BEGIN
    IF (NEW.object_type='offer' AND EXISTS (
            SELECT 1 FROM bursar.catalog_offers
            WHERE catalog_revision_id=NEW.catalog_revision_id AND offer_key=NEW.object_key
        ))
       OR (NEW.object_type='topup' AND EXISTS (
            SELECT 1 FROM bursar.catalog_topups
            WHERE catalog_revision_id=NEW.catalog_revision_id AND topup_key=NEW.object_key
        ))
       OR (NEW.object_type='plan' AND EXISTS (
            SELECT 1 FROM bursar.catalog_plans
            WHERE catalog_revision_id=NEW.catalog_revision_id AND plan_key=NEW.object_key
        ))
    THEN
        RETURN NEW;

    END IF;

    RAISE EXCEPTION 'catalog provider reference target missing' USING ERRCODE='23503';

END $$;

CREATE FUNCTION bursar.one_active_catalog_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
BEGIN
    IF OLD.status<>'draft'
       AND (
           NEW.yaml_schema_version<>OLD.yaml_schema_version
           OR NEW.source_document<>OLD.source_document
           OR NEW.digest<>OLD.digest
           OR NEW.label IS DISTINCT FROM OLD.label
       )
    THEN
        RAISE EXCEPTION 'catalog revision content is immutable' USING ERRCODE='55000';

    END IF;

    IF OLD.status<>'draft' AND NEW.status='draft' THEN
        RAISE EXCEPTION 'catalog revision cannot return to draft' USING ERRCODE='55000';

    END IF;

    IF NEW.status='active' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('bursar.catalog.active',0));

        UPDATE bursar.catalog_revisions
        SET status='retired',retired_at=now()
        WHERE status='active' AND id<>NEW.id;

        IF OLD.status<>'active' THEN NEW.activated_at:=now();
 END IF;

        NEW.retired_at:=NULL;

    END IF;

    IF NEW.status='published' THEN
        NEW.published_at:=COALESCE(NEW.published_at,now());

    END IF;

    IF NEW.status='retired' AND OLD.status<>'retired' THEN
        NEW.retired_at:=now();

    END IF;

    RETURN NEW;

END $$;

CREATE FUNCTION bursar.reject_revision_delete() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
BEGIN
    IF OLD.status<>'draft' THEN
        RAISE EXCEPTION 'catalog revisions are immutable' USING ERRCODE='55000';

    END IF;

    RETURN OLD;

END $$;

CREATE FUNCTION bursar.check_refund_bounds() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '' AS $$
DECLARE
    v_payment uuid;

    v_grant_payment uuid;

    v_original bigint;

    v_refund_amount bigint;

    v_refunded bigint;

    v_payment_provider text;

    v_payment_currency char(3);

    v_grant_credits numeric;

    v_total_credits numeric;

    v_clawed_back numeric;

BEGIN
    IF TG_TABLE_NAME='billing_refunds' THEN
        SELECT amount_minor,provider,currency
        INTO v_original,v_payment_provider,v_payment_currency
        FROM bursar.billing_payments
        WHERE id=NEW.payment_id
        FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'refund payment missing' USING ERRCODE='23503';

        END IF;

        IF NEW.provider<>v_payment_provider OR NEW.currency<>v_payment_currency THEN
            RAISE EXCEPTION 'refund payment mismatch' USING ERRCODE='23514';

        END IF;

        SELECT COALESCE(SUM(amount_minor),0) INTO v_refunded
        FROM bursar.billing_refunds
        WHERE payment_id=NEW.payment_id AND id<>NEW.id;

        IF v_refunded+NEW.amount_minor>v_original THEN
            RAISE EXCEPTION 'refund exceeds payment' USING ERRCODE='23514';

        END IF;

    ELSE
        SELECT payment_id,amount_minor
        INTO v_payment,v_refund_amount
        FROM bursar.billing_refunds
        WHERE id=NEW.refund_id
        FOR UPDATE;

        IF NOT FOUND THEN RAISE EXCEPTION 'refund missing' USING ERRCODE='23503';
 END IF;

        SELECT payment_id,configured_credits*quantity
        INTO v_grant_payment,v_grant_credits
        FROM bursar.billing_credit_grants
        WHERE id=NEW.grant_id
        FOR UPDATE;

        IF NOT FOUND OR v_grant_payment IS DISTINCT FROM v_payment THEN
            RAISE EXCEPTION 'refund grant payment mismatch' USING ERRCODE='23514';

        END IF;

        SELECT amount_minor INTO v_original
        FROM bursar.billing_payments
        WHERE id=v_payment;

        SELECT SUM(configured_credits*quantity) INTO v_total_credits
        FROM bursar.billing_credit_grants
        WHERE payment_id=v_payment;

        IF v_original<=0 OR v_total_credits IS NULL OR v_total_credits<=0 THEN
            RAISE EXCEPTION 'refund credit rate unavailable' USING ERRCODE='23514';

        END IF;

        SELECT COALESCE(SUM(amount_minor),0) INTO v_refunded
        FROM bursar.billing_refund_grants
        WHERE refund_id=NEW.refund_id AND grant_id<>NEW.grant_id;

        IF v_refunded+NEW.amount_minor>v_refund_amount THEN
            RAISE EXCEPTION 'refund grant exceeds refund' USING ERRCODE='23514';

        END IF;

        NEW.credit_amount:=round(NEW.amount_minor::numeric*v_total_credits/v_original,6);

        IF NEW.credit_amount<=0 THEN
            RAISE EXCEPTION 'refund credit amount rounds to zero' USING ERRCODE='23514';

        END IF;

        SELECT COALESCE(SUM(credit_amount),0) INTO v_clawed_back
        FROM bursar.billing_refund_grants
        WHERE grant_id=NEW.grant_id
          AND NOT (refund_id=NEW.refund_id AND grant_id=NEW.grant_id);

        IF v_clawed_back+NEW.credit_amount>v_grant_credits THEN
            RAISE EXCEPTION 'refund grant exceeds original grant' USING ERRCODE='23514';

        END IF;

    END IF;

    RETURN NEW;

END $$;
