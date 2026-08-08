-- Team membership and balance RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.create_team(
    p_owner_subject_id uuid,
    p_name text,
    p_initial_credits numeric DEFAULT 0
)
RETURNS TABLE (
    team_id uuid,
    name text,
    team_subject_id uuid,
    account_id uuid,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_team uuid;

    v_team_subject uuid:=bursar.uuid_v7();

    v_account uuid;

    v_post record;

BEGIN
    IF p_owner_subject_id IS NULL OR length(trim(COALESCE(p_name,'')))=0
       OR p_initial_credits<0
    THEN
        RETURN QUERY SELECT NULL::uuid,NULL::text,NULL::uuid,NULL::uuid,'invalid_request';

        RETURN;

    END IF;

    INSERT INTO bursar.subjects(id)
    VALUES (p_owner_subject_id),(v_team_subject)
    ON CONFLICT (tenant_id, id) DO NOTHING;

    IF bursar.is_subject_pseudonymized(p_owner_subject_id) THEN
        RETURN QUERY SELECT NULL::uuid,NULL::text,NULL::uuid,NULL::uuid,'subject_pseudonymized';
        RETURN;
    END IF;

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

    RETURN QUERY SELECT v_team,trim(p_name),v_team_subject,v_account,NULL::text;

END $$;

CREATE FUNCTION bursar.set_team_member(
    p_team_id uuid,
    p_subject_id uuid,
    p_role text,
    p_spend_cap numeric DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN
    IF p_role NOT IN ('owner','admin','member')
       OR (
           p_spend_cap IS NOT NULL
           AND (
               NOT bursar.is_finite_numeric(p_spend_cap)
               OR p_spend_cap < 0
           )
       )
       OR NOT EXISTS (SELECT 1 FROM bursar.credit_teams WHERE id=p_team_id)
    THEN
        RETURN false;

    END IF;

    INSERT INTO bursar.subjects(id)
    VALUES (p_subject_id)
    ON CONFLICT (tenant_id, id) DO NOTHING;

    IF bursar.is_subject_pseudonymized(p_subject_id) THEN
        RETURN false;
    END IF;

    INSERT INTO bursar.credit_team_members(
        team_id,
        subject_id,
        role,
        spend_cap
    )
    VALUES (p_team_id,p_subject_id,p_role,p_spend_cap)
    ON CONFLICT (team_id,subject_id) DO UPDATE
    SET role=EXCLUDED.role,
        spend_cap=EXCLUDED.spend_cap;

    RETURN true;

END $$;

CREATE FUNCTION bursar.remove_team_member(
    p_team_id uuid,
    p_subject_id uuid
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_role text;

BEGIN
    SELECT role INTO v_role
    FROM bursar.credit_team_members
    WHERE team_id=p_team_id AND subject_id=p_subject_id
    FOR UPDATE;

    IF NOT FOUND THEN RETURN false;
 END IF;

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

CREATE FUNCTION bursar.list_team_members(
    p_team_id uuid
)
RETURNS TABLE (
    user_id uuid,
    role text,
    spend_cap numeric,
    total_spent numeric
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        member.subject_id,
        member.role,
        member.spend_cap,
        COALESCE(sum(team_usage.amount), 0)
    FROM bursar.credit_team_members AS member
    LEFT JOIN bursar.credit_team_usage_charges AS team_usage
      ON team_usage.team_id = member.team_id
     AND team_usage.subject_id = member.subject_id
    WHERE member.team_id=p_team_id
    GROUP BY
        member.subject_id,
        member.role,
        member.spend_cap,
        member.created_at
    ORDER BY member.created_at,member.subject_id
$$;

CREATE FUNCTION bursar.get_team_balance(
    p_team_id uuid
)
RETURNS TABLE (
    team_id uuid,
    name text,
    balance numeric,
    member_count bigint
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT
        team.id,
        team.name,
        account.balance,
        count(member.subject_id)
    FROM bursar.credit_teams AS team
    JOIN bursar.credit_accounts AS account
      ON account.subject_id=team.subject_id
     AND account.account_kind='team'
    LEFT JOIN bursar.credit_team_members AS member
      ON member.team_id = team.id
    WHERE team.id=p_team_id
    GROUP BY team.id, team.name, account.balance
$$;
