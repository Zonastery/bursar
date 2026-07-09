# Service 2 — Data Plane API (SDK-facing)

Version `/v1`, JSON in/out. All money values as **string** (`"100.5000"`) to preserve Decimal precision — same wire format as bursar's existing RPCs. `Idempotency-Key` header honored on all mutating routes.

Consistent response envelope:

```
2xx:  { "data": { ... } }
4xx:  { "error": { "type": "insufficient_credits", "message": "...", "code": "..." } }
```

## Credits

### `GET /v1/users/:userId/balance`

**Scopes:** credits:read

Response:
```json
{ "data": { "user_id": "u1", "balance": "5000.0000", "lifetime_purchased": "10000.0000" } }
```

### `GET /v1/users/:userId/available`

**Scopes:** credits:read

Response:
```json
{ "data": { "user_id": "u1", "available": "4800.0000", "total_balance": "5000.0000", "active_holds": "200.0000" } }
```

### `GET /v1/users/:userId/buckets`

**Scopes:** credits:read

Response:
```json
{ "data": { "buckets": [
  { "bucket_key": "gifted",  "balance": "500.0000", "expires": true, "ttl_days": 30 },
  { "bucket_key": "purchased", "balance": "4500.0000", "expires": false, "ttl_days": null }
]}}
```

### `POST /v1/users/:userId/credits/add`

**Scopes:** credits:write

Body:
```json
{
  "amount": "1000.0000",
  "type": "purchase",
  "metadata": { "order_id": "ord_123" },
  "expires_at": "2027-01-01T00:00:00Z",
  "bucket": "purchased",
  "idempotency_key": "req_abc"
}
```

Response:
```json
{ "data": { "transaction_id": "txn_abc", "balance_after": "6000.0000", "amount": "1000.0000" } }
```

### `POST /v1/users/:userId/credits/deduct`

**The primary charge endpoint.** Service 2 recalculates cost from metrics using the tenant's active pricing — never trusts a client-sent amount.

**Scopes:** credits:write

Body:
```json
{
  "model": "gpt-4",
  "input_tokens": 500,
  "output_tokens": 200,
  "tool_calls": [{"name": "code_exec"}],
  "search_queries": 0,
  "search_results": 0,
  "cache_read_tokens": 0,
  "cache_write_tokens": 0,
  "web_search_calls": 0,
  "code_exec_calls": 0,
  "flat_job": null,
  "idempotency_key": "req_def",
  "metadata": { "session_id": "sess_456" },
  "skip_allowance": false,
  "feature": null
}
```

Response:
```json
{ "data": {
  "transaction_id": "txn_def",
  "amount": "-21.0000",
  "balance_before": "6000.0000",
  "balance_after": "5979.0000",
  "allowance_consumed": "0.0000",
  "cost_breakdown": {
    "model_cost": "15.0000",
    "tool_costs": { "code_exec": "6.0000" }
  }
}}
```

### `POST /v1/users/:userId/credits/deduct-fixed`

**Scopes:** credits:write

Body:
```json
{
  "job_name": "summarize",
  "idempotency_key": "req_ghi",
  "metadata": {},
  "use_allowance": false,
  "required_feature": null,
  "feature": null
}
```

Response: same shape as `deduct`.

### `POST /v1/transactions/:txId/refund`

**Scopes:** refunds:write

Body:
```json
{
  "amount": "10.0000",
  "reason": "overcharge",
  "metadata": { "ticket_id": "tkt_789" }
}
```

Response:
```json
{ "data": { "refund_transaction_id": "txn_jkl", "amount": "10.0000", "new_balance": "5989.0000" } }
```

## Leases (financial safety)

### `POST /v1/users/:userId/leases`

**Scopes:** leases:write

Body:
```json
{
  "metrics": { "model": "gpt-4", "input_tokens": 1000, "output_tokens": 500 },
  "operation_type": "usage",
  "billing_mode": "strict",
  "required_feature": null,
  "ttl_seconds": 600,
  "metadata": { "request_id": "req_xyz" },
  "model": null,
  "feature": null
}
```

`metrics` can also be a flat amount string for simple holds:
```json
{ "amount": "100.0000", "operation_type": "usage", ... }
```

Response:
```json
{ "data": {
  "lease_id": "lease_abc",
  "amount": "40.0000",
  "expires_at": "2026-07-09T12:05:00Z",
  "balance_after": "5959.0000",
  "available_after": "5919.0000"
}}
```

### `POST /v1/users/:userId/leases/:leaseId/settle`

**Scopes:** leases:write

Body:
```json
{
  "amount": "31.5000",
  "idempotency_key": "req_mno",
  "metadata": {},
  "skip_allowance": false,
  "feature": null
}
```

`amount` is the actual cost (de-clamped). Service 2 recalculates from metrics if possible, or uses the provided amount for simple holds.

Response:
```json
{ "data": { "transaction_id": "txn_pqr", "amount": "-31.5000", "balance_after": "5937.5000" } }
```

### `POST /v1/users/:userId/leases/:leaseId/release`

**Scopes:** leases:write

Body: (empty)

Response:
```json
{ "data": { "released_amount": "40.0000", "balance_after": "5959.0000" } }
```

### `POST /v1/users/:userId/leases/:leaseId/renew`

**Scopes:** leases:write

Body:
```json
{ "ttl_seconds": 300 }
```

Response:
```json
{ "data": { "lease_id": "lease_abc", "expires_at": "2026-07-09T12:10:00Z" } }
```

### `GET /v1/users/:userId/can-afford`

**Scopes:** credits:read

Query:
```
?model=gpt-4&input_tokens=500&output_tokens=200&operation_type=usage&billing_mode=strict
```

Response:
```json
{ "data": { "can_afford": true, "cost": "21.0000", "available": "5919.0000", "spendable": "5919.0000" } }
```

### `GET /v1/users/:userId/check-allowance`

**Scopes:** credits:read

Response:
```json
{ "data": { "user_id": "u1", "plan_id": "plan_abc", "plan_label": "Pro", "allowance_remaining": "40000.0000", "allowance_period": "calendar_month" } }
```

## Plans & entitlements

### `GET /v1/users/:userId/plan`

**Scopes:** plans:read

Response:
```json
{ "data": { "plan_id": "plan_abc", "plan_key": "pro", "label": "Pro", "allowance": { "amount": "50000.0000", "period": "calendar_month" }, "assigned_at": "2026-01-15T00:00:00Z" } }
```

### `PUT /v1/users/:userId/plan`

**Scopes:** plans:write

Body:
```json
{ "plan_key": "pro", "plan_assigned_at": "2026-01-15T00:00:00Z" }
```

Response:
```json
{ "data": { "plan_id": "plan_abc", "plan_key": "pro" } }
```

### `DELETE /v1/users/:userId/plan`

**Scopes:** plans:write

Response: `{ "data": { "user_id": "u1", "previous_plan": "pro" } }`

### `GET /v1/users/:userId/features/:feature`

**Scopes:** plans:read

Response:
```json
{ "data": { "feature": "max_daily_roadmaps", "entitled": true, "value": 10 } }
```

### `GET /v1/users/:userId/features/:feature/limit`

**Scopes:** plans:read

Response:
```json
{ "data": { "feature": "max_daily_roadmaps", "max_calls": 10, "period": "daily", "used": 3, "remaining": 7, "on_exceed": "deny" } }
```

## Subscriptions

### `POST /v1/users/:userId/subscription/grant-cycle`

**Scopes:** subscriptions:write

Body:
```json
{
  "amount": "50000.0000",
  "bucket": "subscription",
  "expires_at": "2026-08-09T00:00:00Z",
  "ttl_days": null,
  "replace_prior": true,
  "plan_key": "pro",
  "idempotency_key": "stripe_sub_event_123"
}
```

Response:
```json
{ "data": { "transaction_id": "txn_stu", "amount": "50000.0000", "balance_after": "55937.5000", "replaced_previous": true } }
```

## Spend caps

### `GET /v1/users/:userId/spend-caps`

**Scopes:** credits:read

Response:
```json
{ "data": { "caps": [
  { "cap_type": "daily", "model": null, "limit": "100.0000", "action": "deny" }
]}}
```

### `PUT /v1/users/:userId/spend-caps`

**Scopes:** spendcaps:write

Body:
```json
{ "cap_type": "daily", "limit": "200.0000", "action": "deny", "model": null }
```

Response: `{ "data": { "cap_type": "daily", "limit": "200.0000", "action": "deny" } }`

### `DELETE /v1/users/:userId/spend-caps`

**Scopes:** spendcaps:write

Query: `?cap_type=daily&model=gpt-4`

Response: `{ "data": { "removed": true } }`

## Teams

### `POST /v1/teams`

**Scopes:** teams:write

Body:
```json
{ "name": "Engineering", "initial_balance": "50000.0000" }
```

Response:
```json
{ "data": { "team_id": "team_abc", "name": "Engineering", "balance": "50000.0000", "member_count": 0 } }
```

### `GET /v1/teams/:teamId/balance`

**Scopes:** teams:write

Response:
```json
{ "data": { "team_id": "team_abc", "balance": "50000.0000" } }
```

### `POST /v1/teams/:teamId/members`

**Scopes:** teams:write

Body:
```json
{ "user_id": "user_456", "role": "member", "spend_cap": "10000.0000" }
```

Response:
```json
{ "data": { "team_id": "team_abc", "user_id": "user_456", "role": "member", "spend_cap": "10000.0000" } }
```

### `GET /v1/teams/:teamId/members`

**Scopes:** teams:write

Response:
```json
{ "data": { "members": [
  { "user_id": "user_123", "role": "admin", "spend_cap": null, "total_spent": "0.0000" },
  { "user_id": "user_456", "role": "member", "spend_cap": "10000.0000", "total_spent": "2000.0000" }
]}}
```

### `POST /v1/teams/:teamId/deduct`

**Scopes:** teams:write

Body:
```json
{
  "user_id": "user_456",
  "metrics": { "model": "gpt-4", "input_tokens": 500, "output_tokens": 200 },
  "idempotency_key": "req_vwx",
  "metadata": {}
}
```

Response:
```json
{ "data": { "transaction_id": "txn_yz1", "amount": "-21.0000", "team_balance_after": "49979.0000", "member_spent_after": "2021.0000" } }
```

## Analytics

All analytics routes require **credits:read** scope.

### `GET /v1/analytics/spend-by-user`

Query: `?start=2026-06-01T00:00:00Z&end=2026-07-01T00:00:00Z`

Response:
```json
{ "data": { "rows": [
  { "user_id": "u1", "total_spend": "521.0000", "transaction_count": 47 },
  { "user_id": "u2", "total_spend": "120.0000", "transaction_count": 12 }
]}}
```

### `GET /v1/analytics/spend-by-model`

Query: `?start=...&end=...`

Response:
```json
{ "data": { "rows": [
  { "model": "gpt-4", "total_spend": "400.0000", "request_count": 100 },
  { "model": "claude-3", "total_spend": "241.0000", "request_count": 55 }
]}}
```

### `GET /v1/analytics/top-users`

Query: `?limit=10&start=...&end=...`

Response:
```json
{ "data": { "rows": [ { "user_id": "u1", "total_spend": "521.0000" }, ... ] } }
```

### `GET /v1/analytics/daily-spend`

Query: `?start=...&end=...`

Response:
```json
{ "data": { "rows": [
  { "date": "2026-06-15", "total_spend": "45.0000", "request_count": 10 },
  { "date": "2026-06-16", "total_spend": "62.0000", "request_count": 14 }
]}}
```

### `GET /v1/analytics/aggregate`

Query: `?start=...&end=...`

Response:
```json
{ "data": { "total_spend": "641.0000", "total_requests": 159, "unique_users": 2, "avg_spend_per_user": "320.5000" } }
```

## User transaction & usage history

### `GET /v1/users/:userId/transactions`

Query: `?types=usage,refund&from_date=2026-06-01&to_date=2026-07-01&limit=50&offset=0`

**Scopes:** credits:read

Response:
```json
{ "data": { "transactions": [
  { "id": "txn_abc", "amount": "-21.0000", "type": "usage", "created_at": "...", "metadata": {} }
], "total": 47, "limit": 50, "offset": 0 }}
```

### `GET /v1/users/:userId/usage-events`

**Scopes:** credits:read

Response: similar list of usage event records.

## Credit expiry sweep

### `POST /v1/credits/sweep`

**Scopes:** credits:write

Body:
```json
{ "dry_run": true, "user_id": null }
```

Response:
```json
{ "data": { "swept_count": 5, "total_amount": "1500.0000", "dry_run": true, "details": [
  { "bucket_key": "gifted", "expired_count": 3, "expired_amount": "1000.0000" },
  { "bucket_key": "promo", "expired_count": 2, "expired_amount": "500.0000" }
]}}
```

## Config reads (for SDK pricing engine)

### `GET /v1/config/active`

**Scopes:** config:read

Response: the full active pricing config JSON:
```json
{ "data": { "version": 3, "config": {
  "version": 1,
  "metering": { "models": { "*": "input_tokens * 0.01 + output_tokens * 0.03" }, ... },
  "plans": { "free": { ... }, "pro": { ... } },
  ...
}, "label": "deploy-42", "created_at": "..." }}
```

### `GET /v1/config/versions`

**Scopes:** config:read

Response:
```json
{ "data": { "versions": [
  { "version": 3, "label": "deploy-42", "active": true, "created_at": "..." },
  { "version": 2, "label": "rollback-target", "active": false, "created_at": "..." }
]}}
```

### `GET /v1/config/versions/:version`

**Scopes:** config:read

Response: same shape as `config/active` but for a specific version.

## Operational

### `GET /v1/health`

No auth required.

Response: `{ "status": "ok", "service": "bursar-data-plane", "version": "0.1.0" }`

### `GET /v1/whoami`

Any valid API key. Returns context for debugging.

Response:
```json
{ "data": { "tenant_id": "tnt_abc", "key_id": "key_xyz", "scopes": ["credits:read", "credits:write", ...], "environment": "live" } }
```
