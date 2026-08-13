-- Migration: 001_schema_and_types.sql
-- Purpose: Establish Bursar's isolated schemas, extension prerequisites, shared
--   validation helpers, tenant context, and provider-environment contract.
-- Depends on: PostgreSQL with pgcrypto, pg_jsonschema, and pg_partman 5.x available.
-- Security: Keeps dependencies schema-qualified and tenant/provider context fail-closed.

-- Contents
--   1. Schemas and extension prerequisites
--   2. Shared scalar and identity helpers
--   3. Generated catalog contract and validation
--   4. Bounded-value and backend helpers
--   5. Tenant context and provider environment

-- 1. Schemas and extension prerequisites

-- Isolate Bursar-owned objects from host application objects and reserve a
-- separate namespace for dependency calls whose ACLs remain host-controlled.
CREATE SCHEMA IF NOT EXISTS bursar;

-- The migration runner bootstraps this namespace and its ledger immediately
-- before loading 001. Refuse a pre-existing namespace takeover or unexpected
-- object instead of creating SECURITY DEFINER functions beside attacker-owned
-- relations. The ledger's complete column/key shape is asserted below.
DO $$
DECLARE
    v_unexpected_objects text[];
BEGIN
    IF (SELECT nspowner FROM pg_namespace WHERE nspname = 'bursar')
       IS DISTINCT FROM current_user::regrole::oid
    THEN
        RAISE EXCEPTION
            'bursar schema must be owned by migration role %', current_user
            USING ERRCODE = '42501';
    END IF;

    -- Only the runner-created ledger, its index, and PostgreSQL-generated row
    -- types may exist before Bursar begins creating trusted objects.
    SELECT array_agg(unexpected.object_name ORDER BY unexpected.object_name)
    INTO v_unexpected_objects
    FROM (
        SELECT format('relation:%I', relation_info.relname) AS object_name
        FROM pg_class AS relation_info
        WHERE relation_info.relnamespace = 'bursar'::regnamespace
          AND NOT (
              relation_info.relname = 'schema_migrations'
              AND relation_info.relkind = 'r'
              AND relation_info.relowner = current_user::regrole::oid
          )
          AND NOT (
              relation_info.relname = 'schema_migrations_pkey'
              AND relation_info.relkind = 'i'
              AND relation_info.relowner = current_user::regrole::oid
          )

        UNION ALL

        SELECT format('routine:%I', routine_info.proname)
        FROM pg_proc AS routine_info
        WHERE routine_info.pronamespace = 'bursar'::regnamespace

        UNION ALL

        SELECT format('type:%I', type_info.typname)
        FROM pg_type AS type_info
        WHERE type_info.typnamespace = 'bursar'::regnamespace
          AND NOT (
              type_info.typname IN (
                  'schema_migrations',
                  '_schema_migrations'
              )
              AND type_info.typowner = current_user::regrole::oid
          )

        UNION ALL

        SELECT format('constraint:%I', constraint_info.conname)
        FROM pg_constraint AS constraint_info
        WHERE constraint_info.connamespace = 'bursar'::regnamespace
          AND NOT (
              constraint_info.conrelid = 'bursar.schema_migrations'::regclass
              AND constraint_info.conname = 'schema_migrations_pkey'
          )

        UNION ALL

        -- A schema-scoped default ACL can grant access to every trusted object
        -- created below even when the schema and bootstrap ledger start closed.
        SELECT format('default_acl:%s', owner_role.rolname)
        FROM pg_default_acl AS default_acl
        JOIN pg_roles AS owner_role
          ON owner_role.oid = default_acl.defaclrole
        WHERE default_acl.defaclnamespace = 'bursar'::regnamespace
    ) AS unexpected;

    IF v_unexpected_objects IS NOT NULL THEN
        RAISE EXCEPTION
            'unexpected pre-bootstrap objects in bursar schema: %',
            array_to_string(v_unexpected_objects, ', ')
            USING ERRCODE = '55000';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class AS table_info
        WHERE table_info.oid = 'bursar.schema_migrations'::regclass
          AND table_info.relkind = 'r'
          AND table_info.relpersistence = 'p'
          AND NOT table_info.relispartition
          AND NOT table_info.relrowsecurity
          AND NOT table_info.relforcerowsecurity
          AND table_info.relowner = current_user::regrole::oid
    )
    OR (
        SELECT array_agg(
            format(
                '%s:%s:%s:%s',
                column_info.attname,
                column_info.atttypid::regtype,
                CASE
                    WHEN column_info.attnotnull THEN 'true'
                    ELSE 'false'
                END,
                COALESCE(
                    pg_get_expr(default_info.adbin, default_info.adrelid),
                    ''
                )
            )
            ORDER BY column_info.attnum
        )
        FROM pg_attribute AS column_info
        LEFT JOIN pg_attrdef AS default_info
          ON default_info.adrelid = column_info.attrelid
         AND default_info.adnum = column_info.attnum
        WHERE column_info.attrelid = 'bursar.schema_migrations'::regclass
          AND column_info.attnum > 0
          AND NOT column_info.attisdropped
    ) IS DISTINCT FROM ARRAY[
        'version:text:true:',
        'checksum:text:true:',
        'applied_at:timestamp with time zone:true:now()'
    ]
    OR EXISTS (
        SELECT 1
        FROM pg_attribute AS column_info
        WHERE column_info.attrelid = 'bursar.schema_migrations'::regclass
          AND column_info.attnum > 0
          AND (
              column_info.attisdropped
              OR column_info.attidentity <> ''
              OR column_info.attgenerated <> ''
              OR column_info.attacl IS NOT NULL
          )
    )
    OR NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_info
        JOIN pg_index AS index_info
          ON index_info.indexrelid = constraint_info.conindid
        WHERE constraint_info.conrelid = 'bursar.schema_migrations'::regclass
          AND constraint_info.conname = 'schema_migrations_pkey'
          AND pg_get_constraintdef(constraint_info.oid) =
              'PRIMARY KEY (version)'
          AND index_info.indisvalid
          AND index_info.indisready
    )
    OR EXISTS (
        SELECT 1
        FROM pg_trigger AS trigger_info
        WHERE trigger_info.tgrelid = 'bursar.schema_migrations'::regclass
    )
    OR EXISTS (
        SELECT 1
        FROM pg_rewrite AS rule_info
        WHERE rule_info.ev_class = 'bursar.schema_migrations'::regclass
    )
    OR EXISTS (
        SELECT 1
        FROM pg_policy AS policy_info
        WHERE policy_info.polrelid = 'bursar.schema_migrations'::regclass
    )
    OR EXISTS (
        SELECT 1
        FROM pg_inherits AS inheritance_info
        WHERE inheritance_info.inhrelid =
                  'bursar.schema_migrations'::regclass
           OR inheritance_info.inhparent =
                  'bursar.schema_migrations'::regclass
    )
    OR EXISTS (
        SELECT 1
        FROM pg_class AS table_info
        CROSS JOIN LATERAL aclexplode(
            COALESCE(
                table_info.relacl,
                acldefault('r', table_info.relowner)
            )
        ) AS privilege_info
        WHERE table_info.oid = 'bursar.schema_migrations'::regclass
          AND privilege_info.grantee <> table_info.relowner
    )
    OR EXISTS (
        SELECT 1
        FROM pg_namespace AS namespace_info
        CROSS JOIN LATERAL aclexplode(
            COALESCE(
                namespace_info.nspacl,
                acldefault('n', namespace_info.nspowner)
            )
        ) AS privilege_info
        WHERE namespace_info.oid = 'bursar'::regnamespace
          AND privilege_info.grantee <> namespace_info.nspowner
    )
    OR EXISTS (
        SELECT 1
        FROM bursar.schema_migrations
    )
    THEN
        RAISE EXCEPTION 'invalid bursar.schema_migrations bootstrap table'
            USING ERRCODE = '55000';
    END IF;
END
$$;

-- The supported Python runner applies the complete migration set in one
-- transaction. Fail closed here as defense for direct/manual execution,
-- interrupted alternative runners, or future orchestration changes so they
-- cannot expose the schema through PostgreSQL's default function ACL.
REVOKE ALL ON SCHEMA bursar FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON TABLES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON SEQUENCES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON FUNCTIONS FROM PUBLIC;

CREATE SCHEMA IF NOT EXISTS extensions;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pg_jsonschema WITH SCHEMA extensions;

-- Fail installation if pre-existing extensions live elsewhere: all later
-- function bodies deliberately use stable, schema-qualified dependency names.
DO $$
DECLARE
    v_extension text;
    v_schema text;
BEGIN
    FOREACH v_extension IN ARRAY ARRAY['pgcrypto', 'pg_jsonschema'] LOOP
        SELECT namespace_info.nspname
        INTO v_schema
        FROM pg_extension AS extension_info
        JOIN pg_namespace AS namespace_info
          ON namespace_info.oid = extension_info.extnamespace
        WHERE extension_info.extname = v_extension;

        IF v_schema IS DISTINCT FROM 'extensions' THEN
            RAISE EXCEPTION
                'Bursar requires % in schema extensions, not %',
                v_extension,
                COALESCE(v_schema, '<missing>')
                USING ERRCODE = '3F000';
        END IF;
    END LOOP;
END
$$;

-- The extensions schema may be shared with host-owned extensions. Bursar does
-- not change its ambient ACLs; every dependency call is schema-qualified and
-- the runtime receives only its required functions in the security migration.

-- pg_partman owns the generic creation and retirement of Bursar's monthly
-- payload partitions. Keep its configuration and operator API isolated from
-- the extensions used by tenant-facing functions.
CREATE SCHEMA IF NOT EXISTS partman;

CREATE EXTENSION IF NOT EXISTS pg_partman WITH SCHEMA partman;

-- Partition maintenance relies on pg_partman 5.x behavior and a fixed schema,
-- so reject hosts whose installed dependency cannot satisfy that contract.
DO $$
DECLARE
    v_version text;
    v_schema text;
BEGIN
    SELECT extension_info.extversion, namespace_info.nspname
    INTO v_version, v_schema
    FROM pg_extension AS extension_info
    JOIN pg_namespace AS namespace_info
      ON namespace_info.oid = extension_info.extnamespace
    WHERE extension_info.extname = 'pg_partman';

    IF v_version IS NULL OR v_version !~ '^5\.' THEN
        RAISE EXCEPTION
            'Bursar requires pg_partman 5.x; installed version is %',
            COALESCE(v_version, '<missing>')
            USING ERRCODE = '0A000';
    END IF;

    IF v_schema <> 'partman' THEN
        RAISE EXCEPTION
            'Bursar requires pg_partman in schema partman, not %',
            v_schema
            USING ERRCODE = '3F000';
    END IF;
END
$$;

-- pg_partman may already serve other applications in the host database.
-- Preserve its ambient ACLs; Bursar does not grant its runtime roles USAGE on
-- this schema and reaches it only through locked-search_path operator wrappers.

SET client_encoding = 'UTF8';

SET standard_conforming_strings = on;

SET client_min_messages = warning;

-- Durable schema catalog documentation is attached with the rest of the
-- baseline's public object descriptions in 027_documentation.sql.

-- 2. Shared scalar and identity helpers

-- Generate time-local UUID keys without sacrificing distributed uniqueness;
-- time-biased insertion locality keeps the primary B-trees less fragmented.
CREATE FUNCTION bursar.uuid_v7()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
SET search_path TO ''
AS $$
DECLARE
    v_unix_ms bigint := floor(
        extract(epoch FROM clock_timestamp()) * 1000
    );
    v_bytes bytea := extensions.gen_random_bytes(16);
BEGIN
    -- RFC 9562 UUIDv7: 48-bit Unix millisecond timestamp, version 7,
    -- RFC 4122 variant, and 74 random bits. Values remain ordinary UUIDs
    -- while newly inserted B-tree keys have time locality.
    v_bytes := set_byte(v_bytes, 0, ((v_unix_ms >> 40) & 255)::integer);
    v_bytes := set_byte(v_bytes, 1, ((v_unix_ms >> 32) & 255)::integer);
    v_bytes := set_byte(v_bytes, 2, ((v_unix_ms >> 24) & 255)::integer);
    v_bytes := set_byte(v_bytes, 3, ((v_unix_ms >> 16) & 255)::integer);
    v_bytes := set_byte(v_bytes, 4, ((v_unix_ms >> 8) & 255)::integer);
    v_bytes := set_byte(v_bytes, 5, (v_unix_ms & 255)::integer);
    v_bytes := set_byte(
        v_bytes,
        6,
        (get_byte(v_bytes, 6) & 15) | 112
    );
    v_bytes := set_byte(
        v_bytes,
        8,
        (get_byte(v_bytes, 8) & 63) | 128
    );

    RETURN encode(v_bytes, 'hex')::uuid;
END
$$;

-- Centralize the rejection of PostgreSQL's non-finite numeric sentinels before
-- exact values enter balances, quantities, prices, or policy limits.
CREATE FUNCTION bursar.is_finite_numeric(
    p_value numeric
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
$$;

-- Accept exactly the values representable by Bursar's canonical credit type,
-- numeric(20, 6), without relying on an assignment cast that could round or
-- overflow after an idempotency digest or policy comparison was computed.
CREATE FUNCTION bursar.is_credit_numeric(
    p_value numeric
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT bursar.is_finite_numeric(p_value)
       AND p_value = round(p_value, 6)
       AND abs(p_value) < 100000000000000::numeric
$$;

-- Store exact credits without a numeric typmod: numeric(20,6) silently rounds
-- before a column CHECK can inspect the submitted value. This domain rejects
-- extra fractional precision and out-of-range values at the true table boundary.
CREATE DOMAIN bursar.credit_numeric AS numeric
CHECK (VALUE IS NULL OR bursar.is_credit_numeric(VALUE));

-- Minor-unit money crosses JSON/JavaScript SDK boundaries, so retain exact
-- integer semantics throughout the stack rather than merely fitting bigint.
CREATE FUNCTION bursar.is_nonnegative_safe_integer(
    p_value bigint
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_value BETWEEN 0 AND 9007199254740991
$$;

-- Require a non-zero minor-unit value while preserving the shared safe-integer
-- boundary used by every SDK and JSON transport.
CREATE FUNCTION bursar.is_positive_safe_integer(
    p_value bigint
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_value BETWEEN 1 AND 9007199254740991
$$;

-- PostgreSQL accepts +/-infinity for temporal types. Bursar uses NULL for an
-- open end, so every persisted business timestamp/date must instead be finite.
CREATE FUNCTION bursar.is_finite_timestamptz(
    p_value timestamptz
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL AND pg_catalog.isfinite(p_value)
$$;

-- Apply the same finite-only contract to calendar dates used by rollups.
CREATE FUNCTION bursar.is_finite_date(
    p_value date
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL AND pg_catalog.isfinite(p_value)
$$;

-- The runner-owned ledger is part of the persisted schema contract too.
ALTER TABLE bursar.schema_migrations
ADD CONSTRAINT schema_migrations_applied_at_finite_check
CHECK (bursar.is_finite_timestamptz(applied_at));

-- Quota thresholds are stored in the order they are crossed; enforcing their
-- canonical increasing form removes duplicate notifications and sort drift.
CREATE FUNCTION bursar.is_canonical_threshold_array(
    p_values integer []
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_values IS NOT NULL
       AND cardinality(p_values) <= 100
       AND (
           cardinality(p_values) = 0
           OR (
               array_ndims(p_values) = 1
               AND array_lower(p_values, 1) = 1
               AND NOT EXISTS (
                   SELECT 1
                   FROM unnest(p_values) WITH ORDINALITY
                        AS value(item, position)
                   WHERE value.item IS NULL
                      OR value.item NOT BETWEEN 1 AND 100
                      OR (
                          value.position > 1
                          AND value.item <= p_values[value.position - 1]
                      )
               )
           )
       )
$$;

-- Canonicalize exact numeric values for stable hashes and idempotency digests,
-- independent of representational trailing zeroes.
CREATE FUNCTION bursar.digest_numeric_text(
    p_value numeric
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path TO '' AS $$
    SELECT CASE
        WHEN p_value IS NULL THEN NULL
        WHEN p_value = trunc(p_value) THEN trunc(p_value)::text
        ELSE rtrim(rtrim(p_value::text,'0'),'.')
    END
$$;

-- 3. Generated catalog contract and validation

-- BEGIN GENERATED CATALOG SHAPE SCHEMA
-- Embed the generated catalog shape contract in PostgreSQL so write-boundary
-- and projection validators share structural definitions with the SDKs.
CREATE FUNCTION bursar.catalog_document_shape_schema()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $function$
    SELECT $catalog_json${"$defs":{"AdmissionConfig":{"additionalProperties":false,"properties":{"policies":{"additionalProperties":{"$ref":"#/$defs/AdmissionPolicy"},"type":"object"}},"type":"object"},"AdmissionPolicy":{"additionalProperties":false,"properties":{"max_in_flight":{"anyOf":[{"type":"integer"},{"type":"null"}]},"operations":{"additionalProperties":{"$ref":"#/$defs/OperationAdmission"},"type":"object"}},"type":"object"},"AfterGrantExpiry":{"additionalProperties":false,"properties":{"type":{"const":"after_grant","type":"string"},"interval":{"$ref":"#/$defs/BillingInterval"},"timezone":{"type":"string"}},"required":["type","interval"],"type":"object"},"AutoRechargeGuardrails":{"additionalProperties":false,"properties":{"eligible_topups":{"items":{"type":"string"},"type":"array"},"balance_below":{"$ref":"#/$defs/DecimalRange"},"rearm_above":{"type":"string"},"quantity":{"$ref":"#/$defs/IntegerRange"},"limits":{"$ref":"#/$defs/AutoRechargeLimits"}},"required":["eligible_topups","balance_below","rearm_above","quantity","limits"],"type":"object"},"AutoRechargeLimits":{"additionalProperties":false,"properties":{"max_purchases":{"type":"integer"},"window":{"anyOf":[{"$ref":"#/$defs/CalendarWindow"},{"$ref":"#/$defs/RollingWindow"}]},"max_charge_minor":{"type":"integer"},"cooldown":{"$ref":"#/$defs/Duration"},"max_consecutive_failures":{"type":"integer"},"failure_action":{"const":"pause","type":"string"}},"required":["max_purchases","window","max_charge_minor","cooldown"],"type":"object"},"Availability":{"additionalProperties":false,"properties":{"starts_at":{"anyOf":[{"type":"string"},{"type":"null"}]},"ends_at":{"anyOf":[{"type":"string"},{"type":"null"}]},"regions":{"items":{"type":"string"},"type":"array"}},"type":"object"},"BillingInterval":{"additionalProperties":false,"properties":{"unit":{"enum":["day","week","month","year"],"type":"string"},"count":{"type":"integer"}},"required":["unit"],"type":"object"},"BooleanFeature":{"additionalProperties":false,"properties":{"type":{"const":"boolean","type":"string"},"default":{"type":"boolean"}},"required":["type","default"],"type":"object"},"BucketDefinition":{"additionalProperties":false,"properties":{"priority":{"type":"integer"},"expiry":{"oneOf":[{"$ref":"#/$defs/NeverExpiry"},{"$ref":"#/$defs/AfterGrantExpiry"},{"$ref":"#/$defs/EndOfWindowExpiry"},{"$ref":"#/$defs/FixedExpiry"},{"$ref":"#/$defs/SubscriptionEndExpiry"}]}},"required":["priority"],"type":"object"},"CalendarWindow":{"additionalProperties":false,"properties":{"type":{"const":"calendar","type":"string"},"unit":{"enum":["day","week","month","year"],"type":"string"},"count":{"type":"integer"},"timezone":{"type":"string"}},"required":["type","unit"],"type":"object"},"CatalogConfig":{"additionalProperties":false,"properties":{"default_plan":{"anyOf":[{"type":"string"},{"type":"null"}]}},"type":"object"},"ChargeUnmatched":{"additionalProperties":false,"properties":{"action":{"const":"charge","type":"string"},"charge":{"oneOf":[{"$ref":"#/$defs/FlatCharge"},{"$ref":"#/$defs/PerUnitCharge"},{"$ref":"#/$defs/PackageCharge"},{"$ref":"#/$defs/GraduatedCharge"},{"$ref":"#/$defs/VolumeCharge"},{"$ref":"#/$defs/ExpressionCharge"},{"$ref":"#/$defs/SumCharge"}]}},"required":["action","charge"],"type":"object"},"CommerceConfig":{"additionalProperties":false,"properties":{"providers":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/StripeProvider"},{"$ref":"#/$defs/DodoProvider"},{"$ref":"#/$defs/CustomProvider"}]},"type":"object"},"offers":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/SubscriptionOffer"},{"$ref":"#/$defs/TopupOffer"}]},"type":"object"},"subscription_changes":{"anyOf":[{"$ref":"#/$defs/SubscriptionChanges"},{"type":"null"}]},"auto_recharge":{"anyOf":[{"$ref":"#/$defs/AutoRechargeGuardrails"},{"type":"null"}]}},"type":"object"},"CreditAllowance":{"additionalProperties":false,"properties":{"amount":{"type":"string"},"priority":{"type":"integer"},"window":{"oneOf":[{"$ref":"#/$defs/CalendarWindow"},{"$ref":"#/$defs/RollingWindow"},{"$ref":"#/$defs/PlanAssignmentWindow"}]}},"required":["amount","priority","window"],"type":"object"},"CreditDisplay":{"additionalProperties":false,"properties":{"currency":{"type":"string"},"units_per_major":{"type":"string"}},"required":["currency","units_per_major"],"type":"object"},"CreditLinePolicy":{"additionalProperties":false,"properties":{"type":{"const":"credit_line","type":"string"},"limit":{"type":"string"}},"required":["type","limit"],"type":"object"},"CreditsConfig":{"additionalProperties":false,"properties":{"buckets":{"additionalProperties":{"$ref":"#/$defs/BucketDefinition"},"type":"object"},"default_bucket":{"anyOf":[{"type":"string"},{"type":"null"}]},"policies":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/PrepaidCreditPolicy"},{"$ref":"#/$defs/CreditLinePolicy"}]},"type":"object"},"grant_programs":{"additionalProperties":{"$ref":"#/$defs/GrantProgram"},"type":"object"},"display":{"anyOf":[{"$ref":"#/$defs/CreditDisplay"},{"type":"null"}]}},"type":"object"},"CustomObjectReference":{"additionalProperties":false,"properties":{"type":{"const":"custom_object","type":"string"},"object_kind":{"enum":["subscription","one_time"],"type":"string"},"external_id":{"type":"string"}},"required":["type","object_kind","external_id"],"type":"object"},"CustomProvider":{"additionalProperties":false,"properties":{"type":{"const":"custom","type":"string"},"adapter":{"type":"string"}},"required":["type","adapter"],"type":"object"},"CycleGrant":{"additionalProperties":false,"properties":{"amount":{"type":"string"},"bucket":{"type":"string"},"renewal":{"enum":["replace_previous","accumulate"],"type":"string"},"expiry":{"oneOf":[{"$ref":"#/$defs/NeverExpiry"},{"$ref":"#/$defs/AfterGrantExpiry"},{"$ref":"#/$defs/EndOfWindowExpiry"},{"$ref":"#/$defs/FixedExpiry"},{"$ref":"#/$defs/SubscriptionEndExpiry"}]}},"required":["amount","bucket","renewal"],"type":"object"},"DecimalRange":{"additionalProperties":false,"properties":{"minimum":{"type":"string"},"maximum":{"type":"string"},"default":{"type":"string"}},"required":["minimum","maximum","default"],"type":"object"},"DimensionDefinition":{"additionalProperties":false,"properties":{"type":{"enum":["string","number","boolean"],"type":"string"},"required":{"type":"boolean"}},"required":["type"],"type":"object"},"DodoProductReference":{"additionalProperties":false,"properties":{"type":{"const":"dodo_product","type":"string"},"product_id":{"type":"string"}},"required":["type","product_id"],"type":"object"},"DodoProvider":{"additionalProperties":false,"properties":{"type":{"const":"dodo","type":"string"}},"required":["type"],"type":"object"},"Duration":{"additionalProperties":false,"properties":{"unit":{"enum":["second","minute","hour","day","week"],"type":"string"},"count":{"type":"integer"}},"required":["unit","count"],"type":"object"},"EndOfWindowExpiry":{"additionalProperties":false,"properties":{"type":{"const":"end_of_window","type":"string"},"window":{"anyOf":[{"$ref":"#/$defs/CalendarWindow"},{"$ref":"#/$defs/PlanAssignmentWindow"}]}},"required":["type","window"],"type":"object"},"EntitlementsConfig":{"additionalProperties":false,"properties":{"features":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/BooleanFeature"},{"$ref":"#/$defs/EnumFeature"},{"$ref":"#/$defs/IntegerFeature"},{"$ref":"#/$defs/StringFeature"}]},"type":"object"}},"type":"object"},"EnumFeature":{"additionalProperties":false,"properties":{"type":{"const":"enum","type":"string"},"values":{"items":{"type":"string"},"type":"array"},"default":{"type":"string"}},"required":["type","values","default"],"type":"object"},"EqualMatcher":{"additionalProperties":false,"properties":{"op":{"const":"eq","type":"string"},"value":{"anyOf":[{"type":"string"},{"type":"number"},{"type":"string"},{"type":"boolean"}]}},"required":["op","value"],"type":"object"},"ExpressionCharge":{"additionalProperties":false,"properties":{"type":{"const":"expression","type":"string"},"formula":{"type":"string"}},"required":["type","formula"],"type":"object"},"FixedExpiry":{"additionalProperties":false,"properties":{"type":{"const":"fixed_at","type":"string"},"at":{"type":"string"}},"required":["type","at"],"type":"object"},"FlatCharge":{"additionalProperties":false,"properties":{"type":{"const":"flat","type":"string"},"amount":{"type":"string"}},"required":["type","amount"],"type":"object"},"GraduatedCharge":{"additionalProperties":false,"properties":{"type":{"const":"graduated","type":"string"},"measure":{"type":"string"},"tiers":{"items":{"$ref":"#/$defs/GraduatedTier"},"type":"array"}},"required":["type","measure","tiers"],"type":"object"},"GraduatedTier":{"additionalProperties":false,"properties":{"up_to":{"anyOf":[{"type":"string"},{"type":"null"}]},"rate":{"type":"string"}},"required":["rate"],"type":"object"},"GrantAward":{"additionalProperties":false,"properties":{"recipient":{"enum":["subject","referrer"],"type":"string"},"amount":{"type":"string"},"bucket":{"type":"string"},"expiry":{"anyOf":[{"oneOf":[{"$ref":"#/$defs/NeverExpiry"},{"$ref":"#/$defs/AfterGrantExpiry"},{"$ref":"#/$defs/EndOfWindowExpiry"},{"$ref":"#/$defs/FixedExpiry"},{"$ref":"#/$defs/SubscriptionEndExpiry"}]},{"type":"null"}]}},"required":["amount","bucket"],"type":"object"},"GrantEligibility":{"additionalProperties":false,"properties":{"plans":{"items":{"type":"string"},"type":"array"},"regions":{"items":{"type":"string"},"type":"array"}},"type":"object"},"GrantProgram":{"additionalProperties":false,"properties":{"trigger":{"enum":["account_created","referral_completed","promo_code_redeemed","manual"],"type":"string"},"awards":{"items":{"$ref":"#/$defs/GrantAward"},"type":"array"},"availability":{"anyOf":[{"$ref":"#/$defs/Availability"},{"type":"null"}]},"eligibility":{"$ref":"#/$defs/GrantEligibility"},"max_awards_per_subject":{"type":"integer"},"idempotency_scope":{"enum":["subject","event"],"type":"string"}},"required":["trigger","awards"],"type":"object"},"InMatcher":{"additionalProperties":false,"properties":{"op":{"const":"in","type":"string"},"values":{"items":{"anyOf":[{"type":"string"},{"type":"number"},{"type":"string"},{"type":"boolean"}]},"type":"array"}},"required":["op","values"],"type":"object"},"IntegerFeature":{"additionalProperties":false,"properties":{"type":{"const":"integer","type":"string"},"default":{"type":"integer"},"minimum":{"anyOf":[{"type":"integer"},{"type":"null"}]},"maximum":{"anyOf":[{"type":"integer"},{"type":"null"}]}},"required":["type","default"],"type":"object"},"IntegerRange":{"additionalProperties":false,"properties":{"minimum":{"type":"integer"},"maximum":{"type":"integer"},"default":{"type":"integer"}},"required":["minimum","maximum","default"],"type":"object"},"MeasureDefinition":{"additionalProperties":false,"properties":{"unit":{"type":"string"}},"required":["unit"],"type":"object"},"NeverExpiry":{"additionalProperties":false,"properties":{"type":{"const":"never","type":"string"}},"required":["type"],"type":"object"},"NotInMatcher":{"additionalProperties":false,"properties":{"op":{"const":"not_in","type":"string"},"values":{"items":{"anyOf":[{"type":"string"},{"type":"number"},{"type":"string"},{"type":"boolean"}]},"type":"array"}},"required":["op","values"],"type":"object"},"OfferPrice":{"additionalProperties":false,"properties":{"amount_minor":{"type":"integer"},"currency":{"type":"string"},"tax_behavior":{"enum":["inclusive","exclusive","unspecified"],"type":"string"}},"required":["amount_minor","currency"],"type":"object"},"OperationAdmission":{"additionalProperties":false,"properties":{"max_in_flight":{"type":"integer"}},"required":["max_in_flight"],"type":"object"},"OperationDefinition":{"additionalProperties":false,"properties":{"measures":{"additionalProperties":{"$ref":"#/$defs/MeasureDefinition"},"type":"object"},"dimensions":{"additionalProperties":{"$ref":"#/$defs/DimensionDefinition"},"type":"object"}},"required":["measures"],"type":"object"},"OperationPricing":{"additionalProperties":false,"properties":{"rules":{"items":{"$ref":"#/$defs/PriceRule"},"type":"array"},"unmatched":{"oneOf":[{"$ref":"#/$defs/RejectUnmatched"},{"$ref":"#/$defs/ChargeUnmatched"}]}},"required":["unmatched"],"type":"object"},"PackageCharge":{"additionalProperties":false,"properties":{"type":{"const":"package","type":"string"},"measure":{"type":"string"},"units":{"type":"string"},"amount":{"type":"string"},"rounding":{"enum":["ceil","floor","nearest"],"type":"string"}},"required":["type","measure","units","amount"],"type":"object"},"PerUnitCharge":{"additionalProperties":false,"properties":{"type":{"const":"per_unit","type":"string"},"measure":{"type":"string"},"rate":{"type":"string"},"unit_size":{"type":"string"}},"required":["type","measure","rate"],"type":"object"},"PlanAssignmentWindow":{"additionalProperties":false,"properties":{"type":{"const":"plan_assignment","type":"string"},"interval":{"$ref":"#/$defs/BillingInterval"},"timezone":{"type":"string"}},"required":["type","interval"],"type":"object"},"PlanDefinition":{"additionalProperties":false,"properties":{"display_name":{"type":"string"},"rank":{"type":"integer"},"description":{"anyOf":[{"type":"string"},{"type":"null"}]},"rate_card":{"anyOf":[{"type":"string"},{"type":"null"}]},"allowed_operations":{"items":{"type":"string"},"type":"array"},"features":{"additionalProperties":{"anyOf":[{"type":"boolean"},{"type":"integer"},{"type":"string"}]},"type":"object"},"credit_allowance":{"anyOf":[{"$ref":"#/$defs/CreditAllowance"},{"type":"null"}]},"quotas":{"additionalProperties":{"$ref":"#/$defs/QuotaDefinition"},"type":"object"},"credit_policy":{"anyOf":[{"type":"string"},{"type":"null"}]},"admission_policy":{"anyOf":[{"type":"string"},{"type":"null"}]},"evolution":{"anyOf":[{"$ref":"#/$defs/PlanEvolution"},{"type":"null"}]}},"required":["display_name"],"type":"object"},"PlanEvolution":{"additionalProperties":false,"properties":{"default_rollout":{"enum":["immediate","next_renewal","new_assignments_only"],"type":"string"}},"required":["default_rollout"],"type":"object"},"PrefixMatcher":{"additionalProperties":false,"properties":{"op":{"const":"prefix","type":"string"},"value":{"type":"string"}},"required":["op","value"],"type":"object"},"PrepaidCreditPolicy":{"additionalProperties":false,"properties":{"type":{"const":"prepaid","type":"string"}},"required":["type"],"type":"object"},"PriceRule":{"additionalProperties":false,"properties":{"when":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/EqualMatcher"},{"$ref":"#/$defs/InMatcher"},{"$ref":"#/$defs/NotInMatcher"},{"$ref":"#/$defs/PrefixMatcher"},{"$ref":"#/$defs/RangeMatcher"}]},"type":"object"},"charge":{"oneOf":[{"$ref":"#/$defs/FlatCharge"},{"$ref":"#/$defs/PerUnitCharge"},{"$ref":"#/$defs/PackageCharge"},{"$ref":"#/$defs/GraduatedCharge"},{"$ref":"#/$defs/VolumeCharge"},{"$ref":"#/$defs/ExpressionCharge"},{"$ref":"#/$defs/SumCharge"}]}},"required":["when","charge"],"type":"object"},"PricingConfig":{"additionalProperties":false,"properties":{"operations":{"additionalProperties":{"$ref":"#/$defs/OperationDefinition"},"type":"object"},"rate_cards":{"additionalProperties":{"$ref":"#/$defs/RateCard"},"type":"object"}},"required":["operations","rate_cards"],"type":"object"},"QuantityBounds":{"additionalProperties":false,"properties":{"minimum":{"type":"integer"},"maximum":{"type":"integer"},"default":{"type":"integer"}},"type":"object"},"QuotaDefinition":{"additionalProperties":false,"properties":{"operation":{"type":"string"},"measure":{"type":"string"},"limit":{"type":"string"},"window":{"oneOf":[{"$ref":"#/$defs/CalendarWindow"},{"$ref":"#/$defs/RollingWindow"},{"$ref":"#/$defs/PlanAssignmentWindow"}]},"enforcement":{"enum":["block","allow"],"type":"string"},"emit_at_percent":{"items":{"type":"integer"},"type":"array"}},"required":["operation","measure","limit","window","enforcement"],"type":"object"},"RangeMatcher":{"additionalProperties":false,"properties":{"op":{"const":"range","type":"string"},"gt":{"anyOf":[{"type":"string"},{"type":"null"}]},"gte":{"anyOf":[{"type":"string"},{"type":"null"}]},"lt":{"anyOf":[{"type":"string"},{"type":"null"}]},"lte":{"anyOf":[{"type":"string"},{"type":"null"}]}},"required":["op"],"type":"object"},"RateCard":{"additionalProperties":false,"properties":{"extends":{"anyOf":[{"type":"string"},{"type":"null"}]},"operations":{"additionalProperties":{"$ref":"#/$defs/OperationPricing"},"type":"object"}},"type":"object"},"RejectUnmatched":{"additionalProperties":false,"properties":{"action":{"const":"reject","type":"string"}},"required":["action"],"type":"object"},"RollingWindow":{"additionalProperties":false,"properties":{"type":{"const":"rolling","type":"string"},"duration":{"$ref":"#/$defs/Duration"}},"required":["type","duration"],"type":"object"},"StringFeature":{"additionalProperties":false,"properties":{"type":{"const":"string","type":"string"},"default":{"type":"string"},"pattern":{"anyOf":[{"type":"string"},{"type":"null"}]}},"required":["type","default"],"type":"object"},"StripePriceReference":{"additionalProperties":false,"properties":{"type":{"const":"stripe_price","type":"string"},"price_id":{"type":"string"}},"required":["type","price_id"],"type":"object"},"StripeProvider":{"additionalProperties":false,"properties":{"type":{"const":"stripe","type":"string"}},"required":["type"],"type":"object"},"SubscriptionChangePolicy":{"additionalProperties":false,"properties":{"effective":{"enum":["immediate","renewal"],"type":"string"},"proration":{"enum":["prorated","none"],"type":"string"},"payment_failure":{"enum":["prevent_change","apply_change"],"type":"string"}},"required":["effective","proration"],"type":"object"},"SubscriptionChanges":{"additionalProperties":false,"properties":{"upgrade":{"anyOf":[{"$ref":"#/$defs/SubscriptionChangePolicy"},{"type":"null"}]},"downgrade":{"anyOf":[{"$ref":"#/$defs/SubscriptionChangePolicy"},{"type":"null"}]},"lateral":{"anyOf":[{"$ref":"#/$defs/SubscriptionChangePolicy"},{"type":"null"}]},"cadence_change":{"anyOf":[{"$ref":"#/$defs/SubscriptionChangePolicy"},{"type":"null"}]}},"type":"object"},"SubscriptionEndExpiry":{"additionalProperties":false,"properties":{"type":{"const":"subscription_end","type":"string"}},"required":["type"],"type":"object"},"SubscriptionOffer":{"additionalProperties":false,"properties":{"display_name":{"type":"string"},"description":{"anyOf":[{"type":"string"},{"type":"null"}]},"sort_order":{"type":"integer"},"availability":{"anyOf":[{"$ref":"#/$defs/Availability"},{"type":"null"}]},"price":{"$ref":"#/$defs/OfferPrice"},"providers":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/StripePriceReference"},{"$ref":"#/$defs/DodoProductReference"},{"$ref":"#/$defs/CustomObjectReference"}]},"type":"object"},"type":{"const":"subscription","type":"string"},"plan":{"type":"string"},"billing_interval":{"$ref":"#/$defs/BillingInterval"},"trial":{"anyOf":[{"$ref":"#/$defs/BillingInterval"},{"type":"null"}]},"cycle_grant":{"anyOf":[{"$ref":"#/$defs/CycleGrant"},{"type":"null"}]}},"required":["display_name","price","providers","type","plan","billing_interval"],"type":"object"},"SumCharge":{"additionalProperties":false,"properties":{"type":{"const":"sum","type":"string"},"components":{"items":{"oneOf":[{"$ref":"#/$defs/FlatCharge"},{"$ref":"#/$defs/PerUnitCharge"},{"$ref":"#/$defs/PackageCharge"},{"$ref":"#/$defs/GraduatedCharge"},{"$ref":"#/$defs/VolumeCharge"},{"$ref":"#/$defs/ExpressionCharge"},{"$ref":"#/$defs/SumCharge"}]},"type":"array"}},"required":["type","components"],"type":"object"},"TopupOffer":{"additionalProperties":false,"properties":{"display_name":{"type":"string"},"description":{"anyOf":[{"type":"string"},{"type":"null"}]},"sort_order":{"type":"integer"},"availability":{"anyOf":[{"$ref":"#/$defs/Availability"},{"type":"null"}]},"price":{"$ref":"#/$defs/OfferPrice"},"providers":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/StripePriceReference"},{"$ref":"#/$defs/DodoProductReference"},{"$ref":"#/$defs/CustomObjectReference"}]},"type":"object"},"type":{"const":"topup","type":"string"},"credits_per_unit":{"type":"string"},"quantity":{"$ref":"#/$defs/QuantityBounds"},"bucket":{"type":"string"},"expiry":{"anyOf":[{"oneOf":[{"$ref":"#/$defs/NeverExpiry"},{"$ref":"#/$defs/AfterGrantExpiry"},{"$ref":"#/$defs/EndOfWindowExpiry"},{"$ref":"#/$defs/FixedExpiry"},{"$ref":"#/$defs/SubscriptionEndExpiry"}]},{"type":"null"}]},"lot_behavior":{"enum":["separate_lots","merge_and_refresh"],"type":"string"}},"required":["display_name","price","providers","type","credits_per_unit","bucket"],"type":"object"},"VolumeCharge":{"additionalProperties":false,"properties":{"type":{"const":"volume","type":"string"},"measure":{"type":"string"},"tiers":{"items":{"$ref":"#/$defs/GraduatedTier"},"type":"array"}},"required":["type","measure","tiers"],"type":"object"}},"additionalProperties":false,"properties":{"version":{"const":1,"type":"integer"},"catalog":{"$ref":"#/$defs/CatalogConfig"},"pricing":{"anyOf":[{"$ref":"#/$defs/PricingConfig"},{"type":"null"}]},"credits":{"$ref":"#/$defs/CreditsConfig"},"entitlements":{"$ref":"#/$defs/EntitlementsConfig"},"admission":{"$ref":"#/$defs/AdmissionConfig"},"plans":{"additionalProperties":{"$ref":"#/$defs/PlanDefinition"},"type":"object"},"commerce":{"$ref":"#/$defs/CommerceConfig"}},"required":["version","credits"],"type":"object"}$catalog_json$::jsonb
$function$;
-- END GENERATED CATALOG SHAPE SCHEMA

-- Reject malformed catalog documents at their write boundary and surface the
-- first schema diagnostic as a deterministic data exception.
CREATE FUNCTION bursar.require_catalog_document_shape(
    p_document jsonb
) RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SET search_path TO ''
AS $$
DECLARE
    v_schema json := bursar.catalog_document_shape_schema()::json;
    v_errors text[];
BEGIN
    IF p_document IS NULL
       OR NOT COALESCE(
           extensions.jsonb_matches_schema(v_schema, p_document),
           false
       )
    THEN
        v_errors := extensions.jsonschema_validation_errors(
            v_schema,
            p_document::json
        );
        RAISE EXCEPTION 'invalid_catalog: %',
            COALESCE(v_errors[1], 'document does not match its JSON Schema')
            USING ERRCODE = '22023';
    END IF;
END
$$;

-- Validate a value against an arbitrary fragment while retaining access to
-- the catalog's shared definitions; invalid fragments fail closed.
CREATE FUNCTION bursar.matches_catalog_fragment(
    p_value jsonb,
    p_fragment_schema jsonb
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    WITH composed AS (
        SELECT
            jsonb_build_object(
                '$defs',
                bursar.catalog_document_shape_schema()->'$defs'
            ) || p_fragment_schema AS schema
    )
    SELECT COALESCE(
        CASE
            WHEN p_value IS NULL OR p_fragment_schema IS NULL THEN false
            WHEN NOT extensions.jsonschema_is_valid(composed.schema::json)
                THEN false
            ELSE extensions.jsonb_matches_schema(
                composed.schema::json,
                p_value
            )
        END,
        false
    )
    FROM composed
$$;

-- Let projection constraints accept one of a named set of catalog definitions
-- without duplicating or drifting from the generated schema.
CREATE FUNCTION bursar.matches_catalog_definitions(
    p_value jsonb,
    VARIADIC p_definition_names text []
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    WITH catalog AS (
        SELECT bursar.catalog_document_shape_schema() AS schema
    ),
    requested AS (
        SELECT
            count(*) > 0
                AND bool_and(catalog.schema->'$defs' ? definition_name)
                AS definitions_exist,
            COALESCE(
                jsonb_agg(
                    jsonb_build_object(
                        '$ref',
                        '#/$defs/' || definition_name
                    )
                    ORDER BY definition_name
                ),
                '[]'::jsonb
            ) AS alternatives
        FROM catalog
        CROSS JOIN LATERAL unnest(p_definition_names) AS requested_definition(
            definition_name
        )
    )
    SELECT COALESCE(
        CASE
            WHEN p_value IS NULL OR NOT requested.definitions_exist THEN false
            ELSE extensions.jsonb_matches_schema(
                jsonb_build_object(
                    '$defs', catalog.schema->'$defs',
                    'anyOf', requested.alternatives
                )::json,
                p_value
            )
        END,
        false
    )
    FROM catalog
    CROSS JOIN requested
$$;

-- Derive the runtime value contract from a feature definition so stored
-- entitlement values cannot disagree with their catalog-declared type/range.
CREATE FUNCTION bursar.entitlement_value_schema(
    p_definition jsonb
) RETURNS json
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT CASE p_definition->>'type'
        WHEN 'boolean' THEN
            jsonb_build_object('type', 'boolean')
        WHEN 'integer' THEN
            jsonb_strip_nulls(jsonb_build_object(
                'type', 'integer',
                'minimum', p_definition->'minimum',
                'maximum', p_definition->'maximum'
            ))
        WHEN 'string' THEN
            jsonb_strip_nulls(jsonb_build_object(
                'type', 'string',
                'pattern', p_definition->'pattern'
            ))
        WHEN 'enum' THEN
            jsonb_build_object(
                'type', 'string',
                'enum', p_definition->'values'
            )
        ELSE NULL
    END::json
$$;

-- 4. Bounded-value and backend helpers

-- Bound opaque JSON metadata at the database boundary to protect rows and
-- outbox payloads from unbounded tenant-controlled documents.
CREATE FUNCTION bursar.is_bounded_json_object(
    p_value jsonb,
    p_max_bytes integer
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_max_bytes > 0
       AND COALESCE(
           extensions.jsonb_matches_schema(
               '{"type":"object"}'::json,
               p_value
           ),
           false
       )
       AND octet_length(p_value::text) <= p_max_bytes
$$;

-- Apply a reusable character ceiling to externally supplied identifiers and
-- labels while allowing callers to decide whether empty text is meaningful.
CREATE FUNCTION bursar.is_bounded_text(
    p_value text,
    p_max_characters integer
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_max_characters > 0
       AND length(p_value) <= p_max_characters
$$;

-- Require canonical, trimmed identifiers so uniqueness and provider matching
-- cannot diverge through invisible leading or trailing whitespace.
CREATE FUNCTION bursar.is_nonempty_bounded_text(
    p_value text,
    p_max_characters integer
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_max_characters IS NOT NULL
       AND p_max_characters > 0
       AND p_value = btrim(p_value)
       AND length(p_value) BETWEEN 1 AND p_max_characters
$$;

-- Validate compact one-dimensional identifier sets before a table-specific
-- trigger resolves their relational targets. Empty arrays are allowed because
-- an empty allow-list has an intentional catalog meaning.
CREATE FUNCTION bursar.is_canonical_identifier_array(
    p_values text [],
    p_max_items integer DEFAULT 1000,
    p_max_characters integer DEFAULT 255
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_values IS NOT NULL
       AND p_max_items > 0
       AND p_max_characters > 0
       AND cardinality(p_values) <= p_max_items
       AND (
           cardinality(p_values) = 0
           OR (
               array_ndims(p_values) = 1
               AND array_lower(p_values, 1) = 1
               AND NOT EXISTS (
                   SELECT 1
                   FROM unnest(p_values) AS value(item)
                   WHERE NOT bursar.is_nonempty_bounded_text(
                       value.item,
                       p_max_characters
                   )
               )
               AND cardinality(p_values) = (
                   SELECT count(DISTINCT value.item)
                   FROM unnest(p_values) AS value(item)
               )
           )
       )
$$;

-- Return conservative seconds for rolling windows and zero for non-rolling
-- policies; invalid or overflowing rolling policies return NULL to fail closed.
CREATE FUNCTION bursar.policy_duration_seconds(
    p_policy jsonb
) RETURNS bigint
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
DECLARE
    v_unit text;
    v_count bigint;
    v_unit_seconds bigint;
BEGIN
    IF p_policy IS NULL OR p_policy->>'type' <> 'rolling' THEN
        RETURN 0;
    END IF;

    IF NOT bursar.matches_catalog_definitions(p_policy, 'RollingWindow') THEN
        RETURN NULL;
    END IF;

    v_unit := p_policy #>> '{duration,unit}';
    v_count := (p_policy #>> '{duration,count}')::bigint;

    IF v_count IS NULL OR v_count < 1 THEN
        RETURN NULL;
    END IF;

    v_unit_seconds := CASE v_unit
        WHEN 'second' THEN 1
        WHEN 'minute' THEN 60
        WHEN 'hour' THEN 3600
        WHEN 'day' THEN 86400
        WHEN 'week' THEN 604800
        -- Retention validation deliberately uses conservative upper bounds.
        WHEN 'month' THEN 2678400
        WHEN 'year' THEN 31622400
        ELSE NULL
    END;

    IF v_unit_seconds IS NULL
       OR v_count > 9223372036854775807 / v_unit_seconds
    THEN
        RETURN NULL;
    END IF;

    RETURN v_count * v_unit_seconds;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN NULL;
END
$$;

-- Use one default identifier bound where no domain-specific maximum is needed.
CREATE FUNCTION bursar.is_nonempty_text(
    p_value text
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT bursar.is_nonempty_bounded_text(p_value, 255)
$$;

-- Resolve the configured usage store from a closed set and default safely to
-- PostgreSQL when no transaction/session override exists.
CREATE FUNCTION bursar.current_usage_backend()
RETURNS text
LANGUAGE sql
STABLE
SET search_path TO ''
AS $$
    SELECT CASE
        WHEN current_setting('bursar.usage_backend', true)
             IN ('postgres', 'clickhouse')
        THEN current_setting('bursar.usage_backend', true)
        ELSE 'postgres'
    END
$$;

-- Resolve whether billing payload bodies remain in PostgreSQL or S3 while
-- keeping relational billing facts independent of that storage choice.
CREATE FUNCTION bursar.current_billing_payload_backend()
RETURNS text
LANGUAGE sql
STABLE
SET search_path TO ''
AS $$
    SELECT CASE
        WHEN current_setting('bursar.billing_payload_backend', true)
             IN ('postgres', 's3')
        THEN current_setting('bursar.billing_payload_backend', true)
        ELSE 'postgres'
    END
$$;

-- 5. Tenant context and provider environment

-- Tenancy is part of the baseline data model. Application code binds exactly
-- one tenant to each transaction with:
--
--   SELECT set_config('bursar.tenant_id', '<uuid>', true)
CREATE TABLE bursar.tenants (
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    slug text NOT NULL UNIQUE CHECK (
        bursar.is_bounded_text(slug, 100)
        AND slug ~ '^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$'
    ),
    display_name text,
    status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended', 'closed')),
    created_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(created_at)),
    updated_at timestamptz NOT NULL DEFAULT now()
    CHECK (bursar.is_finite_timestamptz(updated_at)),
    CHECK (
        display_name IS NULL
        OR bursar.is_bounded_text(display_name, 255)
    )
);

-- Read transaction-local tenant identity without inventing a fallback; NULL is
-- useful to policy predicates that must deny access rather than raise.
CREATE FUNCTION bursar.current_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
SET search_path TO ''
AS $$
    SELECT NULLIF(current_setting('bursar.tenant_id', true), '')::uuid
$$;

-- Provide a strict tenant accessor for mutation paths where missing context is
-- an authentication failure, not an empty result set.
CREATE FUNCTION bursar.require_tenant_id()
RETURNS uuid
LANGUAGE plpgsql
STABLE
SET search_path TO ''
AS $$
DECLARE
    v_tenant uuid := bursar.current_tenant_id();
BEGIN
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'bursar tenant context is required'
            USING ERRCODE = '28000';
    END IF;
    RETURN v_tenant;
END
$$;

-- Gate tenant work on lifecycle state so suspension or closure disables new
-- accounting activity without rewriting historical ownership.
CREATE FUNCTION bursar.current_tenant_is_active()
RETURNS boolean
LANGUAGE sql
STABLE
SET search_path TO ''
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bursar.tenants
        WHERE id = bursar.current_tenant_id()
          AND status = 'active'
    )
$$;

REVOKE ALL ON FUNCTION bursar.current_tenant_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.require_tenant_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.current_tenant_is_active() FROM PUBLIC;

-- Require an explicit live/test/sandbox provider namespace to prevent billing
-- identifiers or events from crossing provider environments.
CREATE FUNCTION bursar.current_provider_environment()
RETURNS text
LANGUAGE plpgsql
STABLE
SET search_path TO ''
AS $$
DECLARE
    v_environment text := NULLIF(
        current_setting('bursar.provider_environment', true),
        ''
    );
BEGIN
    IF v_environment IS NULL
       OR v_environment NOT IN ('live', 'test', 'sandbox')
    THEN
        RAISE EXCEPTION 'bursar provider environment is required'
            USING ERRCODE = '22023';
    END IF;

    RETURN v_environment;
END
$$;
