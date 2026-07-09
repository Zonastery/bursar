# API Key Model & Authn/Authz

## 1. Key types & format

| Type | Prefix | Held by | v1? |
|------|--------|---------|-----|
| Secret key — live | `sk_live_` | Customer backend | ✅ |
| Secret key — test | `sk_test_` | Customer backend (sandbox) | ✅ |
| Publishable key | `pk_live_` | Client app (browser/mobile) | ❌ future |

**Format:** `sk_live_<keyId>_<secret>`

- `keyId`: 12 URL-safe chars — fast routing/lookup (indexed).
- `secret`: 32 URL-safe chars — verified by hash; high entropy protects against guessing.
- **Never stored in full.** Only the hash (SHA-256) is persisted.

## 2. Key lifecycle

### Creation

1. Admin creates key via Service 1 UI (or admin API).
2. Service generates `crypto.randomBytes` for keyId + secret.
3. Hashes full key: `key_hash = sha256("sk_live_<keyId>_<secret>")`.
4. Stores `(key_id, key_hash, key_prefix, tenant_id, scopes, environment, name, created_by)` in `api_keys` table.
5. Returns full key to admin **exactly once** — after this, the service can never reconstruct it.
6. Admin must store it securely (env vars, vault, etc.).

### Rotation

`POST /v1/api-keys/:id/rotate` — one-shot:
1. Sets `revoked_at = now()` on old key.
2. Generates a new key (same scopes, environment, name).
3. Returns new full key (shown once).
4. Optionally supports a **grace period** where both keys are valid for N hours (configurable per-tenant).

### Revocation

`DELETE /v1/api-keys/:id` — sets `revoked_at = now()`. In-flight requests within the same second may complete. Immediate for all subsequent requests.

### Expiry

Optional `expires_at` on the key row. Middleware rejects with 401 if `now() > expires_at`.

## 3. Storage schema (`api_keys` table)

```sql
CREATE TABLE api_keys (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  name         text NOT NULL,                     -- "Production server"
  key_id       text NOT NULL UNIQUE,              -- the routing id
  key_hash     text NOT NULL,                     -- sha256 of full key
  key_prefix   text NOT NULL,                     -- "sk_live_a1b2..." for display
  environment  text NOT NULL CHECK (environment IN ('live', 'test')),
  scopes       text[] NOT NULL DEFAULT '{}',      -- granted scopes
  allowed_ips  inet[] DEFAULT NULL,               -- optional IP allowlist
  last_used_at timestamptz DEFAULT NULL,
  expires_at   timestamptz DEFAULT NULL,
  revoked_at   timestamptz DEFAULT NULL,
  created_by   uuid NOT NULL REFERENCES tenant_members(user_id),
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_api_keys_key_id ON api_keys (key_id);
CREATE INDEX idx_api_keys_tenant ON api_keys (tenant_id, revoked_at);
```

## 4. Scopes & roles

### Scope catalog

Every route declares a required scope. Middleware verifies `key.scopes ⊇ required`.

| Scope | Routes covered |
|-------|----------------|
| `credits:read` | balance, available, buckets, can-afford, check-allowance, transactions, usage-events, analytics |
| `credits:write` | add, deduct, deduct-fixed, sweep |
| `leases:write` | reserve, settle, release, renew |
| `plans:read` | get-user-plan, check-feature, check-feature-limit |
| `plans:write` | set/unset plan |
| `refunds:write` | refund |
| `subscriptions:write` | grant-cycle |
| `teams:write` | create team, members, deduct-team |
| `spendcaps:write` | set/remove spend caps |
| `config:read` | read active pricing + version history |

### Preset roles

- **`full`** — all scopes above (default for a customer's main backend key). Note: *not* `config:write` or any admin scopes.
- **`restricted`** — admin selects a custom subset at key creation time.

`config:write` (publish/activate pricing) is explicitly **never** on an SDK key — it's UI/admin-only (Service 1).

## 5. Authentication flow (Service 2 middleware)

```
Request: POST /v1/users/u1/credits/deduct
  Authorization: Bearer sk_live_<keyId>_<secret>
```

1. **Extract bearer token** from `Authorization` header.
2. **Parse keyId** from the token (split on `_`: prefix, env, keyId, secret).
3. **Lookup key** by `key_id`:
   `SELECT * FROM api_keys WHERE key_id = $1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())`
4. **Verify hash:**
   `if (sha256(full_token) !== row.key_hash) → 401 "invalid api key"`
5. **Optional IP allowlist check:**
   `if (row.allowed_ips && !row.allowed_ips.includes(request.ip)) → 403 "ip not allowed"`
6. **Rate-limit check:** per-key token bucket (see cross-cutting doc).
7. **Update last_used_at:** debounced (≤1 write/minute per key to avoid hot-row contention).
8. **Inject context:**
   `req.ctx = { tenantId: row.tenant_id, keyId: row.key_id, environment: row.environment, scopes: row.scopes }`
9. **Pass to route handler** → required scope check → tenant-scoped bursar call.

### Performance notes

- `key_id` is a unique text index — lookup is O(1).
- Hash verification is O(1) and cache-friendly.
- `last_used_at` updates use a **throttled background queue** (in-memory debounce per key) rather than synchronous writes on the hot path.
- No DB roundtrips per request beyond the key lookup.

## 6. Authorization enforcement model

### Tenant isolation

- `tenantId` comes **only** from the authenticated API key — never from the request body/params. The customer's backend asserts `user_id`, but cannot forge `tenantId`.
- Every bursar call is scoped by `tenantId` (today: selects the tenant's store; later: adds `WHERE tenant_id = $1` to every query).
- This is a single enforced chokepoint — impossible for a tenant to access another tenant's data.

### Scope enforcement

- Each route handler (or route-level middleware) declares `requiredScopes: string[]`.
- If `req.ctx.scopes` does not contain every required scope → `403 { error: { type: "forbidden", message: "missing scope: credits:write" } }`.

### Execution isolation

- The tenant's bursar data tables are **not directly accessible** via the API. Every operation passes through bursar's RPCs/store methods which enforce the tenant's pricing, balance floors, allowances, and spend caps. The API is just a thin translation layer.

## 7. End-user identity

- Since all SDK calls come from the customer's **backend** (secret key held server-side), we trust the customer to assert their own `user_id`.
- The customer passes `user_id` in the request path (e.g. `/v1/users/{userId}/balance`).
- With `tenant_id`-scoped queries, `user_id` only needs to be unique *within* a tenant, not globally.

**Future (publishable keys):** For client-side SDK calls, the customer's backend will issue a signed JWT asserting the end-user identity, and the key will use a restricted scope that requires this JWT.

## 8. Admin API keys (Service 1)

For CI/CD pipelines that need to publish/activate pricing config without a human browser session:

- Separate key type: `sk_admin_<keyId>_<secret>`.
- Stored in same `api_keys` table with an `admin = true` flag.
- Scopes: `config:write`, `config:read`, optionally `members:read`.
- **Never** usable on Service 2 — Service 1 routes only.
- Admin middleware: same lookup/hash flow, but gated to `api_keys.admin = true`.

## 9. Audit trail

Every key-related event is logged to `audit_log`:

| Event | Fields logged |
|-------|---------------|
| Key created | tenant_id, created_by, key_id (not hash!), scopes, name, environment |
| Key rotated | tenant_id, created_by, old_key_id, new_key_id |
| Key revoked | tenant_id, revoked_by, key_id |
| Key expired (auto) | tenant_id, key_id (logged at auth check rejection time) |
