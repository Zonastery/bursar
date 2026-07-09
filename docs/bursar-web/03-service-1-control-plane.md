# Service 1 — Control Plane / UI API

Serves the admin UI (Next.js) and optionally accepts **admin API keys** (`sk_admin_…`) for CI/CD pipelines. Human auth via **Supabase Auth JWT** (email/OAuth) + `tenant_members` check.

Two auth paths:

| Path | Auth | Used for |
|------|------|----------|
| Browser → Next.js → API | Supabase Auth session JWT + membership | Admin humans in UI |
| Direct API calls | `Authorization: Bearer sk_admin_…` | CI/CD config publishing |

## Pricing config editing & publishing

The core value of Service 1. Leverages bursar's immutable versioned config model. Two-step process for safe rollouts:

1. **Publish** — validates config, creates a new immutable version (not yet active)
2. **Activate** — atomically switches the tenant to that version (instant rollback by activating a prior version)

### `GET /v1/config/schema`

Returns the JSON Schema for the pricing config (for form validation / editor UX).

**Auth:** session or admin key

Response:
```json
{ "data": { "$schema": "https://json-schema.org/draft-07/schema#", "type": "object", ... } }
```

### `GET /v1/config/active`

Returns the currently active pricing config for the tenant (the starting point for drafting edits).

**Auth:** session or admin key

Response:
```json
{ "data": { "version": 3, "config": { "version": 1, "metering": {...}, "plans": {...}, ... }, "label": "deploy-42", "created_at": "..." } }
```

### `POST /v1/config/validate`

Validates a pricing config draft without persisting it. Returns errors and optionally a sample cost preview.

**Auth:** session or admin key

Body:
```json
{
  "config": { "version": 1, "metering": { "models": { "*": "input_tokens * 0.01" } }, ... },
  "sample_metrics": { "model": "gpt-4", "input_tokens": 500, "output_tokens": 200 }
}
```

Response:
```json
{ "data": { "valid": true, "errors": [], "warnings": ["plan 'pro' has no allowance"], "sample_cost": { "total": "11.0000", "breakdown": { "model_cost": "11.0000" } } } }
```

On invalid:
```json
{ "error": { "type": "validation_error", "message": "missing required field", "code": "VALIDATION_ERROR", "details": [
  { "path": "$.metering.models", "message": "must have at least 1 entry" }
]}}
```

### `POST /v1/config/publish`

Creates a new immutable pricing version for the tenant. **The config is NOT active yet.**

**Auth:** session or admin key

Body:
```json
{ "config": { "version": 1, "metering": { "models": { "*": "input_tokens * 0.01" } } }, "label": "deploy-43" }
```

Response:
```json
{ "data": { "version": 4, "label": "deploy-43", "created_at": "2026-07-09T10:00:00Z", "active": false } }
```

### `GET /v1/config/versions`

List all pricing versions for the tenant, with active marker.

**Auth:** session or admin key

Response:
```json
{ "data": { "versions": [
  { "version": 4, "label": "deploy-43", "active": false, "created_at": "..." },
  { "version": 3, "label": "deploy-42", "active": true, "created_at": "..." },
  { "version": 2, "label": "rollback-target", "active": false, "created_at": "..." },
  { "version": 1, "label": null, "active": false, "created_at": "..." }
]}}
```

### `GET /v1/config/versions/:version`

Export a specific version's full config.

**Auth:** session or admin key

Response:
```json
{ "data": { "version": 2, "config": { ... }, "label": "rollback-target", "created_at": "..." } }
```

### `POST /v1/config/versions/:version/activate`

Atomically switch the tenant's active pricing to this version. Enables instant rollback (just activate a prior version).

**Auth:** session or admin key

Body: (empty)

Response:
```json
{ "data": { "version": 2, "label": "rollback-target", "active": true, "activated_at": "2026-07-09T10:01:00Z" } }
```

### `GET /v1/config/diff`

Unified diff between two versions.

**Auth:** session or admin key

Query: `?a=2&b=4`

Response:
```json
{ "data": { "a": 2, "b": 4, "diff": "@@ -1,5 +1,6 @@\n metering:\n-  models: { \"*\": ... } \n+  models: { \"*\": ..., \"gpt-4\": ... }" } }
```

## API key management

### `GET /v1/api-keys`

List all API keys for the tenant. **Never returns the actual secret.**

**Auth:** session (admin+) or admin key

Response:
```json
{ "data": { "keys": [
  { "id": "key_uuid", "name": "Production server", "key_prefix": "sk_live_a1b2...", "environment": "live", "scopes": ["credits:read", "credits:write", ...], "created_at": "...", "last_used_at": null, "expires_at": null, "revoked_at": null },
  { "id": "key_uuid2", "name": "Staging CI", "key_prefix": "sk_test_c3d4...", "environment": "test", "scopes": ["full"], "created_at": "...", "last_used_at": "...", "revoked_at": "..." }
]}}
```

### `POST /v1/api-keys`

Create a new API key.

**Auth:** session (admin+) or admin key

Body:
```json
{
  "name": "Production server v2",
  "environment": "live",
  "scopes": ["credits:read", "credits:write", "leases:write", "plans:write", "teams:write", "config:read"],
  "expires_at": null
}
```

Response: **(includes the full secret — shown once)**
```json
{ "data": { "id": "key_uuid", "full_key": "sk_live_abc123def456_xyz789...", "key_prefix": "sk_live_abc1...", "name": "Production server v2", "environment": "live", "scopes": [...], "created_at": "..." } }
```

### `POST /v1/api-keys/:id/rotate`

Revoke the current key and create a new one with identical settings. Grace period configurable.

**Auth:** session (admin+) or admin key

Body:
```json
{ "grace_period_seconds": 300 }
```

Response:
```json
{ "data": { "old_key_id": "key_uuid", "old_revoked_at": "...", "new_key": { "id": "new_key_uuid", "full_key": "sk_live_...", ... } } }
```

During grace period, both old and new keys are valid. After grace period, old key is rejected.

### `DELETE /v1/api-keys/:id`

Revoke immediately.

**Auth:** session (admin+) or admin key

Response: `{ "data": { "id": "key_uuid", "revoked_at": "..." } }`

## Member management

### `GET /v1/members`

List tenant members.

**Auth:** session (developer+)

Response:
```json
{ "data": { "members": [
  { "id": "mem_uuid", "email": "alice@example.com", "role": "owner", "joined_at": "..." },
  { "id": "mem_uuid2", "email": "bob@example.com", "role": "admin", "joined_at": "..." }
]}}
```

### `POST /v1/members`

Invite a member.

**Auth:** session (owner)

Body:
```json
{ "email": "carol@example.com", "role": "developer" }
```

Response: `{ "data": { "id": "mem_uuid3", "email": "carol@example.com", "role": "developer", "status": "pending" } }`

### `PATCH /v1/members/:memberId`

Change role.

**Auth:** session (owner)

Body:
```json
{ "role": "admin" }
```

Response: `{ "data": { "id": "mem_uuid3", "role": "admin" } }`

### `DELETE /v1/members/:memberId`

Remove a member.

**Auth:** session (owner)

Response: `{ "data": { "removed": true } }`

## Webhook configuration

### `GET /v1/webhooks`

List webhook endpoints.

**Auth:** session (admin+) or admin key

Response:
```json
{ "data": { "endpoints": [
  { "id": "wh_uuid", "url": "https://example.com/webhook", "events": ["credits.low_balance", "credits.cap_reached"], "status": "active", "created_at": "..." }
]}}
```

### `POST /v1/webhooks`

Add a webhook endpoint.

**Auth:** session (admin+) or admin key

Body:
```json
{
  "url": "https://example.com/webhook",
  "events": ["credits.low_balance", "credits.cap_reached", "credits.expired"],
  "secret": null
}
```

If `secret` is null, one is auto-generated. The response includes the secret **once**.

Response:
```json
{ "data": { "id": "wh_uuid", "url": "...", "events": [...], "secret": "whsec_...", "status": "active", "created_at": "..." } }
```

### `PATCH /v1/webhooks/:id`

Update endpoint.

**Auth:** session (admin+) or admin key

Body: (any subset of url, events, secret)

### `DELETE /v1/webhooks/:id`

Remove endpoint. No more deliveries sent.

**Auth:** session (admin+) or admin key

### `POST /v1/webhooks/:id/test`

Send a test event to verify the endpoint.

**Auth:** session (admin+) or admin key

Response:
```json
{ "data": { "delivery_id": "del_uuid", "status": "delivered", "response_code": 200 } }
```

## Tenant settings

### `GET /v1/settings`

**Auth:** session (admin+)

Response:
```json
{ "data": { "tenant_id": "tnt_abc", "name": "Acme Corp", "slug": "acme", "environment": "live", "created_at": "...", "settings": { "rate_limit_rps": 100, "webhook_retry_max": 5, "webhook_retry_interval_seconds": 60 } } }
```

### `PATCH /v1/settings`

**Auth:** session (owner)

Body:
```json
{ "settings": { "rate_limit_rps": 200 } }
```

## Platform admin endpoints (super-admin only)

These are for the managed service operator, not for customers. Not detailed here but include:

- `POST /v1/tenants` — provision a new tenant
- `GET /v1/tenants` — list all tenants
- `GET /v1/tenants/:id` — inspect tenant
- `PATCH /v1/tenants/:id` — update tenant (suspend, change plan, etc.)
- `DELETE /v1/tenants/:id` — delete/suspend tenant
