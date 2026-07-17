-- Durable checkout coordination and a database-level subscription guard.
-- A user may have one current subscription per provider; provider changes are
-- reconciled explicitly by deactivate_other_provider_subscriptions().

CREATE TABLE IF NOT EXISTS bursar.billing_checkout_intents (
    id uuid DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
    actor_key text NOT NULL,
    provider text NOT NULL,
    type text NOT NULL,
    product_id text NOT NULL,
    request_fingerprint text NOT NULL,
    status text NOT NULL DEFAULT 'open',
    provider_session_id text,
    checkout_url text,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_checkout_intents_type CHECK (type IN ('subscription', 'credit_pack')),
    CONSTRAINT billing_checkout_intents_status CHECK (status IN ('open', 'completed', 'failed', 'expired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_checkout_intents_open_actor
    ON bursar.billing_checkout_intents(actor_key)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_billing_checkout_intents_expiry
    ON bursar.billing_checkout_intents(expires_at);

CREATE TABLE IF NOT EXISTS bursar.billing_subscription_conflicts (
    id uuid DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
    user_id uuid,
    provider text NOT NULL,
    duplicate_subscription_id text NOT NULL,
    existing_subscription_id text,
    event_id text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'open',
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolution text,
    CONSTRAINT billing_subscription_conflicts_status CHECK (status IN ('open', 'resolved'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_subscription_conflicts_duplicate
    ON bursar.billing_subscription_conflicts(provider, duplicate_subscription_id);

-- This is intentionally provider-scoped: Bursar supports an explicit provider
-- migration path, but two current subscriptions from the same provider are
-- never a valid entitlement state. Run `bursar audit-subscriptions` and repair
-- any historical duplicates before applying this migration.
CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_subscriptions_one_current_per_provider
    ON bursar.billing_subscriptions(user_id, provider)
    WHERE status IN ('active', 'trialing', 'past_due', 'incomplete');
