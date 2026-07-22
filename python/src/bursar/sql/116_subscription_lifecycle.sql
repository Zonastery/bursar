-- Durable, provider-neutral state for customer initiated subscription changes.
ALTER TABLE bursar.billing_subscriptions
  ADD COLUMN IF NOT EXISTS grace_ends_at timestamptz;

CREATE TABLE IF NOT EXISTS bursar.billing_subscription_changes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL,
  provider text NOT NULL, provider_subscription_id text NOT NULL,
  from_plan text, from_interval text, to_plan text NOT NULL, to_interval text NOT NULL,
  effective_at text NOT NULL CHECK (effective_at IN ('immediately', 'next_billing_date', 'trial_end')),
  state text NOT NULL CHECK (state IN ('draft', 'awaiting_payment', 'scheduled', 'completed', 'failed', 'canceled', 'superseded')),
  proration_billing_mode text NOT NULL, quote jsonb NOT NULL DEFAULT '{}'::jsonb, quote_hash text NOT NULL,
  provider_operation_id text, provider_payment_id text, failure_code text, failure_message text,
  effective_date timestamptz, expires_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS billing_subscription_changes_one_open_idx ON bursar.billing_subscription_changes (provider, provider_subscription_id) WHERE state IN ('awaiting_payment', 'scheduled');
CREATE INDEX IF NOT EXISTS billing_subscription_changes_user_idx ON bursar.billing_subscription_changes (user_id, created_at DESC);
ALTER TABLE bursar.billing_subscription_changes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Server-only subscription changes" ON bursar.billing_subscription_changes;
CREATE POLICY "Server-only subscription changes" ON bursar.billing_subscription_changes USING (false);
REVOKE ALL ON TABLE bursar.billing_subscription_changes FROM anon, authenticated;
