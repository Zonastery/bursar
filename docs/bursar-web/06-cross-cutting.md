# Cross-Cutting Concerns

## 1. Idempotency

Bursar's core mutation RPCs already support idempotency keys (`deduct_with_allowance`, `credits_add`, `refund_credits`, `grant_subscription_cycle`, etc.). The managed service adds an **HTTP-level idempotency layer** for defense in depth.

### Protocol

- **Header:** `Idempotency-Key: <unique-string>` on all `POST`/`PUT`/`DELETE` routes.
- **Scope:** per API key. Same key from different customers (different API keys) are independent — no collision risk.
- **Storage:** in-memory cache (Redis or in-process LRU) keyed by `sha256(apiKeyId + ":" + idempotencyKey)`, TTL 24 hours.
- **Behavior:**
  1. First request with a given key: execute, store response (cached), return.
  2. Subsequent requests with same key within TTL: return cached response (same status + body). Do not re-execute.
  3. After TTL expiry: execute again (idempotency falls back to bursar's RPC-level dedup, which is permanent).
- **Pass-through:** the `Idempotency-Key` header value is forwarded to bursar's `idempotency_key` parameter on the relevant store calls, so bursar's own dedup also applies.

### Why two layers?

- HTTP layer catches retries within 24h (network timeouts, client library retries).
- Bursar layer catches retries beyond 24h (e.g. a webhook replayed after a day) — permanent per `(user_id, idempotency_key)` unique index in `credit_transactions`.

### Customer guidance

The customer should generate an idempotency key per unique operation (e.g., a UUID per deduction). Retries with the same key are safe. Different operations must use different keys.

```ts
// Safe retry:
const result = await manager.deduct("user_abc", metrics, {
  idempotencyKey: `deduct_${sessionId}_${timestamp}`
});
```

## 2. Webhooks

Bursar's `CreditEventEmitter` is in-process (not durable). The managed service bridges events to customer-configured webhook URLs.

### Flow

```
1. Service 2's CreditManager operation completes.
2. Attached CreditEventEmitter fires (e.g., "credits.low_balance").
3. The emitter's listener constructs a webhook payload and INSERTs a row into
   `webhook_deliveries` for each matching endpoint of that tenant.
4. A background worker (polling or push queue) reads pending deliveries,
   POSTs to the customer URL with HMAC signature.
5. On success (2xx): mark `delivered`, set `completed_at`.
6. On failure: increment `attempt_count`, set `next_attempt_at` with backoff.
7. After `max_attempts` (default 5): mark `exhausted`, stop retrying.
```

### Signature format

```
Header: X-Bursar-Signature: t=<unix_timestamp>,v1=<hmac_hex>

hmac = HMAC-SHA256(secret, `${timestamp}.${payload}`)
```

The customer verifies:
1. Parse `t` and `v1` from header.
2. Optionally check `t` is within 5 minutes (replay protection).
3. Compute expected HMAC with their webhook secret.
4. Compare (constant-time) with `v1`.
5. Process the payload: `JSON.parse(body)`.

### Payload format

```json
{
  "type": "credits.low_balance",
  "id": "evt_abc123",
  "created_at": "2026-07-09T10:00:00Z",
  "data": {
    "user_id": "user_xyz",
    "balance": "100.0000",
    "threshold": "500.0000",
    "environment": "live"
  }
}
```

### Retry schedule

| Attempt | Delay after previous |
|---------|---------------------|
| 1 | 60s |
| 2 | 5 min |
| 3 | 30 min |
| 4 | 2 hours |
| 5 | 6 hours |

Configurable per tenant in `tenants.settings.webhook_retry_max` and `webhook_retry_interval_seconds`.

## 3. Error model & envelopes

### Consistent response shape

**Success (2xx):**
```json
{ "data": { ... } }
```

**Client error (4xx):**
```json
{ "error": { "type": "insufficient_credits", "message": "Insufficient credits for user u1", "code": "INSUFFICIENT_CREDITS", "details": { "balance": "10.0000", "required": "21.0000" } } }
```

**Server error (5xx):**
```json
{ "error": { "type": "internal_error", "message": "An unexpected error occurred", "code": "INTERNAL_ERROR", "request_id": "req_xyz" } }
```

### HTTP status code mapping

| Condition | HTTP | `error.type` |
|-----------|------|-------------|
| Invalid/missing API key | 401 | `unauthorized` |
| Key scope insufficient | 403 | `forbidden` |
| IP not in allowlist | 403 | `forbidden` |
| Insufficient credits | 402 | `insufficient_credits` |
| Feature not entitled | 403 | `feature_not_entitled` |
| Lease not found | 404 | `lease_not_found` |
| User not found | 404 | `user_not_found` |
| Lease expired | 409 | `lease_expired` |
| Pricing not loaded | 409 | `pricing_not_loaded` |
| Concurrent lease limit | 429 | `concurrency_limit` |
| Rate limited | 429 | `rate_limited` |
| Validation error | 400 | `validation_error` |
| Idempotency replay mismatch | 422 | `idempotency_conflict` |
| Unknown/other | 500 | `internal_error` |

### Request ID

Every response carries a `X-Request-Id` header (UUID). Logged server-side for debugging. The customer can include it when reporting issues.

## 4. Rate limiting

### Per-key rate limit

- Algorithm: **token bucket** (or sliding window) per API key.
- Default: 100 requests/second, burst up to 200.
- Configurable per tenant in `tenants.settings.rate_limit_rps`.
- Applies **before** the key is looked up? No — must authenticate first. Flow: authenticate → resolve tenant settings → rate-limit check.
- Response on limit hit: `429 { error: { type: "rate_limited", message: "Too many requests", retry_after: 1 } }` with `Retry-After: 1` header.

### Per-tenant concurrency cap

- Max concurrent requests across all API keys for a tenant: default 500.
- Tracked with an in-memory counter (or Redis for multi-instance).
- Useful for preventing a single noisy tenant from starving others.
- Response: same 429 as above, with `code: "tenant_concurrency_limit"`.

### Endpoint-specific limits (future)

- Analytics queries are more expensive. May need lower limits on `/v1/analytics/*`.
- Suggest deferring to v2 when metrics exist.

## 5. Observability

### Structured logging

Every request logs:
```
tenantId=<uuid> keyId=<key_id> method=POST path=/v1/users/u1/credits/deduct
status=200 duration_ms=45 requestId=req_xyz
```

### Metrics (for the service operator)

| Metric | Source | Purpose |
|--------|--------|---------|
| `requests_total` by tenant, route, status | middleware | usage tracking, anomaly detection |
| `request_duration_ms` p50/p95/p99 | middleware | latency SLO |
| `api_keys_active` by tenant | scheduled count | key hygiene |
| `key_lookup_duration_ms` | auth middleware | auth path latency |
| `webhook_delivery_duration_ms` | worker | webhook health |
| `webhook_deliveries_pending` | worker count | backlog detection |
| `credits_deducted_total` by tenant | event listener | billing metering (for the managed service itself) |
| `rate_limit_hits_total` by tenant | rate limiter | abuse detection |

### Audit log

Every control-plane mutation is recorded in `audit_log`. This is for security forensics and compliance — not for general debugging (use structured logs for that).

### Health and readiness

- `GET /v1/health` — returns 200 when the process is alive. No auth.
- `GET /v1/ready` — returns 200 when DB connectivity is verified, migrations are current. No auth.
- `GET /v1/metrics` — Prometheus endpoint (internal).

## 6. API versioning

- All routes are prefixed with `/v1/`.
- When we need to introduce breaking changes, we create `/v2/` and support both concurrently for a deprecation window.
- The `ManagedBursarStore` in the SDK is configured with a version-aware base URL.
- Breaking changes are flagged with ample notice and a migration guide.

## 7. Security considerations

### Transport
- All traffic must be HTTPS (TLS 1.2+). HTTP is rejected by the load balancer.
- HSTS header: `Strict-Transport-Security: max-age=31536000; includeSubDomains`.

### Key storage
- API keys are shown once and never stored in full.
- Key hashes use SHA-256 (fast to verify; key entropy is high enough that preimage attack is not a concern).
- Admin keys (`sk_admin_`) follow the same hash-and-trash pattern.

### Rate limiting as security control
- Prevents brute-force of API keys (though guessing a 32-char random secret is astronomically unlikely).
- Prevents one compromised key from causing a billing explosion.

### Webhook secret
- Auto-generated per endpoint (32+ chars).
- Stored as hash in `webhook_endpoints.secret`. Only the hash is shown in the UI; the raw secret is shown once at creation.
- The raw secret must be stored by the customer to verify signatures.

### Request validation
- All inputs validated against JSON Schema before reaching bursar.
- `UsageMetrics` fields: `input_tokens` etc. must be non-negative integers, `amount` must be a valid decimal string.
- Pricing config validation reuses bursar's existing `BursarConfig` Pydantic validation (or its JS equivalent).

## 8. Graceful degradation

- **Cache degrade:** if the pricing config cache lookup fails, fall through to DB.
- **Rate limiter degrade:** if the rate limiter backend (Redis) is down, allow the request (fail-open, but log a warning). Token bucket implemented as a local in-memory fallback.
- **DB degrade:** health check detects DB unavailability, k8s kills the pod. In multi-instance setups, remaining instances handle traffic.
