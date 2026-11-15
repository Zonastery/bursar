# Bursar Web Service

`bursar-web` is the HTTP façade over the Bursar credit and billing core (the Go
SDK lives in [`../golang`](../golang)). This module ships the platform
foundation — configuration, structured logging, OpenTelemetry, health and
readiness probes, and graceful shutdown — so feature routes can be added
without re-solving operational concerns. It requires Go 1.26+.

## Quick start

```bash
go run ./cmd/bursar-web
```

```bash
curl -s http://localhost:8080/healthz   # liveness: 200 OK once serving
curl -s http://localhost:8080/readyz    # readiness: 200 OK; probes Postgres when DATABASE_URL is set
curl -s http://localhost:8080/version   # build info: version, commit, date
```

## Configuration

All behavior is configured through environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BURSAR_WEB_ADDR` | `:8080` | HTTP listen address |
| `BURSAR_WEB_ENV` | `development` | `development` or `production`; selects logging defaults |
| `BURSAR_WEB_LOG_LEVEL` | `info` | slog level (`debug`, `info`, `warn`, `error`) |
| `BURSAR_WEB_TELEMETRY_ENABLED` | `false` | Enables OpenTelemetry traces and metrics export |
| `BURSAR_WEB_SHUTDOWN_GRACE` | `30s` | Grace period for in-flight requests during shutdown |
| `BURSAR_WEB_READ_HEADER_TIMEOUT` | `10s` | Maximum time to read request headers (slowloris protection) |
| `BURSAR_WEB_READ_TIMEOUT` | `30s` | Maximum time to read the full request |
| `BURSAR_WEB_WRITE_TIMEOUT` | `60s` | Maximum time to write the response |
| `BURSAR_WEB_IDLE_TIMEOUT` | `120s` | Maximum idle time for keep-alive connections |
| `BURSAR_WEB_HANDLER_TIMEOUT` | `30s` | Per-request handler timeout |
| `DATABASE_URL` | unset | Optional PostgreSQL connection string; enables the `readyz` database probe |

When telemetry is enabled (`BURSAR_WEB_TELEMETRY_ENABLED=true`), export follows
the standard `OTEL_EXPORTER_OTLP_*` variables (endpoint, headers, protocol,
sampling) as defined by the OpenTelemetry specification.

## Observability

- **Logging** — structured `log/slog` JSON records on stdout.
- **Tracing and metrics** — OpenTelemetry spans and metrics exported over
  OTLP/HTTP when telemetry is enabled.
- **Propagation** — W3C Trace Context headers are honored end to end.

## Layout

```
web/
├── cmd/bursar-web/       # Entrypoint
└── internal/
    ├── app/              # Wiring, server lifecycle, graceful shutdown
    ├── config/           # Environment parsing and validation
    ├── httpapi/          # Router and handlers (chi v5)
    ├── observability/    # slog setup and OpenTelemetry initialization
    ├── postgres/         # pgx/v5 pool for the readiness probe
    └── version/          # Build metadata injected via -ldflags
```

## Testing and quality

| Target | Purpose |
| --- | --- |
| `make lint` | gofmt check, `go vet`, pinned Staticcheck (`staticcheck@v0.8.2`) |
| `make test` | Race-enabled unit tests |
| `make cover` | Atomic coverage profile plus the coverage gate |
| `make tidy-check` | Fails if `go.mod`/`go.sum` are not tidy |
| `make build` | Static binary into `bin/bursar-web` with version metadata |
| `make ci` | Full gate: tidy-check, lint, test, cover |

Integration tests run against a real PostgreSQL instance only when
`TEST_DATABASE_URL` is set; otherwise they skip, mirroring the convention used
by the Go SDK in `../golang`.

Coverage thresholds live in `.testcoverage.yml`: 90% per package and 90%
total, enforced by `go-test-coverage`. The threshold is a floor — ratchet it
up, never down.

## Docker

The image builds a static binary and runs it as non-root on distroless base.

```bash
docker build --build-arg VERSION=v2.0.0 -t bursar-web:v2.0.0 .
docker run --rm -p 8080:8080 bursar-web:v2.0.0
```

## Roadmap

Wire the Bursar Go SDK (`github.com/Zonastery/bursar/golang/v2`) into this
façade to expose credits, catalog, and billing routes over authenticated HTTP.

## License

AGPL-3.0-only. See [LICENSE](../LICENSE).
