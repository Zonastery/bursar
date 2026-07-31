"""bursar CLI — database migrations and pricing-version management.

Built on :mod:`argparse` so flags, ``--help``, exit codes and type coercion are
handled by the stdlib rather than hand-rolled ``argv`` slicing.

Connection secrets are taken from ``DATABASE_URL``, never the command line.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from bursar.credits.store import CreditStore
    from bursar.credits.types import BursarConfigResult

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]


# ── Retry tuning ────────────────────────────────────────────────────────────
# A freshly-applied migration may not be visible to PostgREST until its schema
# cache reloads. Only that *transient* condition is retried — never auth,
# validation, or a write that may have already committed server-side.
_RETRY_INITIAL_DELAY = 1.0
_RETRY_MAX_DELAY = 8.0
_RETRIES = 5

# Substrings that mark a transient PostgREST schema-cache / connectivity miss.
# These are matched case-insensitively against the StoreError message.
_TRANSIENT_MARKERS = (
    "pgrst205",  # PostgREST: requested function not found in schema cache
    "pgrst204",  # PostgREST: column not found in schema cache
    "pgrst202",  # PostgREST: function signature not found in schema cache
    "schema cache",
    "could not find the function",
    "timed out",
    "request error",  # wrapped httpx.RequestError (connection refused/reset)
    "connection",
)


def _load_env() -> None:
    """Load ``.env`` from CWD. Existing environment variables win."""
    env_path = Path.cwd() / ".env"
    if env_path.is_file() and load_dotenv:
        load_dotenv(env_path, override=False)


# Extra name → top-level import names needed
_EXTRAS: dict[str, list[str]] = {
    "postgres": ["psycopg2"],
}


def _require_extra(extra: str) -> None:
    """Exit (code 1) with an install hint if any import for *extra* is missing."""
    for mod in _EXTRAS.get(extra, []):
        try:
            __import__(mod)
        except ImportError:
            print(
                f"bursar[{extra}] extra required (missing: {mod}). pip install bursar[{extra}]",
                file=sys.stderr,
            )
            raise SystemExit(1) from None


def _is_transient(exc: Exception) -> bool:
    """True only for the PostgREST schema-cache / connection errors worth retrying."""
    from bursar.credits.store import StoreError

    if not isinstance(exc, StoreError):
        return False
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _retry_transient[T](op: Callable[[], T], *, what: str) -> T:
    """Run *op*, retrying ONLY transient PostgREST/connection errors (H7).

    A non-transient error (auth, validation, a write that already committed)
    is surfaced immediately so we never create a duplicate immutable pricing
    version by blind-retrying a non-idempotent write.
    """
    delay = _RETRY_INITIAL_DELAY
    for attempt in range(_RETRIES):
        try:
            return op()
        except Exception as exc:
            last = attempt == _RETRIES - 1
            if last or not _is_transient(exc):
                print(f"Failed to {what}: {exc}", file=sys.stderr)
                if _is_transient(exc):
                    print(
                        "Tip: run 'bursar migrate' and wait for the PostgREST schema cache to refresh.",
                        file=sys.stderr,
                    )
                raise SystemExit(1) from exc
            time.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY)
    raise AssertionError("unreachable")  # pragma: no cover


def _store_from_env(
    store_type: str | None = None,
    *,
    tenant_id: str | None = None,
) -> CreditStore:
    """Create a store from env vars (``DATABASE_URL``).

    Args:
        store_type: ``"postgres"``. Falls back to the
            ``BURSAR_STORE`` env var, then ``"postgres"``.
    """
    kind = store_type or os.environ.get("BURSAR_STORE", "postgres")

    if kind != "postgres":
        print(f"Unknown store: {kind}", file=sys.stderr)
        raise SystemExit(1)

    _require_extra("postgres")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL required", file=sys.stderr)
        raise SystemExit(1)
    resolved_tenant_id = tenant_id or os.environ.get("BURSAR_TENANT_ID")
    if not resolved_tenant_id:
        print("BURSAR_TENANT_ID required", file=sys.stderr)
        raise SystemExit(1)
    from bursar.credits.postgres.store import PostgresStore

    return PostgresStore(database_url=database_url, tenant_id=resolved_tenant_id)


# ── File loading ─────────────────────────────────────────────────────────────


def _load_pricing_file(filepath: str) -> dict[str, Any]:
    """Read a JSON or YAML pricing config into a dict.

    All failure modes (missing file, directory, permission denied, parse error,
    empty/non-object payload) print a clean message to stderr and exit 1 — no
    tracebacks (M12).
    """
    is_yaml = filepath.endswith((".yaml", ".yml"))

    if filepath == "-":
        raw = sys.stdin.read()
        data = _parse_pricing_text(raw, is_yaml=False, source="<stdin>")
    else:
        path = Path(filepath)
        if path.is_dir():
            print(f"Not a file (is a directory): {filepath}", file=sys.stderr)
            raise SystemExit(1)
        try:
            raw = path.read_text()
        except FileNotFoundError:
            print(f"File not found: {filepath}", file=sys.stderr)
            raise SystemExit(1) from None
        except PermissionError:
            print(f"Permission denied: {filepath}", file=sys.stderr)
            raise SystemExit(1) from None
        except OSError as exc:
            print(f"Could not read {filepath}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        data = _parse_pricing_text(raw, is_yaml=is_yaml, source=filepath)

    if not isinstance(data, dict):
        print(f"Pricing config must be a JSON/YAML object, got {type(data).__name__}", file=sys.stderr)
        raise SystemExit(1)
    if not data:
        print("Pricing config is empty.", file=sys.stderr)
        raise SystemExit(1)
    return data


def _parse_pricing_text(raw: str, *, is_yaml: bool, source: str) -> Any:
    """Parse *raw* as YAML or JSON, exiting 1 with a clean message on failure."""
    if is_yaml:
        try:
            import yaml
        except ImportError:
            print("PyYAML required for .yaml files: pip install bursar[postgres]", file=sys.stderr)
            raise SystemExit(1) from None

        class _StrictYamlLoader(yaml.SafeLoader):
            pass

        def _construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise yaml.YAMLError(f"duplicate key: {key!r}")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        _StrictYamlLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
        try:
            return yaml.load(raw, Loader=_StrictYamlLoader)
        except yaml.YAMLError as exc:
            print(f"Invalid YAML in {source}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {source}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


# ── Command handlers ─────────────────────────────────────────────────────────


def _read_post_migrate_sql(paths: list[str]) -> list[tuple[str, str]]:
    """Read trusted host SQL before opening the migration transaction."""
    statements: list[tuple[str, str]] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            sql = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Cannot read post-migration SQL file {path}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        if not sql.strip():
            print(f"Post-migration SQL file is empty: {path}", file=sys.stderr)
            raise SystemExit(1)
        statements.append((str(path), sql))
    return statements


def _cmd_migrate(args: argparse.Namespace) -> None:
    _require_extra("postgres")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(1)
    from bursar.credits.postgres.store import run_migrations

    post_migration_sql = _read_post_migrate_sql(args.post_migrate_sql)
    run_migrations(database_url, post_migration_sql=post_migration_sql)
    print("Migrations applied successfully.")


def _provision_tenant(
    *,
    tenant_id: UUID,
    slug: str,
    display_name: str | None,
) -> UUID:
    _require_extra("postgres")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(1)

    import psycopg2

    try:
        with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT bursar.create_tenant(%s::uuid, %s::text, %s::text)",
                (str(tenant_id), slug, display_name),
            )
            row = cursor.fetchone()
            if row is None:
                print("Failed to create tenant: no result returned", file=sys.stderr)
                raise SystemExit(1)
            created_id = row[0]
    except psycopg2.Error as exc:
        print(f"Failed to create tenant: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    return UUID(str(created_id))


def _parse_tenant_id(raw_tenant_id: str | None, *, generate: bool) -> UUID:
    if raw_tenant_id is None and generate:
        return uuid4()
    if raw_tenant_id is None:
        print("BURSAR_TENANT_ID or --id is required", file=sys.stderr)
        raise SystemExit(1)
    try:
        return UUID(raw_tenant_id)
    except ValueError:
        print("Tenant ID must be a UUID", file=sys.stderr)
        raise SystemExit(1) from None


def _cmd_tenant_create(args: argparse.Namespace) -> None:
    tenant_id = _parse_tenant_id(args.id, generate=True)
    created_id = _provision_tenant(
        tenant_id=tenant_id,
        slug=args.slug,
        display_name=args.display_name,
    )
    print(str(created_id))


def _cmd_tenant_status(args: argparse.Namespace) -> None:
    _require_extra("postgres")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(1)

    try:
        tenant_id = UUID(args.id)
    except ValueError:
        print("Tenant ID must be a UUID", file=sys.stderr)
        raise SystemExit(1) from None

    import psycopg2

    try:
        with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT bursar.set_tenant_status(%s::uuid, %s::text)",
                (str(tenant_id), args.status),
            )
            row = cursor.fetchone()
            if row is None:
                print("Failed to update tenant: no result returned", file=sys.stderr)
                raise SystemExit(1)
            updated = row[0]
    except psycopg2.Error as exc:
        print(f"Failed to update tenant: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not updated:
        print("Tenant not found", file=sys.stderr)
        raise SystemExit(1)
    print(str(tenant_id))


def _cmd_config_validate(args: argparse.Namespace) -> None:
    from bursar.config import ConfigError, load_config_from_dict

    data = _load_pricing_file(args.file)
    try:
        load_config_from_dict(data)
    except ConfigError as exc:
        if args.json:
            print(json.dumps({"valid": False, "errors": exc.errors()}, indent=2, default=str))
        else:
            print(f"Validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if args.json:
        print(json.dumps({"valid": True, "errors": []}, indent=2))
    else:
        print("Pricing config is valid.")


def _validated_config(filepath: str) -> dict[str, Any]:
    from bursar.config import ConfigError, canonical_bursar_config_dict

    data = _load_pricing_file(filepath)
    try:
        return canonical_bursar_config_dict(data)
    except ConfigError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def _set_config(
    data: dict[str, Any],
    *,
    store_type: str,
    tenant_id: str | None = None,
    label: str | None = None,
) -> bool:
    store = _store_from_env(store_type, tenant_id=tenant_id)

    # Abort if identical to the currently active version to avoid pointless
    # version churn. First-time setup always proceeds.
    active = store.get_active_pricing()
    if active is not None and data == active.config:
        return False

    _retry_transient(lambda: store.set_active_pricing(data, label=label), what="set pricing")
    return True


def _cmd_config_set(args: argparse.Namespace) -> None:
    data = _validated_config(args.file)
    if not _set_config(data, store_type=args.store, label=args.label):
        print("No changes — config is identical to the active version.")
        return
    print("Bursar config set successfully.")


def _cmd_tenant_bootstrap(args: argparse.Namespace) -> None:
    # Validate before provisioning so malformed config cannot leave behind a
    # tenant that can never start. Provisioning and config publication are both
    # idempotent, so an operational failure can be retried safely.
    data = _validated_config(args.file)
    tenant_id = _parse_tenant_id(
        args.id or os.environ.get("BURSAR_TENANT_ID"),
        generate=False,
    )
    created_id = _provision_tenant(
        tenant_id=tenant_id,
        slug=args.slug,
        display_name=args.display_name,
    )
    changed = _set_config(
        data,
        store_type=args.store,
        tenant_id=str(created_id),
        label=args.label,
    )
    outcome = "config applied" if changed else "config unchanged"
    print(f"Tenant {created_id} bootstrapped successfully ({outcome}).")


def _cmd_config_get(args: argparse.Namespace) -> None:
    store = _store_from_env(args.store)
    result = _retry_transient(store.get_active_pricing, what="get pricing")
    if result is None:
        print("No active Bursar config.", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result.model_dump(mode="json"), indent=2))


def _cmd_config_list(args: argparse.Namespace) -> None:
    store = _store_from_env(args.store)
    rows = _retry_transient(store.get_pricing_history, what="list pricing")
    if not rows:
        print("No Bursar configs found.", file=sys.stderr)
        raise SystemExit(1)
    for r in rows:
        marker = "*" if r.active else " "
        label = f"  {r.label}" if r.label else ""
        print(f"  {marker} v{r.version}  (id={r.id[:8]}...){label}  {r.created_at[:19]}")


def _cmd_config_activate(args: argparse.Namespace) -> None:
    store = _store_from_env(args.store)
    _retry_transient(lambda: store.activate_pricing(args.version), what="activate pricing")
    print(f"Pricing v{args.version} activated.")


def _cmd_config_export(args: argparse.Namespace) -> None:
    from bursar.config import BursarConfig

    store = _store_from_env(args.store)
    result = _retry_transient(lambda: store.get_bursar_config(args.version), what="fetch pricing")
    if result is None:
        print(f"Version {args.version} not found.", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(BursarConfig.model_validate(result.config).model_dump(mode="json"), indent=2))


def _cmd_config_diff(args: argparse.Namespace) -> None:
    from bursar.config import BursarConfig

    store = _store_from_env(args.store)

    def _fetch() -> tuple[BursarConfigResult | None, BursarConfigResult | None]:
        return store.get_bursar_config(args.version_a), store.get_bursar_config(args.version_b)

    a, b = _retry_transient(_fetch, what="fetch pricing configs")
    if a is None:
        print(f"Version {args.version_a} not found.", file=sys.stderr)
        raise SystemExit(1)
    if b is None:
        print(f"Version {args.version_b} not found.", file=sys.stderr)
        raise SystemExit(1)

    a_json = json.dumps(BursarConfig.model_validate(a.config).model_dump(mode="json"), indent=2)
    b_json = json.dumps(BursarConfig.model_validate(b.config).model_dump(mode="json"), indent=2)
    diff = difflib.unified_diff(
        a_json.splitlines(keepends=True),
        b_json.splitlines(keepends=True),
        fromfile=f"v{args.version_a}",
        tofile=f"v{args.version_b}",
    )
    sys.stdout.writelines(diff)


def _cmd_config_schema(_args: argparse.Namespace) -> None:
    """Print the pricing config JSON Schema (for editor autocompletion/validation)."""
    from bursar.config import BursarConfig

    print(json.dumps(BursarConfig.model_json_schema(), indent=2))


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="bursar",
        description="bursar — credit calculation engine: migrations & pricing management.",
    )
    parser.add_argument(
        "--store",
        choices=["postgres"],
        default=os.environ.get("BURSAR_STORE", "postgres"),
        help="Store backend (env: BURSAR_STORE, default: postgres)",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # migrate
    p_migrate = sub.add_parser(
        "migrate",
        help="Run database migrations (bursar[postgres])",
        description="Run bundled SQL migrations using DATABASE_URL.",
    )
    p_migrate.add_argument(
        "--post-migrate-sql",
        action="append",
        default=[],
        metavar="FILE",
        help=(
            "Trusted host SQL file to run after bundled migrations in the same transaction; repeat for multiple files"
        ),
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    # tenant
    p_tenant = sub.add_parser(
        "tenant",
        help="Manage SaaS tenants",
        description="Provision and inspect Bursar tenants.",
    )
    tenant_sub = p_tenant.add_subparsers(
        dest="subcommand",
        metavar="<subcommand>",
    )
    p_tenant_create = tenant_sub.add_parser(
        "create",
        help="Provision an idempotent tenant",
    )
    p_tenant_create.add_argument("slug", help="Unique tenant slug")
    p_tenant_create.add_argument(
        "--id",
        default=None,
        help="Tenant UUID (generated when omitted)",
    )
    p_tenant_create.add_argument(
        "--display-name",
        default=None,
        help="Optional display name",
    )
    p_tenant_create.set_defaults(func=_cmd_tenant_create)
    p_tenant_bootstrap = tenant_sub.add_parser(
        "bootstrap",
        help="Provision a tenant and apply its initial config",
    )
    p_tenant_bootstrap.add_argument("slug", help="Unique tenant slug")
    p_tenant_bootstrap.add_argument(
        "file",
        help="JSON/YAML pricing file, or '-' for stdin",
    )
    p_tenant_bootstrap.add_argument(
        "--id",
        default=None,
        help="Tenant UUID (defaults to BURSAR_TENANT_ID)",
    )
    p_tenant_bootstrap.add_argument(
        "--display-name",
        default=None,
        help="Optional display name",
    )
    p_tenant_bootstrap.add_argument(
        "--label",
        default=None,
        help="Optional label/message for the initial config version",
    )
    p_tenant_bootstrap.set_defaults(func=_cmd_tenant_bootstrap)
    p_tenant_status = tenant_sub.add_parser(
        "status",
        help="Activate, suspend, or close a tenant",
    )
    p_tenant_status.add_argument("id", help="Tenant UUID")
    p_tenant_status.add_argument(
        "status",
        choices=["active", "suspended", "closed"],
    )
    p_tenant_status.set_defaults(func=_cmd_tenant_status)

    # pricing
    p_config = sub.add_parser(
        "config",
        help="Manage Bursar config",
        description="Manage immutable Bursar catalog versions.",
    )
    psub = p_config.add_subparsers(dest="subcommand", metavar="<subcommand>")

    p_set = psub.add_parser("set", help="Apply config (always creates a new version)")
    p_set.add_argument("file", help="JSON/YAML pricing file, or '-' for stdin")
    p_set.add_argument("--label", default=None, help="Optional label/message for this version")
    p_set.set_defaults(func=_cmd_config_set)

    p_get = psub.add_parser("get", help="Show the active Bursar config as JSON")
    p_get.set_defaults(func=_cmd_config_get)

    p_list = psub.add_parser("list", help="List all pricing versions (* = active)")
    p_list.set_defaults(func=_cmd_config_list)

    p_activate = psub.add_parser("activate", help="Switch the active version")
    p_activate.add_argument("version", type=int, help="Version number to activate")
    p_activate.set_defaults(func=_cmd_config_activate)

    p_validate = psub.add_parser("validate", help="Validate a pricing file without applying it")
    p_validate.add_argument("file", help="JSON/YAML pricing file, or '-' for stdin")
    p_validate.add_argument("--json", action="store_true", help="Emit native validator errors as JSON")
    p_validate.set_defaults(func=_cmd_config_validate)

    p_schema = psub.add_parser("schema", help="Print the Bursar config JSON Schema")
    p_schema.set_defaults(func=_cmd_config_schema)

    p_diff = psub.add_parser("diff", help="Unified diff between two versions")
    p_diff.add_argument("version_a", type=int, help="First version")
    p_diff.add_argument("version_b", type=int, help="Second version")
    p_diff.set_defaults(func=_cmd_config_diff)

    p_export = psub.add_parser("export", help="Dump a version as JSON")
    p_export.add_argument("version", type=int, help="Version number to export")
    p_export.set_defaults(func=_cmd_config_export)

    p_config.set_defaults(_pricing_parser=p_config)
    return parser


def main(argv: list[str] | None = None) -> None:
    _load_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        raise SystemExit(1)

    # `config` with no subcommand: show its help and exit non-zero.
    if not hasattr(args, "func"):
        sub_parser = getattr(args, "_pricing_parser", parser)
        sub_parser.print_help(sys.stderr)
        raise SystemExit(1)

    args.func(args)


if __name__ == "__main__":
    main()
