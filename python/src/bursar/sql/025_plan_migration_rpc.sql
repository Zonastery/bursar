-- Plan assignment and migration RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.assign_plan(
    p_subject_id uuid,
    p_plan_id uuid,
    p_starts_at timestamptz DEFAULT now(),
    p_ends_at timestamptz DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_account uuid;

    v_revision uuid;

BEGIN
    IF p_starts_at IS NULL OR (p_ends_at IS NOT NULL AND p_ends_at<=p_starts_at) THEN
        RETURN false;

    END IF;

    SELECT catalog_revision_id INTO v_revision
    FROM bursar.catalog_plans
    WHERE id=p_plan_id;

    IF NOT FOUND THEN RETURN false;
 END IF;

    v_account:=bursar.account_for_subject(p_subject_id);

    INSERT INTO bursar.account_plan_assignments(
        account_id,plan_id,catalog_revision_id,starts_at,ends_at
    )
    VALUES (v_account,p_plan_id,v_revision,p_starts_at,p_ends_at)
    ON CONFLICT (account_id) DO UPDATE
    SET plan_id=EXCLUDED.plan_id,
        catalog_revision_id=EXCLUDED.catalog_revision_id,
        starts_at=EXCLUDED.starts_at,
        ends_at=EXCLUDED.ends_at;

    RETURN true;

END $$;

CREATE FUNCTION bursar.start_plan_migration(
    p_from_plan_id uuid,
    p_to_plan_id uuid
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_id uuid;

BEGIN
    IF p_to_plan_id IS NULL OR p_from_plan_id IS NOT DISTINCT FROM p_to_plan_id
       OR NOT EXISTS (SELECT 1 FROM bursar.catalog_plans WHERE id=p_to_plan_id)
       OR (p_from_plan_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM bursar.catalog_plans WHERE id=p_from_plan_id
       ))
    THEN
        RETURN NULL;

    END IF;

    INSERT INTO bursar.credit_plan_migrations(from_plan_id,to_plan_id)
    VALUES (p_from_plan_id,p_to_plan_id)
    RETURNING id INTO v_id;

    RETURN v_id;

END $$;

CREATE FUNCTION bursar.migrate_plan_batch(
    p_migration_id uuid,
    p_batch_size integer DEFAULT 100
)
RETURNS TABLE(
    migrated integer,
    done boolean,
    next_cursor uuid
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_migration bursar.credit_plan_migrations;

    v_count integer:=0;

    r record;

BEGIN
    IF p_batch_size IS NULL OR p_batch_size<1 OR p_batch_size>1000 THEN
        RAISE EXCEPTION 'invalid batch size' USING ERRCODE='22023';

    END IF;

    SELECT * INTO v_migration
    FROM bursar.credit_plan_migrations
    WHERE id=p_migration_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 0,false,NULL::uuid;

        RETURN;

    END IF;

    IF v_migration.status<>'running' THEN
        RETURN QUERY
        SELECT 0,v_migration.status='completed',v_migration.cursor_account_id;

        RETURN;

    END IF;

    FOR r IN
        SELECT a.account_id
        FROM bursar.account_plan_assignments a
        WHERE (v_migration.from_plan_id IS NULL OR a.plan_id=v_migration.from_plan_id)
          AND (
              v_migration.cursor_account_id IS NULL
              OR a.account_id>v_migration.cursor_account_id
          )
        ORDER BY a.account_id
        LIMIT p_batch_size
        FOR UPDATE
    LOOP
        UPDATE bursar.account_plan_assignments
        SET plan_id=v_migration.to_plan_id,
            catalog_revision_id=(
                SELECT catalog_revision_id
                FROM bursar.catalog_plans
                WHERE id=v_migration.to_plan_id
            )
        WHERE account_id=r.account_id;

        v_count:=v_count+1;

        v_migration.cursor_account_id:=r.account_id;

    END LOOP;

    UPDATE bursar.credit_plan_migrations
    SET cursor_account_id=v_migration.cursor_account_id,
        migrated_count=migrated_count+v_count,
        status=CASE WHEN v_count<p_batch_size THEN 'completed' ELSE status END
    WHERE id=p_migration_id;

    RETURN QUERY
    SELECT v_count,v_count<p_batch_size,v_migration.cursor_account_id;

END $$;

CREATE FUNCTION bursar.open_subscription_change(
    p_subscription_id uuid,
    p_to_offer_id uuid,
    p_effective_at timestamptz,
    p_idempotency_key text
)
RETURNS TABLE(
    change_id uuid,
    state text,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    s bursar.billing_subscriptions;

    c bursar.billing_subscription_changes;

BEGIN
    IF p_idempotency_key IS NULL OR p_idempotency_key='' THEN
        RETURN QUERY SELECT NULL::uuid,NULL::text,'invalid_request';

        RETURN;

    END IF;

    SELECT * INTO s
    FROM bursar.billing_subscriptions
    WHERE id=p_subscription_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::uuid,NULL::text,'missing_subscription';

        RETURN;

    END IF;

    IF s.offer_id=p_to_offer_id
       OR NOT EXISTS (SELECT 1 FROM bursar.catalog_offers WHERE id=p_to_offer_id)
    THEN
        RETURN QUERY SELECT NULL::uuid,NULL::text,'invalid_target_offer';

        RETURN;

    END IF;

    SELECT * INTO c
    FROM bursar.billing_subscription_changes
    WHERE subscription_id=p_subscription_id
      AND idempotency_key=p_idempotency_key
    FOR UPDATE;

    IF FOUND THEN
        IF c.from_offer_id<>s.offer_id
           OR c.to_offer_id<>p_to_offer_id
           OR c.effective_at IS DISTINCT FROM p_effective_at
        THEN
            RETURN QUERY SELECT NULL::uuid,NULL::text,'idempotency_conflict';

        ELSE
            RETURN QUERY SELECT c.id,c.state,NULL::text;

        END IF;

        RETURN;

    END IF;

    IF EXISTS (
        SELECT 1
        FROM bursar.billing_subscription_changes
        WHERE subscription_id=p_subscription_id
          AND state IN ('awaiting_payment','scheduled')
    ) THEN
        RETURN QUERY SELECT NULL::uuid,NULL::text,'open_change_exists';

        RETURN;

    END IF;

    INSERT INTO bursar.billing_subscription_changes(
        subscription_id,state,from_offer_id,to_offer_id,effective_at,idempotency_key
    )
    VALUES (
        p_subscription_id,'scheduled',s.offer_id,p_to_offer_id,
        p_effective_at,p_idempotency_key
    )
    RETURNING * INTO c;

    RETURN QUERY SELECT c.id,c.state,NULL::text;

END $$;

CREATE FUNCTION bursar.advance_subscription_change(
    p_change_id uuid,
    p_state text
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE c bursar.billing_subscription_changes;

BEGIN
    IF p_state NOT IN ('awaiting_payment','scheduled','applied','canceled','failed') THEN
        RETURN false;

    END IF;

    SELECT * INTO c
    FROM bursar.billing_subscription_changes
    WHERE id=p_change_id
    FOR UPDATE;

    IF NOT FOUND THEN RETURN false;
 END IF;

    IF c.state=p_state THEN RETURN true;
 END IF;

    IF (c.state,p_state) NOT IN (
        ('awaiting_payment','scheduled'),
        ('awaiting_payment','canceled'),
        ('awaiting_payment','failed'),
        ('scheduled','applied'),
        ('scheduled','canceled'),
        ('scheduled','failed')
    ) THEN
        RETURN false;

    END IF;

    IF p_state='applied' THEN
        UPDATE bursar.billing_subscriptions
        SET offer_id=c.to_offer_id,
            catalog_revision_id=(
                SELECT catalog_revision_id
                FROM bursar.catalog_offers
                WHERE id=c.to_offer_id
            )
        WHERE id=c.subscription_id AND offer_id=c.from_offer_id;

        IF NOT FOUND THEN RETURN false;
 END IF;

    END IF;

    UPDATE bursar.billing_subscription_changes
    SET state=p_state
    WHERE id=p_change_id;

    RETURN true;

END $$;
