-- Make team creation safe to retry after an indeterminate client response.
--
-- One caller-owned idempotency key identifies one creation request inside a
-- tenant. The request digest binds that key to the owner, normalized name, and
-- initial balance so a changed replay cannot create another team or grant.

ALTER TABLE bursar.credit_teams
ADD COLUMN creation_idempotency_key text;

ALTER TABLE bursar.credit_teams
ADD COLUMN creation_request_digest bytea;

-- Forced RLS deliberately excludes the migration role. Temporarily disable it
-- while the transactional migration backfills legacy rows, then restore the
-- exact fail-closed posture installed by migration 029.
ALTER TABLE bursar.credit_teams DISABLE ROW LEVEL SECURITY;

UPDATE bursar.credit_teams
SET creation_idempotency_key = 'legacy:' || id::text,
    creation_request_digest = extensions.digest(
        convert_to(
            jsonb_build_object('legacy_team_id', id::text)::text,
            'UTF8'
        ),
        'sha256'
    );

ALTER TABLE bursar.credit_teams ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.credit_teams FORCE ROW LEVEL SECURITY;

ALTER TABLE bursar.credit_teams
ALTER COLUMN creation_idempotency_key SET NOT NULL;

ALTER TABLE bursar.credit_teams
ALTER COLUMN creation_request_digest SET NOT NULL;

ALTER TABLE bursar.credit_teams
ADD CONSTRAINT credit_teams_creation_idempotency_key_check
CHECK (bursar.is_nonempty_bounded_text(creation_idempotency_key, 255));

ALTER TABLE bursar.credit_teams
ADD CONSTRAINT credit_teams_creation_request_digest_check
CHECK (octet_length(creation_request_digest) = 32);

ALTER TABLE bursar.credit_teams
ADD CONSTRAINT credit_teams_creation_idempotency_key_unique
UNIQUE (tenant_id, creation_idempotency_key);

-- Migration 029 transferred the old RPC to the non-BYPASSRLS runtime owner.
-- Assume that role only while dropping the owned function; the migration role
-- creates the replacement and transfers ownership back below.
SET LOCAL ROLE bursar_runtime;

DROP FUNCTION bursar.create_team(uuid, text, numeric);

RESET ROLE;

CREATE FUNCTION bursar.create_team(
    p_owner_subject_id uuid,
    p_name text,
    p_idempotency_key text,
    p_initial_credits numeric DEFAULT 0
)
RETURNS TABLE (
    team_id uuid,
    name text,
    team_subject_id uuid,
    account_id uuid,
    idempotent boolean,
    error_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_team uuid;
    v_candidate_team_subject uuid := bursar.uuid_v7();
    v_team_subject uuid;
    v_account uuid;
    v_name text;
    v_request_digest bytea;
    v_stored_digest bytea;
    v_post record;
BEGIN
    IF p_owner_subject_id IS NULL
       OR NOT bursar.is_nonempty_bounded_text(
           trim(COALESCE(p_name, '')),
           200
       )
       OR NOT bursar.is_nonempty_bounded_text(p_idempotency_key, 255)
       OR NOT bursar.is_finite_numeric(p_initial_credits)
       OR p_initial_credits < 0
    THEN
        RETURN QUERY SELECT
            NULL::uuid,
            NULL::text,
            NULL::uuid,
            NULL::uuid,
            false,
            'invalid_request'::text;
        RETURN;
    END IF;

    v_name := trim(p_name);
    v_request_digest := extensions.digest(
        convert_to(
            jsonb_build_object(
                'owner_subject_id', p_owner_subject_id::text,
                'name', v_name,
                'initial_credits',
                    bursar.digest_numeric_text(p_initial_credits)
            )::text,
            'UTF8'
        ),
        'sha256'
    );

    -- Reject a known pseudonymized owner before claiming the operation key.
    -- A missing subject is created only after this request wins the upsert, so
    -- a changed-owner conflict cannot leave an orphan subject behind.
    IF bursar.is_subject_pseudonymized(p_owner_subject_id) THEN
        RETURN QUERY SELECT
            NULL::uuid,
            NULL::text,
            NULL::uuid,
            NULL::uuid,
            false,
            'subject_pseudonymized'::text;
        RETURN;
    END IF;

    -- The candidate subject satisfies the team foreign key for a possible
    -- insert. Losing replays delete their unreferenced candidate before
    -- returning the already-created logical team.
    INSERT INTO bursar.subjects(id)
    VALUES (v_candidate_team_subject);

    INSERT INTO bursar.credit_teams AS stored_team(
        subject_id,
        name,
        creation_idempotency_key,
        creation_request_digest
    )
    VALUES (
        v_candidate_team_subject,
        v_name,
        p_idempotency_key,
        v_request_digest
    )
    ON CONFLICT (tenant_id, creation_idempotency_key)
    DO UPDATE SET
        -- A no-op update makes PostgreSQL return the winner after waiting for
        -- a concurrent insert, without rewriting its immutable request.
        creation_idempotency_key = stored_team.creation_idempotency_key
    RETURNING
        stored_team.id,
        stored_team.name,
        stored_team.subject_id,
        stored_team.creation_request_digest
    INTO
        v_team,
        name,
        v_team_subject,
        v_stored_digest;

    IF v_team_subject <> v_candidate_team_subject THEN
        DELETE FROM bursar.subjects
        WHERE id = v_candidate_team_subject;

        IF v_stored_digest IS DISTINCT FROM v_request_digest THEN
            RETURN QUERY SELECT
                NULL::uuid,
                NULL::text,
                NULL::uuid,
                NULL::uuid,
                false,
                'idempotency_conflict'::text;
            RETURN;
        END IF;

        SELECT credit_account.id
        INTO v_account
        FROM bursar.credit_accounts AS credit_account
        WHERE credit_account.subject_id = v_team_subject
          AND credit_account.account_kind = 'team';

        IF NOT FOUND THEN
            RAISE EXCEPTION 'idempotent team creation is missing its account'
                USING ERRCODE = '55000';
        END IF;

        RETURN QUERY SELECT
            v_team,
            name,
            v_team_subject,
            v_account,
            true,
            NULL::text;
        RETURN;
    END IF;

    INSERT INTO bursar.subjects(id)
    VALUES (p_owner_subject_id)
    ON CONFLICT (tenant_id, id) DO NOTHING;

    -- Close the race with concurrent pseudonymization after the preflight
    -- check. No children exist yet, so the winning request can cleanly release
    -- its claimed team and candidate subject before returning.
    IF bursar.is_subject_pseudonymized(p_owner_subject_id) THEN
        DELETE FROM bursar.credit_teams
        WHERE id = v_team;

        DELETE FROM bursar.subjects
        WHERE id = v_candidate_team_subject;

        RETURN QUERY SELECT
            NULL::uuid,
            NULL::text,
            NULL::uuid,
            NULL::uuid,
            false,
            'subject_pseudonymized'::text;
        RETURN;
    END IF;

    INSERT INTO bursar.credit_team_members(team_id, subject_id, role)
    VALUES (v_team, p_owner_subject_id, 'owner');

    INSERT INTO bursar.credit_accounts(subject_id, account_kind)
    VALUES (v_team_subject, 'team')
    RETURNING id INTO v_account;

    IF p_initial_credits > 0 THEN
        SELECT *
        INTO v_post
        FROM bursar.post_credit_account(
            v_account,
            'grant',
            p_initial_credits,
            'team_initial_grant',
            'team-initial:' || v_team::text
        );

        IF v_post.error_code IS NOT NULL THEN
            RAISE EXCEPTION 'team initial grant failed: %', v_post.error_code;
        END IF;
    END IF;

    RETURN QUERY SELECT
        v_team,
        name,
        v_team_subject,
        v_account,
        false,
        NULL::text;
END
$$;

REVOKE ALL
ON FUNCTION bursar.create_team(uuid, text, text, numeric)
FROM PUBLIC;

-- The new owner needs CREATE on the containing schema for the ownership
-- transfer. Revoke it again before committing the migration.
GRANT CREATE ON SCHEMA bursar TO bursar_runtime;

ALTER FUNCTION bursar.create_team(uuid, text, text, numeric)
OWNER TO bursar_runtime;

SET LOCAL ROLE bursar_runtime;

GRANT EXECUTE
ON FUNCTION bursar.create_team(uuid, text, text, numeric)
TO bursar_client;

COMMENT ON FUNCTION bursar.create_team(uuid, text, text, numeric)
IS 'Creates or returns one team bound to a tenant-scoped idempotency key and immutable request digest.';

RESET ROLE;

REVOKE CREATE ON SCHEMA bursar FROM bursar_runtime;
