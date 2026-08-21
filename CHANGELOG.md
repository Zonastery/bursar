# Changelog

Notable user-facing changes to Bursar are recorded here. Future release tags
also publish generated notes on the [GitHub Releases](https://github.com/Zonastery/bursar/releases)
page after the Python and TypeScript packages are published successfully.

## [Unreleased]

See the [comparison with 2.0.4](https://github.com/Zonastery/bursar/compare/v2.0.4...HEAD).

## [2.0.4] - 2026-08-21

### Added

- Added the Go Google ADK integration module and expanded Go API and integration
  documentation.
- Added cross-SDK contract, package-smoke, and production-confidence coverage
  for billing, commerce, storage, auto-recharge, entitlements, and financial
  invariants.

### Changed

- Expanded and aligned the JavaScript, Python, and Go billing, commerce,
  provider, storage, and PostgreSQL lifecycle contracts.
- Standardized the JavaScript SDK quality toolchain on Oxlint and Oxfmt and
  expanded the CI matrix across supported Node, Python, PostgreSQL, and Go
  versions.

### Fixed

- Hardened billing event processing, subscription entitlement reconciliation,
  provider error handling, storage maintenance, and PostgreSQL state boundaries.
- Added release and parity gates that validate SDK versions, package artifacts,
  and cross-SDK behavior before publishing.

## [2.0.3] - 2026-08-15

### Added

- Added the Go SDK and cross-SDK contract parity and package smoke coverage.
- Added OpenTelemetry instrumentation plus storage diagnostics, maintenance,
  and recovery APIs to the Python and TypeScript SDKs.

### Changed

- Expanded billing, commerce, provider, storage, and persistence contracts
  across the supported SDKs.

### Fixed

- Hardened storage runtime maintenance, partition permissions, provider event
  mapping, and PostgreSQL integration contracts.

## [2.0.2] - 2026-08-10

### Added

- Stabilized the application-facing `Bursar` facade and explicit credit,
  billing, catalog, account, and commerce capability contracts across the
  Python and TypeScript SDKs.
- Added caller-stable idempotency helpers, exact decimal credit inputs, and
  explicit provider-environment types for replay-safe financial operations.
- Added AI SaaS onboarding guides, package smoke examples, agent-readable
  documentation, and improved repository discovery metadata.

### Changed

- Centralized configuration loading and aligned the Python and TypeScript
  configuration, pricing, commerce, provider, storage, and persistence
  contracts.
- Made six-decimal, half-up credit accounting an SDK invariant instead of a
  configurable catalog field.
- Consolidated the PostgreSQL baseline and clarified tenant, catalog, billing,
  and storage lifecycle responsibilities.
- Narrowed package-root exports while retaining detailed contracts in focused
  subpackages.

### Fixed

- Aligned the Google ADK integration with the public credit capability.
- Corrected provider event identity mapping, retry/idempotency behavior,
  catalog plan resolution, and storage worker coordination.
- Strengthened cross-SDK parity, package smoke, browser-entry, migration
  manifest, and CI quality checks.

### Security

- Provider-backed operations now fail closed unless their environment is
  explicitly configured.
- Hardened PostgreSQL caller-role membership validation and tenant creation
  while preserving host-owned extension privileges.

### Upgrade note

- v2.0.2 ships a consolidated greenfield SQL baseline. Existing v2.0.1
  `bursar.schema_migrations` ledgers are intentionally not supported in place;
  recreate the Bursar schema or database before deploying this release.

## [2.0.1] - 2026-08-08

### Security

- Hardened runtime PostgreSQL function privileges.

### Documentation

- Updated SDK references for the 2.x release line.

## [2.0.0] - 2026-08-08

### Added

- Expanded credit, billing, subscription, team, Stripe, and Dodo workflows.
- Published synchronized Python and TypeScript SDK behavior against one PostgreSQL schema and shared contract fixtures.

### Changed

- Redesigned schema and integration contracts for the 2.x release line.
- Consolidated the documentation and refreshed Context7 indexing metadata.

### Security

- Tightened credit-account ownership checks and database contracts.

## Earlier releases

- [1.0.1](https://github.com/Zonastery/bursar/compare/v1.0.0...v1.0.1)
- [1.0.0](https://github.com/Zonastery/bursar/compare/v0.0.2...v1.0.0)
- [0.0.2](https://github.com/Zonastery/bursar/compare/v0.0.1...v0.0.2)
- [0.0.1](https://github.com/Zonastery/bursar/releases/tag/v0.0.1)

[2.0.4]: https://github.com/Zonastery/bursar/compare/v2.0.3...v2.0.4
[2.0.3]: https://github.com/Zonastery/bursar/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/Zonastery/bursar/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/Zonastery/bursar/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/Zonastery/bursar/compare/v1.0.1...v2.0.0
