CREATE FUNCTION bursar.create_team(
    p_owner_subject_id uuid,p_name text,p_initial_credits numeric DEFAULT 0
)
RETURNS TABLE(team_id uuid,team_subject_id uuid,account_id uuid,error_code text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_team uuid;
    v_team_subject uuid:=gen_random_uuid();
    v_account uuid;
    v_post record;
BEGIN
    IF p_owner_subject_id IS NULL OR length(trim(COALESCE(p_name,'')))=0
       OR p_initial_credits<0
    THEN
        RETURN QUERY SELECT NULL::uuid,NULL::uuid,NULL::uuid,'invalid_request';
        RETURN;
    END IF;
    INSERT INTO bursar.subjects(id)
    VALUES (p_owner_subject_id),(v_team_subject)
    ON CONFLICT DO NOTHING;
    INSERT INTO bursar.credit_teams(subject_id,name)
    VALUES (v_team_subject,trim(p_name))
    RETURNING id INTO v_team;
    INSERT INTO bursar.credit_team_members(team_id,subject_id,role)
    VALUES (v_team,p_owner_subject_id,'owner');
    INSERT INTO bursar.credit_accounts(subject_id,account_kind)
    VALUES (v_team_subject,'team')
    RETURNING id INTO v_account;
    IF p_initial_credits>0 THEN
        SELECT * INTO v_post
        FROM bursar.post_credit_account(
            v_account,'grant',p_initial_credits,'team_initial_grant',
            'team-initial:'||v_team::text
        );
        IF v_post.error_code IS NOT NULL THEN
            RAISE EXCEPTION 'team initial grant failed: %',v_post.error_code;
        END IF;
    END IF;
    RETURN QUERY SELECT v_team,v_team_subject,v_account,NULL::text;
END $$;

CREATE FUNCTION bursar.set_team_member(
    p_team_id uuid,p_subject_id uuid,p_role text
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
    IF p_role NOT IN ('owner','admin','member')
       OR NOT EXISTS (SELECT 1 FROM bursar.credit_teams WHERE id=p_team_id)
    THEN
        RETURN false;
    END IF;
    INSERT INTO bursar.subjects(id) VALUES (p_subject_id) ON CONFLICT DO NOTHING;
    INSERT INTO bursar.credit_team_members(team_id,subject_id,role)
    VALUES (p_team_id,p_subject_id,p_role)
    ON CONFLICT (team_id,subject_id) DO UPDATE SET role=EXCLUDED.role;
    RETURN true;
END $$;

CREATE FUNCTION bursar.remove_team_member(
    p_team_id uuid,p_subject_id uuid
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_role text;
BEGIN
    SELECT role INTO v_role
    FROM bursar.credit_team_members
    WHERE team_id=p_team_id AND subject_id=p_subject_id
    FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF v_role='owner' AND (
        SELECT count(*) FROM bursar.credit_team_members
        WHERE team_id=p_team_id AND role='owner'
    )<=1 THEN
        RETURN false;
    END IF;
    DELETE FROM bursar.credit_team_members
    WHERE team_id=p_team_id AND subject_id=p_subject_id;
    RETURN true;
END $$;

CREATE FUNCTION bursar.list_team_members(p_team_id uuid)
RETURNS SETOF bursar.credit_team_members
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT * FROM bursar.credit_team_members
    WHERE team_id=p_team_id
    ORDER BY created_at,subject_id
$$;

CREATE FUNCTION bursar.get_team_balance(p_team_id uuid)
RETURNS numeric
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO '' AS $$
    SELECT a.balance
    FROM bursar.credit_teams t
    JOIN bursar.credit_accounts a
      ON a.subject_id=t.subject_id AND a.account_kind='team'
    WHERE t.id=p_team_id
$$;
