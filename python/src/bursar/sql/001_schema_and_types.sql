CREATE SCHEMA IF NOT EXISTS bursar;

CREATE SCHEMA IF NOT EXISTS extensions;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

SET client_encoding = 'UTF8';

SET standard_conforming_strings = on;

SET client_min_messages = warning;

COMMENT ON SCHEMA bursar IS
'Backend-only Bursar accounting, catalog, and billing schema.';

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

CREATE FUNCTION bursar.require_json_object(
    p_value jsonb,
    p_name text
) RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
SET search_path TO ''
AS $$
BEGIN
    IF p_value IS NULL OR jsonb_typeof(p_value) <> 'object' THEN
        RAISE EXCEPTION '% must be a JSON object', p_name
            USING ERRCODE = '22023';
    END IF;

    RETURN p_value;
END
$$;

-- BEGIN GENERATED CATALOG SHAPE SCHEMA
CREATE FUNCTION bursar.catalog_document_shape_schema()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $function$
    SELECT $catalog_json${"$defs":{"AdmissionConfig":{"additionalProperties":false,"properties":{"policies":{"additionalProperties":{"$ref":"#/$defs/AdmissionPolicy"},"type":"object"}},"type":"object"},"AdmissionPolicy":{"additionalProperties":false,"properties":{"max_in_flight":{"anyOf":[{"type":"integer"},{"type":"null"}]},"operations":{"additionalProperties":{"$ref":"#/$defs/OperationAdmission"},"type":"object"}},"type":"object"},"AfterGrantExpiry":{"additionalProperties":false,"properties":{"type":{"const":"after_grant","type":"string"},"interval":{"$ref":"#/$defs/BillingInterval"},"timezone":{"type":"string"}},"required":["type","interval"],"type":"object"},"AutoRechargeGuardrails":{"additionalProperties":false,"properties":{"eligible_topups":{"items":{"type":"string"},"type":"array"},"balance_below":{"$ref":"#/$defs/DecimalRange"},"rearm_above":{"type":"string"},"quantity":{"$ref":"#/$defs/IntegerRange"},"limits":{"$ref":"#/$defs/AutoRechargeLimits"}},"required":["eligible_topups","balance_below","rearm_above","quantity","limits"],"type":"object"},"AutoRechargeLimits":{"additionalProperties":false,"properties":{"max_purchases":{"type":"integer"},"window":{"anyOf":[{"$ref":"#/$defs/CalendarWindow"},{"$ref":"#/$defs/RollingWindow"}]},"max_charge_minor":{"type":"integer"},"cooldown":{"$ref":"#/$defs/Duration"},"max_consecutive_failures":{"type":"integer"},"failure_action":{"const":"pause","type":"string"}},"required":["max_purchases","window","max_charge_minor","cooldown"],"type":"object"},"Availability":{"additionalProperties":false,"properties":{"starts_at":{"anyOf":[{"type":"string"},{"type":"null"}]},"ends_at":{"anyOf":[{"type":"string"},{"type":"null"}]},"regions":{"items":{"type":"string"},"type":"array"}},"type":"object"},"BillingInterval":{"additionalProperties":false,"properties":{"unit":{"enum":["day","week","month","year"],"type":"string"},"count":{"type":"integer"}},"required":["unit"],"type":"object"},"BooleanFeature":{"additionalProperties":false,"properties":{"type":{"const":"boolean","type":"string"},"default":{"type":"boolean"}},"required":["type","default"],"type":"object"},"BucketDefinition":{"additionalProperties":false,"properties":{"priority":{"type":"integer"},"expiry":{"oneOf":[{"$ref":"#/$defs/NeverExpiry"},{"$ref":"#/$defs/AfterGrantExpiry"},{"$ref":"#/$defs/EndOfWindowExpiry"},{"$ref":"#/$defs/FixedExpiry"},{"$ref":"#/$defs/SubscriptionEndExpiry"}]}},"required":["priority"],"type":"object"},"CalendarWindow":{"additionalProperties":false,"properties":{"type":{"const":"calendar","type":"string"},"unit":{"enum":["day","week","month","year"],"type":"string"},"count":{"type":"integer"},"timezone":{"type":"string"}},"required":["type","unit"],"type":"object"},"CatalogConfig":{"additionalProperties":false,"properties":{"default_plan":{"anyOf":[{"type":"string"},{"type":"null"}]}},"type":"object"},"ChargeUnmatched":{"additionalProperties":false,"properties":{"action":{"const":"charge","type":"string"},"charge":{"oneOf":[{"$ref":"#/$defs/FlatCharge"},{"$ref":"#/$defs/PerUnitCharge"},{"$ref":"#/$defs/PackageCharge"},{"$ref":"#/$defs/GraduatedCharge"},{"$ref":"#/$defs/VolumeCharge"},{"$ref":"#/$defs/ExpressionCharge"},{"$ref":"#/$defs/SumCharge"}]}},"required":["action","charge"],"type":"object"},"CommerceConfig":{"additionalProperties":false,"properties":{"providers":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/StripeProvider"},{"$ref":"#/$defs/DodoProvider"},{"$ref":"#/$defs/CustomProvider"}]},"type":"object"},"offers":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/SubscriptionOffer"},{"$ref":"#/$defs/TopupOffer"}]},"type":"object"},"subscription_changes":{"anyOf":[{"$ref":"#/$defs/SubscriptionChanges"},{"type":"null"}]},"auto_recharge":{"anyOf":[{"$ref":"#/$defs/AutoRechargeGuardrails"},{"type":"null"}]}},"type":"object"},"CreditAccounting":{"additionalProperties":false,"properties":{"unit":{"const":"credit","type":"string"},"scale":{"const":6,"type":"integer"},"rounding":{"const":"half_up","type":"string"}},"type":"object"},"CreditAllowance":{"additionalProperties":false,"properties":{"amount":{"type":"string"},"priority":{"anyOf":[{"type":"integer"},{"type":"null"}]},"window":{"oneOf":[{"$ref":"#/$defs/CalendarWindow"},{"$ref":"#/$defs/RollingWindow"},{"$ref":"#/$defs/PlanAssignmentWindow"}]}},"required":["amount","window"],"type":"object"},"CreditDisplay":{"additionalProperties":false,"properties":{"currency":{"type":"string"},"units_per_major":{"type":"string"}},"required":["currency","units_per_major"],"type":"object"},"CreditLinePolicy":{"additionalProperties":false,"properties":{"type":{"const":"credit_line","type":"string"},"limit":{"type":"string"}},"required":["type","limit"],"type":"object"},"CreditsConfig":{"additionalProperties":false,"properties":{"accounting":{"$ref":"#/$defs/CreditAccounting"},"buckets":{"additionalProperties":{"$ref":"#/$defs/BucketDefinition"},"type":"object"},"default_bucket":{"anyOf":[{"type":"string"},{"type":"null"}]},"policies":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/PrepaidCreditPolicy"},{"$ref":"#/$defs/CreditLinePolicy"}]},"type":"object"},"grant_programs":{"additionalProperties":{"$ref":"#/$defs/GrantProgram"},"type":"object"},"display":{"anyOf":[{"$ref":"#/$defs/CreditDisplay"},{"type":"null"}]}},"type":"object"},"CustomObjectReference":{"additionalProperties":false,"properties":{"type":{"const":"custom_object","type":"string"},"object_kind":{"enum":["subscription","one_time"],"type":"string"},"external_id":{"type":"string"}},"required":["type","object_kind","external_id"],"type":"object"},"CustomProvider":{"additionalProperties":false,"properties":{"type":{"const":"custom","type":"string"},"adapter":{"type":"string"}},"required":["type","adapter"],"type":"object"},"CycleGrant":{"additionalProperties":false,"properties":{"amount":{"type":"string"},"bucket":{"type":"string"},"renewal":{"enum":["replace_previous","accumulate"],"type":"string"},"expiry":{"oneOf":[{"$ref":"#/$defs/NeverExpiry"},{"$ref":"#/$defs/AfterGrantExpiry"},{"$ref":"#/$defs/EndOfWindowExpiry"},{"$ref":"#/$defs/FixedExpiry"},{"$ref":"#/$defs/SubscriptionEndExpiry"}]}},"required":["amount","bucket","renewal"],"type":"object"},"DecimalRange":{"additionalProperties":false,"properties":{"minimum":{"type":"string"},"maximum":{"type":"string"},"default":{"type":"string"}},"required":["minimum","maximum","default"],"type":"object"},"DimensionDefinition":{"additionalProperties":false,"properties":{"type":{"enum":["string","number","boolean"],"type":"string"},"required":{"type":"boolean"}},"required":["type"],"type":"object"},"DodoProductReference":{"additionalProperties":false,"properties":{"type":{"const":"dodo_product","type":"string"},"product_id":{"type":"string"}},"required":["type","product_id"],"type":"object"},"DodoProvider":{"additionalProperties":false,"properties":{"type":{"const":"dodo","type":"string"}},"required":["type"],"type":"object"},"Duration":{"additionalProperties":false,"properties":{"unit":{"enum":["second","minute","hour","day","week"],"type":"string"},"count":{"type":"integer"}},"required":["unit","count"],"type":"object"},"EndOfWindowExpiry":{"additionalProperties":false,"properties":{"type":{"const":"end_of_window","type":"string"},"window":{"anyOf":[{"$ref":"#/$defs/CalendarWindow"},{"$ref":"#/$defs/PlanAssignmentWindow"}]}},"required":["type","window"],"type":"object"},"EntitlementsConfig":{"additionalProperties":false,"properties":{"features":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/BooleanFeature"},{"$ref":"#/$defs/EnumFeature"},{"$ref":"#/$defs/IntegerFeature"},{"$ref":"#/$defs/StringFeature"}]},"type":"object"}},"type":"object"},"EnumFeature":{"additionalProperties":false,"properties":{"type":{"const":"enum","type":"string"},"values":{"items":{"type":"string"},"type":"array"},"default":{"type":"string"}},"required":["type","values","default"],"type":"object"},"EqualMatcher":{"additionalProperties":false,"properties":{"op":{"const":"eq","type":"string"},"value":{"anyOf":[{"type":"string"},{"type":"number"},{"type":"string"},{"type":"boolean"}]}},"required":["op","value"],"type":"object"},"ExpressionCharge":{"additionalProperties":false,"properties":{"type":{"const":"expression","type":"string"},"formula":{"type":"string"}},"required":["type","formula"],"type":"object"},"FixedExpiry":{"additionalProperties":false,"properties":{"type":{"const":"fixed_at","type":"string"},"at":{"type":"string"}},"required":["type","at"],"type":"object"},"FlatCharge":{"additionalProperties":false,"properties":{"type":{"const":"flat","type":"string"},"amount":{"type":"string"}},"required":["type","amount"],"type":"object"},"GraduatedCharge":{"additionalProperties":false,"properties":{"type":{"const":"graduated","type":"string"},"measure":{"type":"string"},"tiers":{"items":{"$ref":"#/$defs/GraduatedTier"},"type":"array"}},"required":["type","measure","tiers"],"type":"object"},"GraduatedTier":{"additionalProperties":false,"properties":{"up_to":{"anyOf":[{"type":"string"},{"type":"null"}]},"rate":{"type":"string"}},"required":["rate"],"type":"object"},"GrantAward":{"additionalProperties":false,"properties":{"recipient":{"enum":["subject","referrer"],"type":"string"},"amount":{"type":"string"},"bucket":{"type":"string"},"expiry":{"anyOf":[{"oneOf":[{"$ref":"#/$defs/NeverExpiry"},{"$ref":"#/$defs/AfterGrantExpiry"},{"$ref":"#/$defs/EndOfWindowExpiry"},{"$ref":"#/$defs/FixedExpiry"},{"$ref":"#/$defs/SubscriptionEndExpiry"}]},{"type":"null"}]}},"required":["amount","bucket"],"type":"object"},"GrantEligibility":{"additionalProperties":false,"properties":{"plans":{"items":{"type":"string"},"type":"array"},"regions":{"items":{"type":"string"},"type":"array"}},"type":"object"},"GrantProgram":{"additionalProperties":false,"properties":{"trigger":{"enum":["account_created","referral_completed","promo_code_redeemed","manual"],"type":"string"},"awards":{"items":{"$ref":"#/$defs/GrantAward"},"type":"array"},"availability":{"anyOf":[{"$ref":"#/$defs/Availability"},{"type":"null"}]},"eligibility":{"$ref":"#/$defs/GrantEligibility"},"max_awards_per_subject":{"type":"integer"},"idempotency_scope":{"enum":["subject","event"],"type":"string"}},"required":["trigger","awards"],"type":"object"},"InMatcher":{"additionalProperties":false,"properties":{"op":{"const":"in","type":"string"},"values":{"items":{"anyOf":[{"type":"string"},{"type":"number"},{"type":"string"},{"type":"boolean"}]},"type":"array"}},"required":["op","values"],"type":"object"},"IntegerFeature":{"additionalProperties":false,"properties":{"type":{"const":"integer","type":"string"},"default":{"type":"integer"},"minimum":{"anyOf":[{"type":"integer"},{"type":"null"}]},"maximum":{"anyOf":[{"type":"integer"},{"type":"null"}]}},"required":["type","default"],"type":"object"},"IntegerRange":{"additionalProperties":false,"properties":{"minimum":{"type":"integer"},"maximum":{"type":"integer"},"default":{"type":"integer"}},"required":["minimum","maximum","default"],"type":"object"},"MeasureDefinition":{"additionalProperties":false,"properties":{"unit":{"type":"string"}},"required":["unit"],"type":"object"},"NeverExpiry":{"additionalProperties":false,"properties":{"type":{"const":"never","type":"string"}},"required":["type"],"type":"object"},"NotInMatcher":{"additionalProperties":false,"properties":{"op":{"const":"not_in","type":"string"},"values":{"items":{"anyOf":[{"type":"string"},{"type":"number"},{"type":"string"},{"type":"boolean"}]},"type":"array"}},"required":["op","values"],"type":"object"},"OfferPrice":{"additionalProperties":false,"properties":{"amount_minor":{"type":"integer"},"currency":{"type":"string"},"tax_behavior":{"enum":["inclusive","exclusive","unspecified"],"type":"string"}},"required":["amount_minor","currency"],"type":"object"},"OperationAdmission":{"additionalProperties":false,"properties":{"max_in_flight":{"type":"integer"}},"required":["max_in_flight"],"type":"object"},"OperationDefinition":{"additionalProperties":false,"properties":{"measures":{"additionalProperties":{"$ref":"#/$defs/MeasureDefinition"},"type":"object"},"dimensions":{"additionalProperties":{"$ref":"#/$defs/DimensionDefinition"},"type":"object"}},"required":["measures"],"type":"object"},"OperationPricing":{"additionalProperties":false,"properties":{"rules":{"items":{"$ref":"#/$defs/PriceRule"},"type":"array"},"unmatched":{"oneOf":[{"$ref":"#/$defs/RejectUnmatched"},{"$ref":"#/$defs/ChargeUnmatched"}]}},"required":["unmatched"],"type":"object"},"PackageCharge":{"additionalProperties":false,"properties":{"type":{"const":"package","type":"string"},"measure":{"type":"string"},"units":{"type":"string"},"amount":{"type":"string"},"rounding":{"enum":["ceil","floor","nearest"],"type":"string"}},"required":["type","measure","units","amount"],"type":"object"},"PerUnitCharge":{"additionalProperties":false,"properties":{"type":{"const":"per_unit","type":"string"},"measure":{"type":"string"},"rate":{"type":"string"},"unit_size":{"type":"string"}},"required":["type","measure","rate"],"type":"object"},"PlanAssignmentWindow":{"additionalProperties":false,"properties":{"type":{"const":"plan_assignment","type":"string"},"interval":{"$ref":"#/$defs/BillingInterval"},"timezone":{"type":"string"}},"required":["type","interval"],"type":"object"},"PlanDefinition":{"additionalProperties":false,"properties":{"display_name":{"type":"string"},"rank":{"type":"integer"},"description":{"anyOf":[{"type":"string"},{"type":"null"}]},"rate_card":{"anyOf":[{"type":"string"},{"type":"null"}]},"allowed_operations":{"items":{"type":"string"},"type":"array"},"features":{"additionalProperties":{"anyOf":[{"type":"boolean"},{"type":"integer"},{"type":"string"}]},"type":"object"},"credit_allowance":{"anyOf":[{"$ref":"#/$defs/CreditAllowance"},{"type":"null"}]},"quotas":{"additionalProperties":{"$ref":"#/$defs/QuotaDefinition"},"type":"object"},"credit_policy":{"anyOf":[{"type":"string"},{"type":"null"}]},"admission_policy":{"anyOf":[{"type":"string"},{"type":"null"}]},"revision_policy":{"anyOf":[{"enum":["immediate","next_renewal","pinned"],"type":"string"},{"type":"null"}]}},"required":["display_name"],"type":"object"},"PrefixMatcher":{"additionalProperties":false,"properties":{"op":{"const":"prefix","type":"string"},"value":{"type":"string"}},"required":["op","value"],"type":"object"},"PrepaidCreditPolicy":{"additionalProperties":false,"properties":{"type":{"const":"prepaid","type":"string"}},"required":["type"],"type":"object"},"PriceRule":{"additionalProperties":false,"properties":{"when":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/EqualMatcher"},{"$ref":"#/$defs/InMatcher"},{"$ref":"#/$defs/NotInMatcher"},{"$ref":"#/$defs/PrefixMatcher"},{"$ref":"#/$defs/RangeMatcher"}]},"type":"object"},"charge":{"oneOf":[{"$ref":"#/$defs/FlatCharge"},{"$ref":"#/$defs/PerUnitCharge"},{"$ref":"#/$defs/PackageCharge"},{"$ref":"#/$defs/GraduatedCharge"},{"$ref":"#/$defs/VolumeCharge"},{"$ref":"#/$defs/ExpressionCharge"},{"$ref":"#/$defs/SumCharge"}]}},"required":["when","charge"],"type":"object"},"PricingConfig":{"additionalProperties":false,"properties":{"operations":{"additionalProperties":{"$ref":"#/$defs/OperationDefinition"},"type":"object"},"rate_cards":{"additionalProperties":{"$ref":"#/$defs/RateCard"},"type":"object"}},"required":["operations","rate_cards"],"type":"object"},"QuantityBounds":{"additionalProperties":false,"properties":{"minimum":{"type":"integer"},"maximum":{"type":"integer"},"default":{"type":"integer"}},"type":"object"},"QuotaDefinition":{"additionalProperties":false,"properties":{"operation":{"type":"string"},"measure":{"type":"string"},"limit":{"type":"string"},"window":{"oneOf":[{"$ref":"#/$defs/CalendarWindow"},{"$ref":"#/$defs/RollingWindow"},{"$ref":"#/$defs/PlanAssignmentWindow"}]},"enforcement":{"enum":["block","allow"],"type":"string"},"emit_at_percent":{"items":{"type":"integer"},"type":"array"}},"required":["operation","measure","limit","window","enforcement"],"type":"object"},"RangeMatcher":{"additionalProperties":false,"properties":{"op":{"const":"range","type":"string"},"gt":{"anyOf":[{"type":"string"},{"type":"null"}]},"gte":{"anyOf":[{"type":"string"},{"type":"null"}]},"lt":{"anyOf":[{"type":"string"},{"type":"null"}]},"lte":{"anyOf":[{"type":"string"},{"type":"null"}]}},"required":["op"],"type":"object"},"RateCard":{"additionalProperties":false,"properties":{"extends":{"anyOf":[{"type":"string"},{"type":"null"}]},"operations":{"additionalProperties":{"$ref":"#/$defs/OperationPricing"},"type":"object"}},"type":"object"},"RejectUnmatched":{"additionalProperties":false,"properties":{"action":{"const":"reject","type":"string"}},"required":["action"],"type":"object"},"RollingWindow":{"additionalProperties":false,"properties":{"type":{"const":"rolling","type":"string"},"duration":{"$ref":"#/$defs/Duration"}},"required":["type","duration"],"type":"object"},"StringFeature":{"additionalProperties":false,"properties":{"type":{"const":"string","type":"string"},"default":{"type":"string"},"pattern":{"anyOf":[{"type":"string"},{"type":"null"}]}},"required":["type","default"],"type":"object"},"StripePriceReference":{"additionalProperties":false,"properties":{"type":{"const":"stripe_price","type":"string"},"price_id":{"type":"string"}},"required":["type","price_id"],"type":"object"},"StripeProvider":{"additionalProperties":false,"properties":{"type":{"const":"stripe","type":"string"}},"required":["type"],"type":"object"},"SubscriptionChangePolicy":{"additionalProperties":false,"properties":{"effective":{"enum":["immediate","renewal"],"type":"string"},"proration":{"enum":["prorated","none"],"type":"string"},"payment_failure":{"enum":["prevent_change","apply_change"],"type":"string"}},"required":["effective","proration"],"type":"object"},"SubscriptionChanges":{"additionalProperties":false,"properties":{"upgrade":{"anyOf":[{"$ref":"#/$defs/SubscriptionChangePolicy"},{"type":"null"}]},"downgrade":{"anyOf":[{"$ref":"#/$defs/SubscriptionChangePolicy"},{"type":"null"}]},"lateral":{"anyOf":[{"$ref":"#/$defs/SubscriptionChangePolicy"},{"type":"null"}]},"cadence_change":{"anyOf":[{"$ref":"#/$defs/SubscriptionChangePolicy"},{"type":"null"}]}},"type":"object"},"SubscriptionEndExpiry":{"additionalProperties":false,"properties":{"type":{"const":"subscription_end","type":"string"}},"required":["type"],"type":"object"},"SubscriptionOffer":{"additionalProperties":false,"properties":{"display_name":{"type":"string"},"description":{"anyOf":[{"type":"string"},{"type":"null"}]},"sort_order":{"type":"integer"},"availability":{"anyOf":[{"$ref":"#/$defs/Availability"},{"type":"null"}]},"price":{"$ref":"#/$defs/OfferPrice"},"providers":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/StripePriceReference"},{"$ref":"#/$defs/DodoProductReference"},{"$ref":"#/$defs/CustomObjectReference"}]},"type":"object"},"type":{"const":"subscription","type":"string"},"plan":{"type":"string"},"billing_interval":{"$ref":"#/$defs/BillingInterval"},"trial":{"anyOf":[{"$ref":"#/$defs/BillingInterval"},{"type":"null"}]},"cycle_grant":{"anyOf":[{"$ref":"#/$defs/CycleGrant"},{"type":"null"}]}},"required":["display_name","price","providers","type","plan","billing_interval"],"type":"object"},"SumCharge":{"additionalProperties":false,"properties":{"type":{"const":"sum","type":"string"},"components":{"items":{"oneOf":[{"$ref":"#/$defs/FlatCharge"},{"$ref":"#/$defs/PerUnitCharge"},{"$ref":"#/$defs/PackageCharge"},{"$ref":"#/$defs/GraduatedCharge"},{"$ref":"#/$defs/VolumeCharge"},{"$ref":"#/$defs/ExpressionCharge"},{"$ref":"#/$defs/SumCharge"}]},"type":"array"}},"required":["type","components"],"type":"object"},"TopupOffer":{"additionalProperties":false,"properties":{"display_name":{"type":"string"},"description":{"anyOf":[{"type":"string"},{"type":"null"}]},"sort_order":{"type":"integer"},"availability":{"anyOf":[{"$ref":"#/$defs/Availability"},{"type":"null"}]},"price":{"$ref":"#/$defs/OfferPrice"},"providers":{"additionalProperties":{"oneOf":[{"$ref":"#/$defs/StripePriceReference"},{"$ref":"#/$defs/DodoProductReference"},{"$ref":"#/$defs/CustomObjectReference"}]},"type":"object"},"type":{"const":"topup","type":"string"},"credits_per_unit":{"type":"string"},"quantity":{"$ref":"#/$defs/QuantityBounds"},"bucket":{"type":"string"},"expiry":{"anyOf":[{"oneOf":[{"$ref":"#/$defs/NeverExpiry"},{"$ref":"#/$defs/AfterGrantExpiry"},{"$ref":"#/$defs/EndOfWindowExpiry"},{"$ref":"#/$defs/FixedExpiry"},{"$ref":"#/$defs/SubscriptionEndExpiry"}]},{"type":"null"}]},"lot_behavior":{"enum":["separate_lots","merge_and_refresh"],"type":"string"}},"required":["display_name","price","providers","type","credits_per_unit","bucket"],"type":"object"},"VolumeCharge":{"additionalProperties":false,"properties":{"type":{"const":"volume","type":"string"},"measure":{"type":"string"},"tiers":{"items":{"$ref":"#/$defs/GraduatedTier"},"type":"array"}},"required":["type","measure","tiers"],"type":"object"}},"additionalProperties":false,"properties":{"version":{"const":1,"type":"integer"},"catalog":{"$ref":"#/$defs/CatalogConfig"},"pricing":{"anyOf":[{"$ref":"#/$defs/PricingConfig"},{"type":"null"}]},"credits":{"$ref":"#/$defs/CreditsConfig"},"entitlements":{"$ref":"#/$defs/EntitlementsConfig"},"admission":{"$ref":"#/$defs/AdmissionConfig"},"plans":{"additionalProperties":{"$ref":"#/$defs/PlanDefinition"},"type":"object"},"commerce":{"$ref":"#/$defs/CommerceConfig"}},"required":["version","credits"],"type":"object"}$catalog_json$::jsonb
$function$;
-- END GENERATED CATALOG SHAPE SCHEMA

CREATE FUNCTION bursar.catalog_shape_error(
    p_value jsonb,
    p_schema jsonb,
    p_definitions jsonb,
    p_path text DEFAULT '$'
) RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SET search_path TO ''
AS $$
DECLARE
    v_schema jsonb := p_schema;
    v_reference text;
    v_expected_type text;
    v_candidate jsonb;
    v_error text;
    v_first_error text;
    v_key text;
    v_child jsonb;
    v_additional jsonb;
    v_required text;
    v_index bigint;
BEGIN
    IF jsonb_typeof(v_schema) = 'boolean' THEN
        RETURN CASE WHEN v_schema = 'true'::jsonb THEN NULL ELSE p_path || ' is not allowed' END;
    END IF;

    WHILE v_schema ? '$ref' LOOP
        v_reference := v_schema->>'$ref';
        IF v_reference !~ '^#/\$defs/[A-Za-z0-9_]+$' THEN
            RETURN p_path || ' uses an unsupported schema reference';
        END IF;
        v_schema := p_definitions->substring(v_reference FROM 9);
        IF v_schema IS NULL THEN
            RETURN p_path || ' uses an unknown schema reference';
        END IF;
    END LOOP;

    IF v_schema ? 'anyOf' OR v_schema ? 'oneOf' THEN
        FOR v_candidate IN
            SELECT value
            FROM jsonb_array_elements(COALESCE(v_schema->'anyOf', v_schema->'oneOf'))
        LOOP
            v_error := bursar.catalog_shape_error(
                p_value,
                v_candidate,
                p_definitions,
                p_path
            );
            IF v_error IS NULL THEN
                RETURN NULL;
            END IF;
            v_first_error := COALESCE(v_first_error, v_error);
        END LOOP;
        RETURN COALESCE(v_first_error, p_path || ' does not match an allowed shape');
    END IF;

    IF v_schema ? 'const' AND p_value IS DISTINCT FROM v_schema->'const' THEN
        RETURN p_path || ' has an unsupported value';
    END IF;

    IF v_schema ? 'enum'
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(v_schema->'enum') AS allowed(value)
           WHERE allowed.value = p_value
       )
    THEN
        RETURN p_path || ' has an unsupported value';
    END IF;

    v_expected_type := v_schema->>'type';
    IF v_expected_type IS NOT NULL
       AND NOT (
           CASE v_expected_type
               WHEN 'object' THEN jsonb_typeof(p_value) = 'object'
               WHEN 'array' THEN jsonb_typeof(p_value) = 'array'
               WHEN 'string' THEN jsonb_typeof(p_value) = 'string'
               WHEN 'boolean' THEN jsonb_typeof(p_value) = 'boolean'
               WHEN 'number' THEN jsonb_typeof(p_value) = 'number'
               WHEN 'integer' THEN jsonb_typeof(p_value) = 'number'
                   AND (p_value #>> '{}')::numeric = trunc((p_value #>> '{}')::numeric)
               WHEN 'null' THEN jsonb_typeof(p_value) = 'null'
               ELSE false
           END
       )
    THEN
        RETURN format('%s must be %s', p_path, v_expected_type);
    END IF;

    IF jsonb_typeof(p_value) = 'object' THEN
        FOR v_required IN
            SELECT value
            FROM jsonb_array_elements_text(COALESCE(v_schema->'required', '[]'::jsonb))
        LOOP
            IF NOT p_value ? v_required THEN
                RETURN format('%s.%s is required', p_path, v_required);
            END IF;
        END LOOP;

        FOR v_key, v_child IN SELECT key, value FROM jsonb_each(p_value)
        LOOP
            IF COALESCE(v_schema->'properties', '{}'::jsonb) ? v_key THEN
                v_child := v_schema->'properties'->v_key;
            ELSE
                v_additional := v_schema->'additionalProperties';
                IF v_additional = 'false'::jsonb THEN
                    RETURN format('%s.%s is not allowed', p_path, v_key);
                ELSIF jsonb_typeof(v_additional) = 'object' THEN
                    v_child := v_additional;
                ELSE
                    CONTINUE;
                END IF;
            END IF;

            v_error := bursar.catalog_shape_error(
                p_value->v_key,
                v_child,
                p_definitions,
                format('%s.%s', p_path, v_key)
            );
            IF v_error IS NOT NULL THEN
                RETURN v_error;
            END IF;
        END LOOP;
    ELSIF jsonb_typeof(p_value) = 'array' AND v_schema ? 'items' THEN
        v_index := 0;
        FOR v_child IN SELECT value FROM jsonb_array_elements(p_value)
        LOOP
            v_error := bursar.catalog_shape_error(
                v_child,
                v_schema->'items',
                p_definitions,
                format('%s[%s]', p_path, v_index)
            );
            IF v_error IS NOT NULL THEN
                RETURN v_error;
            END IF;
            v_index := v_index + 1;
        END LOOP;
    END IF;

    RETURN NULL;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN format('%s has an invalid %s value', p_path, v_expected_type);
END
$$;

CREATE FUNCTION bursar.require_catalog_document_shape(
    p_document jsonb
) RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SET search_path TO ''
AS $$
DECLARE
    v_schema jsonb := bursar.catalog_document_shape_schema();
    v_error text;
BEGIN
    v_error := bursar.catalog_shape_error(
        p_document,
        v_schema,
        v_schema->'$defs'
    );
    IF v_error IS NOT NULL THEN
        RAISE EXCEPTION 'invalid_catalog: %', v_error
            USING ERRCODE = '22023';
    END IF;
END
$$;

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
       AND jsonb_typeof(p_value) = 'object'
       AND octet_length(p_value::text) <= p_max_bytes
$$;

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
    IF p_policy IS NULL
       OR jsonb_typeof(p_policy) <> 'object'
       OR p_policy->>'type' <> 'rolling'
    THEN
        RETURN 0;
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

CREATE FUNCTION bursar.is_nonempty_text(
    p_value text
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_value = btrim(p_value)
       AND length(btrim(p_value)) BETWEEN 1 AND 255
$$;

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

-- Tenancy is part of the baseline data model. Trusted application code binds
-- one tenant to each transaction with:
--
--   SELECT set_config('bursar.tenant_id', '<uuid>', true)
--
-- Supabase/PostgREST requests may instead supply the tenant in trusted JWT
-- app_metadata. User-controlled metadata is deliberately ignored.
CREATE TABLE bursar.tenants (
    id uuid PRIMARY KEY DEFAULT bursar.uuid_v7(),
    slug text NOT NULL UNIQUE CHECK (
        bursar.is_bounded_text(slug, 100)
        AND slug ~ '^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$'
    ),
    display_name text,
    status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended', 'closed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        display_name IS NULL
        OR bursar.is_bounded_text(display_name, 255)
    )
);

CREATE FUNCTION bursar.current_tenant_id()
RETURNS uuid
LANGUAGE plpgsql
STABLE
SET search_path TO ''
AS $$
DECLARE
    v_explicit text;
    v_claims_text text;
    v_claims jsonb;
    v_tenant text;
BEGIN
    v_explicit := NULLIF(
        current_setting('bursar.tenant_id', true),
        ''
    );
    IF v_explicit IS NOT NULL THEN
        RETURN v_explicit::uuid;
    END IF;

    v_claims_text := NULLIF(
        current_setting('request.jwt.claims', true),
        ''
    );
    IF v_claims_text IS NULL THEN
        RETURN NULL;
    END IF;

    v_claims := v_claims_text::jsonb;
    v_tenant := COALESCE(
        v_claims #>> '{app_metadata,tenant_id}',
        v_claims #>> '{app_metadata,tenantId}'
    );
    RETURN NULLIF(v_tenant, '')::uuid;
EXCEPTION
    WHEN invalid_text_representation THEN
        RETURN NULL;
END
$$;

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

CREATE FUNCTION bursar.current_provider_environment()
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT CASE
        WHEN current_setting('bursar.provider_environment', true)
             IN ('live', 'test', 'sandbox')
        THEN current_setting('bursar.provider_environment', true)
        ELSE 'live'
    END
$$;
