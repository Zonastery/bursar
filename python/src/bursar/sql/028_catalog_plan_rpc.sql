-- Resolve the catalog plan referenced by a provider offer.
CREATE FUNCTION bursar.resolve_catalog_plan(
    p_provider text,
    p_lookup_type text,
    p_lookup_value text
)
RETURNS bursar.catalog_plans
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT p.*
    FROM bursar.catalog_provider_refs r
    JOIN bursar.catalog_revisions cr
      ON cr.id = r.catalog_revision_id
     AND cr.status = 'active'
    JOIN bursar.catalog_offers o
      ON o.catalog_revision_id = r.catalog_revision_id
     AND o.offer_key = r.object_key
    JOIN bursar.catalog_plans p
      ON p.catalog_revision_id = o.catalog_revision_id
     AND p.plan_key = o.plan_key
    WHERE r.provider = p_provider
      AND r.lookup_type = p_lookup_type
      AND r.lookup_value = p_lookup_value
      AND r.object_type = 'offer'
    LIMIT 1
$$;
