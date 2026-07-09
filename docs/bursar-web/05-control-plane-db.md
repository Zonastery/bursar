# Control-Plane Database Schema

A separate set of tables in Supabase Postgres (same DB or same project as bursar's data tables). These manage the multi-tenant control plane: tenants, members, API keys, webhooks, and audit.

## Entity relationship

```
tenants
  │
  ├── tenant_members          (tenant_id → auth.users)
  ├── api_keys                (tenant_id)
  ├── webhook_endpoints       (tenant_id)
  ├── webhook_deliveries      (tenant_id → webhook_endpoints)
  └── audit_log               (tenant_id)
```

## Table definitions

### `tenants`

One row per managed-service customer. Created by super-admin when provisioning.

```sql
CREATE TABLE tenants (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text NOT NULL,
  slug         text NOT NULL UNIQUE,            -- URL-safe, e.g. "acme-corp"
  environment  text NOT NULL DEFAULT 'live' CHECK (environment IN ('live', 'test', 'sandbox')),
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleting')),
  settings     jsonb NOT NULL DEFAULT '{}',     -- rate_limit_rps, webhook_retry_max, etc.
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
```

Settings schema (defaults):
```json
{
  "rate_limit_rps": 100,
  "webhook_retry_max": 5,
  "webhook_retry_interval_seconds": 60,
  "pricing_cache_ttl_seconds": 300
}
```

### `tenant_members`

Who can access Service 1 (the admin UI). Links Supabase Auth users to tenants.

```sql
CREATE TABLE tenant_members (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role       text NOT NULL DEFAULT 'developer' CHECK (role IN ('owner', 'admin', 'developer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id)
);
```

Roles:
- **owner** — full access, can manage members, delete tenant
- **admin** — can manage API keys, webhooks, publish config
- **developer** — can view config, view analytics, view keys (not create/revoke)

### `api_keys`

Secret and admin API keys. Only hashes stored; full keys shown once at creation.

```sql
CREATE TABLE api_keys (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name          text NOT NULL,
  key_id        text NOT NULL UNIQUE,               -- routing id (12 chars)
  key_hash      text NOT NULL,                      -- sha256 of full key
  key_prefix    text NOT NULL,                      -- "sk_live_a1b2..." for display
  environment   text NOT NULL CHECK (environment IN ('live', 'test')),
  key_type      text NOT NULL DEFAULT 'sdk' CHECK (key_type IN ('sdk', 'admin')),
  scopes        text[] NOT NULL DEFAULT '{}',
  allowed_ips   inet[] DEFAULT NULL,
  last_used_at  timestamptz DEFAULT NULL,
  expires_at    timestamptz DEFAULT NULL,
  revoked_at    timestamptz DEFAULT NULL,
  created_by    uuid NOT NULL REFERENCES tenant_members(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_api_keys_key_id ON api_keys (key_id);
CREATE INDEX idx_api_keys_tenant_active ON api_keys (tenant_id) WHERE revoked_at IS NULL;
```

Key types:
- **`sdk`** — used on Service 2 (data plane). Scopes from the SDK scope catalog.
- **`admin`** — used on Service 1 (control plane). Scopes: `config:write`, `config:read`, `members:read`. Not usable on Service 2.

### `webhook_endpoints`

Per-tenant webhook configuration. Hooks into bursar events → delivery to customer URL.

```sql
CREATE TABLE webhook_endpoints (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  url        text NOT NULL,
  secret     text NOT NULL,                         -- HMAC signing secret (hashed for display)
  events     text[] NOT NULL DEFAULT '{}',          -- e.g. {"credits.low_balance", "credits.cap_reached"}
  status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'deleted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, url)
);
```

Supported events (mapped from bursar's `CreditEventEmitter` event types):
- `credits.deducted` — high volume, opt-in
- `credits.low_balance` — triggered when balance drops below threshold
- `credits.cap_reached` — spend cap hit
- `credits.expired` — credits swept/expired
- `feature_limit.reached` — entitlement max calls hit
- `subscription.cycle_granted` — subscription allowance/credits granted
- `lease.expired` — lease automatically expired (abandoned hold)

### `webhook_deliveries`

Delivery log. Inserted by Service 2 when an event fires. A background worker picks up pending deliveries and POSTs them.

```sql
CREATE TYPE delivery_status AS ENUM ('pending', 'delivering', 'delivered', 'failed', 'exhausted');

CREATE TABLE webhook_deliveries (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  endpoint_id       uuid NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
  event_type        text NOT NULL,
  event_id          text NOT NULL,            -- idempotency on the customer side
  payload           jsonb NOT NULL,
  status            delivery_status NOT NULL DEFAULT 'pending',
  attempt_count     integer NOT NULL DEFAULT 0,
  max_attempts      integer NOT NULL DEFAULT 5,
  last_response_code integer DEFAULT NULL,
  last_response_body text DEFAULT NULL,
  next_attempt_at   timestamptz DEFAULT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz DEFAULT NULL
);

CREATE INDEX idx_webhook_deliveries_pending ON webhook_deliveries (tenant_id, next_attempt_at)
  WHERE status = 'pending' AND next_attempt_at <= now();

CREATE INDEX idx_webhook_deliveries_event_id ON webhook_deliveries (event_id);
```

Retry strategy: exponential backoff from `webhook_retry_interval_seconds` (default 60s). After `max_attempts`, status → `exhausted`.

### `audit_log`

Immutable log of all control-plane mutations.

```sql
CREATE TABLE audit_log (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  actor_id   uuid,                                 -- tenant_member id who performed the action (null = system)
  actor_type text NOT NULL CHECK (actor_type IN ('member', 'admin_key', 'system')),
  action     text NOT NULL,                        -- "api_key.created", "config.published", "member.invited"
  target     jsonb NOT NULL DEFAULT '{}',          -- what was affected: { "key_id": "...", "version": 4 }
  metadata   jsonb DEFAULT '{}',                   -- user agent, IP, request ID
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_tenant ON audit_log (tenant_id, created_at DESC);
```

Actions logged: every config publish/activate, key create/rotate/revoke, member invite/role-change/remove, webhook create/update/delete, tenant settings change.

## Notes on schema management

- Control-plane tables are migrated via the **same migration system** as bursar's data tables (or a separate `control_xxx` migration series).
- All tables use `ENABLE ROW LEVEL SECURITY` with `USING (false)` — accessed only via `SECURITY DEFINER` RPCs or backend service_role. The API layer (Service 1 and Service 2) is the only way to query these tables.
- The `tenant_id` column on these tables serves as the tenancy boundary for the control plane. The Service 1 membership check (`tenant_members`) ensures an admin user can only see/manage their own tenant's resources.
