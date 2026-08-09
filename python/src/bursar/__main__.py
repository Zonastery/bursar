"""Bursar CLI for database migrations and catalog management.

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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from bursar.retry import BursarRetryOptions, retry_bursar_operation

if TYPE_CHECKING:
    from bursar.credits.store import CreditStore
    from bursar.credits.types import CatalogRevision

load_dotenv: Callable[..., bool] | None
try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:
    load_dotenv = None
else:
    load_dotenv = _load_dotenv


# ── Retry tuning ────────────────────────────────────────────────────────────
# Retry typed transient PostgreSQL failures, but never a mutation whose commit
# outcome is indeterminate.
_RETRY_INITIAL_DELAY = 1.0
_RETRY_MAX_DELAY = 8.0
_RETRIES = 5


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


def _is_safe_to_retry(exc: BaseException) -> bool:
    """Return whether a typed transient failure is safe to replay."""
    from bursar.errors import StoreError, is_retryable_bursar_error

    return is_retryable_bursar_error(exc) and not (isinstance(exc, StoreError) and exc.indeterminate)


def _retry_store_operation[T](op: Callable[[], T], *, what: str) -> T:
    """Run *op*, retrying only replay-safe transient PostgreSQL failures.

    A non-transient error (auth, validation, a write that already committed)
    is surfaced immediately so we never create a duplicate immutable catalog
    revision by blind-retrying a non-idempotent write.
    """
    try:
        return retry_bursar_operation(
            op,
            retry_options=BursarRetryOptions(
                max_attempts=_RETRIES,
                base_delay_seconds=_RETRY_INITIAL_DELAY,
                max_delay_seconds=_RETRY_MAX_DELAY,
                factor=2,
                jitter=False,
                should_retry=_is_safe_to_retry,
            ),
        )
    except Exception as exc:
        print(f"Failed to {what}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _store_from_env(
    *,
    tenant_id: str | None = None,
) -> CreditStore:
    """Create the Postgres store from its required environment variables."""
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


def _load_config_file(filepath: str) -> dict[str, Any]:
    """Read a JSON or YAML Bursar config into a dict.

    All failure modes (missing file, directory, permission denied, parse error,
    empty/non-object payload) print a clean message to stderr and exit 1 — no
    tracebacks.
    """
    from bursar.errors import ConfigError
    from bursar.load_config_file import load_config_file, load_config_text

    try:
        if filepath == "-":
            return load_config_text(sys.stdin.read(), is_yaml=False, source="<stdin>")
        return load_config_file(filepath)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
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

    data = _load_config_file(args.file)
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
        print("Bursar config is valid.")


def _validated_config(filepath: str) -> dict[str, Any]:
    from bursar.config import ConfigError, canonical_bursar_config_dict

    data = _load_config_file(filepath)
    try:
        return canonical_bursar_config_dict(data)
    except ConfigError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def _validated_rollout(
    filepath: str | None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if filepath is None:
        return None

    from bursar.config import (
        ConfigError,
        canonical_catalog_rollout_dict,
        load_config_from_dict,
    )

    data = _load_config_file(filepath)
    try:
        parsed_config = load_config_from_dict(config) if config is not None else None
        return canonical_catalog_rollout_dict(data, parsed_config)
    except ConfigError as exc:
        print(f"Rollout validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def _set_config(
    data: dict[str, Any],
    *,
    tenant_id: str | None = None,
    label: str | None = None,
    rollout: dict[str, Any] | None = None,
) -> bool:
    store = _store_from_env(tenant_id=tenant_id)

    # Abort if identical to the currently active version to avoid pointless
    # version churn. First-time setup always proceeds.
    active = store.get_active_catalog()
    if active is not None and data == active.config:
        if rollout is not None and rollout.get("plans"):
            _retry_store_operation(
                lambda: store.activate_catalog_revision(active.version, rollout),
                what="apply catalog rollout",
            )
            return True
        return False

    _retry_store_operation(
        lambda: store.publish_and_activate_catalog(data, label=label, rollout=rollout),
        what="publish catalog",
    )
    return True


def _cmd_config_set(args: argparse.Namespace) -> None:
    data = _validated_config(args.file)
    rollout = _validated_rollout(args.rollout, data)
    if not _set_config(
        data,
        label=args.label,
        rollout=rollout,
    ):
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
        tenant_id=str(created_id),
        label=args.label,
    )
    outcome = "config applied" if changed else "config unchanged"
    print(f"Tenant {created_id} bootstrapped successfully ({outcome}).")


def _cmd_config_get(args: argparse.Namespace) -> None:
    store = _store_from_env()
    result = _retry_store_operation(store.get_active_catalog, what="get active catalog")
    if result is None:
        print("No active Bursar config.", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result.model_dump(mode="json"), indent=2))


def _cmd_config_list(args: argparse.Namespace) -> None:
    store = _store_from_env()
    rows = _retry_store_operation(store.get_catalog_history, what="list catalog revisions")
    if not rows:
        print("No Bursar configs found.", file=sys.stderr)
        raise SystemExit(1)
    for r in rows:
        marker = "*" if r.active else " "
        label = f"  {r.label}" if r.label else ""
        print(f"  {marker} v{r.version}  (id={r.id[:8]}...){label}  {r.created_at[:19]}")


def _cmd_config_activate(args: argparse.Namespace) -> None:
    store = _store_from_env()
    rollout = _validated_rollout(args.rollout)
    _retry_store_operation(
        lambda: store.activate_catalog_revision(args.version, rollout),
        what="activate catalog revision",
    )
    print(f"Catalog revision {args.version} activated.")


def _cmd_config_pin(args: argparse.Namespace) -> None:
    store = _store_from_env()
    pinned = not args.unpin
    changed = _retry_store_operation(
        lambda: store.set_plan_revision_pin(args.subject_id, pinned),
        what="update plan revision pin",
    )
    if not changed:
        print("No current plan assignment found.", file=sys.stderr)
        raise SystemExit(1)
    state = "pinned" if pinned else "unpinned"
    print(f"Plan revision {state} for {args.subject_id}.")


def _cmd_config_apply_due(args: argparse.Namespace) -> None:
    store = _store_from_env()
    applied = _retry_store_operation(
        lambda: store.apply_due_plan_changes(args.limit),
        what="apply due plan changes",
    )
    print(f"Applied {applied} due plan change(s).")


def _cmd_config_export(args: argparse.Namespace) -> None:
    from bursar.config import BursarConfig

    store = _store_from_env()
    result = _retry_store_operation(lambda: store.get_catalog_revision(args.version), what="fetch catalog revision")
    if result is None:
        print(f"Version {args.version} not found.", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(BursarConfig.model_validate(result.config).model_dump(mode="json"), indent=2))


def _cmd_config_diff(args: argparse.Namespace) -> None:
    from bursar.config import BursarConfig

    store = _store_from_env()

    def _fetch() -> tuple[CatalogRevision | None, CatalogRevision | None]:
        return store.get_catalog_revision(args.version_a), store.get_catalog_revision(args.version_b)

    a, b = _retry_store_operation(_fetch, what="fetch catalog revisions")
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
    """Print the Bursar config JSON Schema for editors and validation."""
    from bursar.config import BursarConfig

    print(json.dumps(BursarConfig.model_json_schema(), indent=2))


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="bursar",
        description="Bursar SDK: database migrations and catalog management.",
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
        help="JSON/YAML Bursar config file, or '-' for stdin",
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

    # config
    p_config = sub.add_parser(
        "config",
        help="Manage Bursar config",
        description="Manage immutable Bursar catalog versions.",
    )
    psub = p_config.add_subparsers(dest="subcommand", metavar="<subcommand>")

    p_set = psub.add_parser("set", help="Apply config (always creates a new version)")
    p_set.add_argument("file", help="JSON/YAML Bursar config file, or '-' for stdin")
    p_set.add_argument("--label", default=None, help="Optional label/message for this version")
    p_set.add_argument(
        "--rollout",
        default=None,
        metavar="FILE",
        help="Optional JSON/YAML per-release rollout manifest",
    )
    p_set.set_defaults(func=_cmd_config_set)

    p_get = psub.add_parser("get", help="Show the active Bursar config as JSON")
    p_get.set_defaults(func=_cmd_config_get)

    p_list = psub.add_parser("list", help="List catalog revisions (* = active)")
    p_list.set_defaults(func=_cmd_config_list)

    p_activate = psub.add_parser("activate", help="Switch the active version")
    p_activate.add_argument("version", type=int, help="Version number to activate")
    p_activate.add_argument(
        "--rollout",
        default=None,
        metavar="FILE",
        help="Optional JSON/YAML per-release rollout manifest",
    )
    p_activate.set_defaults(func=_cmd_config_activate)

    p_pin = psub.add_parser(
        "pin",
        help="Pin or unpin one subject's current plan revision",
    )
    p_pin.add_argument("subject_id", help="Subject UUID")
    p_pin.add_argument(
        "--unpin",
        action="store_true",
        help="Remove the pin; re-activate the catalog explicitly to catch up",
    )
    p_pin.set_defaults(func=_cmd_config_pin)

    p_apply_due = psub.add_parser(
        "apply-due",
        help="Apply renewal-effective plan changes that are now due",
    )
    p_apply_due.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum assignments to process (1-1000; default: 100)",
    )
    p_apply_due.set_defaults(func=_cmd_config_apply_due)

    p_validate = psub.add_parser("validate", help="Validate a Bursar config without applying it")
    p_validate.add_argument("file", help="JSON/YAML Bursar config file, or '-' for stdin")
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

    p_config.set_defaults(_config_parser=p_config)
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
        sub_parser = getattr(args, "_config_parser", parser)
        sub_parser.print_help(sys.stderr)
        raise SystemExit(1)

    args.func(args)


if __name__ == "__main__":
    main()
