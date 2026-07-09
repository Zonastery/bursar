# Bursar Managed Service — Architecture Overview

## What we're building

A multi-tenant managed service for [bursar](https://github.com/Zonastery/bursar) — the declarative credit calculation engine. Customers get a hosted bursar backend they call via SDK (no self-hosting Supabase required).

## Two services

### Service 1 — Control Plane / UI

- **Stack:** Next.js + Supabase Auth
- **Auth:** email/OAuth sessions (admins) + admin API keys (CI/CD)
- **Responsibilities:**
  - Edit/validate/publish/activate pricing config
  - Manage API keys (create, rotate, revoke)
  - Invite/manage team members
  - Configure webhook endpoints
  - View analytics dashboards
  - Tenant settings

### Service 2 — Data Plane / SDK-facing API

- **Stack:** TypeScript/Node (Fastify or Hono)
- **Auth:** Bearer API keys (`sk_live_…`, `sk_test_…`)
- **Responsibilities:**
  - Authenticate SDK calls via API key → resolve tenant + scopes
  - Serve the `ManagedBursarStore` operations (credits, leases, plans, etc.)
  - Recalculate costs server-side from raw `UsageMetrics` (source of truth)
  - Emit webhook events to customers
  - Rate-limit per key/tenant

## Architecture diagram

```
┌──────────────────────────────────────────────────────────────────┐
│  Customer's backend                                              │
│    CreditManager + ManagedBursarStore                             │
└───────────────┬──────────────────────────────────────────────────┘
                │ HTTPS + Bearer sk_live_<keyId>_<secret>
                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Service 2 — Data Plane API  (Node/TS)                          │
│  • Auth middleware: resolve key → tenant_id + scopes             │
│  • JS bursar SDK in-process                                      │
│  • Cost recalculation from raw metrics (authoritative)           │
│  • Webhook enqueue on events                                     │
└───────────────┬──────────────────────────────────────────────────┘
                │ tenant-scoped queries (service_role or tenant_id)
                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Supabase Postgres                                               │
│  • Bursar data tables (eventually with tenant_id columns)        │
│  • Control-plane tables (api_keys, webhooks, members, audit)     │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  Service 1 — Control Plane / UI  (Next.js + Supabase Auth)       │
│  • Admin humans: Supabase Auth JWT + membership check            │
│  • Edit/validate/publish pricing config, manage keys/members     │
│  • Optionally: admin API keys for CI/CD config publishing        │
└──────────────────────────────────────────────────────────────────┘
```

## Design documents

| File | Content |
|------|---------|
| `01-api-keys-authn-authz.md` | API key model, format, storage, scopes, auth flow, key lifecycle |
| `02-service-2-data-plane.md` | Full REST API surface for the SDK-facing service |
| `03-service-1-control-plane.md` | Full REST API surface for the admin UI service |
| `04-sdk-integration.md` | `ManagedBursarStore` — how the JS SDK connects to Service 2 |
| `05-control-plane-db.md` | Control-plane database schema and notes |
| `06-cross-cutting.md` | Idempotency, webhooks, error model, rate limiting, observability |

## Key decisions (confirmed)

| Decision | Choice |
|----------|--------|
| Multi-tenancy | Shared DB with `tenant_id` columns (design deferred until service interfaces are settled) |
| SDK call origin | Backend-only (secret keys); no publishable keys in v1 |
| Tech stack | **Both services TypeScript/Node** — JS bursar SDK in-process for Service 2 |
| Cost authority | **Service 2 recalculates** from raw `UsageMetrics` — never trusts a client-sent amount |
| Config publishing | Publish creates new version (not yet active) → activate separately for safe rollouts |
