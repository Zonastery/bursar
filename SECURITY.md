# Security Policy

ducto is a credit/billing engine with a sandboxed expression evaluator. We take
security reports — especially sandbox escapes and money-safety issues —
seriously. **Please do not open a public issue for a vulnerability.**

## Reporting a Vulnerability

Use one of these **private** channels:

1. **GitHub Private Vulnerability Reporting (preferred).** Go to the
   repository's **Security** tab → **Report a vulnerability**
   (<https://github.com/apoorwv/ducto/security/advisories/new>). This opens a
   private security advisory visible only to you and the maintainers.
2. **Email:** <apoorwv@gmail.com> with the subject line `ducto security`.

Please include a description of the issue, the affected version(s), and a
minimal reproduction (e.g. the pricing expression or API call that triggers it).

## Response Targets

| Stage | Target |
|-------|--------|
| Acknowledgement of report | within **3 business days** |
| Initial assessment / triage | within **7 business days** |
| Fix or mitigation for a confirmed High/Critical issue | within **30 days**, coordinated with you on disclosure timing |

We will keep you updated through the advisory/email thread and credit you in the
release notes unless you prefer to remain anonymous.

## Scope

This is a billing and sandbox-security-sensitive library. We are particularly
interested in:

- **Expression sandbox escapes** — the evaluator (`python/src/ducto/expr.py`,
  `javascript/src/expr.ts`) is designed to reject arbitrary code execution via
  an AST allowlist. Pricing expressions are loaded from the database, so they
  are a real trust boundary. Any bypass that allows unauthorized computation,
  resource exhaustion (DoS), or data access is in scope.
- **Money-safety / integrity bugs** — non-atomic deductions, double-spend,
  idempotency bypass, spend-cap bypass, refund-of-refund, or any path that lets
  a caller be over- or under-charged.
- **Data exposure** — RPCs callable by `anon`/`authenticated` roles that leak
  another user's balance/transaction history on a Supabase deployment.
- **Credential handling** — leakage of connection strings or publish tokens.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅ |
| < 1.0   | ❌ |
