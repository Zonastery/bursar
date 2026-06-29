"""ducto CLI — database migrations and pricing-version management.

Built on :mod:`argparse` so flags, ``--help``, exit codes and type coercion are
handled by the stdlib rather than hand-rolled ``argv`` slicing.

Connection secrets are taken from the environment, never the command line:

* ``migrate`` reads ``DATABASE_URL`` (primary). A positional URL is accepted for
  convenience but is discouraged — it leaks via ``ps``/shell history/CI logs.
* ``pricing`` reads ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``.
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
from typing import TYPE_CHECKING, Any, TypeVar

_T = TypeVar("_T")

if TYPE_CHECKING:
    from ducto.interface.models import PricingConfigResult
    from ducto.interface.supabase import HttpxSupabaseStore

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
    "supabase": ["httpx"],
}


def _require_extra(extra: str) -> None:
    """Exit (code 1) with an install hint if any import for *extra* is missing."""
    for mod in _EXTRAS.get(extra, []):
        try:
            __import__(mod)
        except ImportError:
            print(
                f"ducto[{extra}] extra required (missing: {mod}). pip install ducto[{extra}]",
                file=sys.stderr,
            )
            raise SystemExit(1) from None


def _is_transient(exc: Exception) -> bool:
    """True only for the PostgREST schema-cache / connection errors worth retrying."""
    from ducto.interface.base import StoreError

    if not isinstance(exc, StoreError):
        return False
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _retry_transient(op: Callable[[], _T], *, what: str) -> _T:
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
                        "Tip: run 'ducto migrate' and wait for the PostgREST schema cache to refresh.",
                        file=sys.stderr,
                    )
                raise SystemExit(1) from exc
            time.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY)
    raise AssertionError("unreachable")  # pragma: no cover


def _store_from_env() -> HttpxSupabaseStore:
    """Create an :class:`HttpxSupabaseStore` from ``SUPABASE_*`` env vars."""
    _require_extra("supabase")

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url:
        print("SUPABASE_URL required", file=sys.stderr)
        raise SystemExit(1)
    if not key:
        print("SUPABASE_SERVICE_ROLE_KEY required", file=sys.stderr)
        raise SystemExit(1)

    from ducto.interface.supabase import HttpxSupabaseStore

    return HttpxSupabaseStore(url=url, key=key)


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
            print("PyYAML required for .yaml files: pip install ducto[supabase]", file=sys.stderr)
            raise SystemExit(1) from None
        try:
            return yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            print(f"Invalid YAML in {source}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {source}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


# ── Command handlers ─────────────────────────────────────────────────────────


def _cmd_migrate(args: argparse.Namespace) -> None:
    _require_extra("postgres")

    # DATABASE_URL (env) is primary; a positional arg is a discouraged fallback.
    database_url = os.environ.get("DATABASE_URL") or args.database_url
    if args.database_url:
        print(
            "warning: passing the database URL on the command line leaks the password "
            "via 'ps'/shell history/CI logs — prefer the DATABASE_URL env var.",
            file=sys.stderr,
        )
    if not database_url:
        print(
            "No database URL. Set DATABASE_URL (recommended) or pass it positionally:\n"
            "  DATABASE_URL=postgresql://… ducto migrate",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from ducto.interface.supabase import run_migrations

    result = run_migrations(database_url)
    for t in result.tables_created:
        print(f"  ✓ {t}")
    for e in result.errors:
        print(f"  ✗ {e}", file=sys.stderr)

    if result.success:
        print("Migration complete.")
    else:
        print("Migration completed with errors.", file=sys.stderr)
        raise SystemExit(1)


def _cmd_pricing_validate(args: argparse.Namespace) -> None:
    from ducto.config import PricingConfig
    from ducto.interface.models import PricingConfigData

    data = _load_pricing_file(args.file)
    try:
        PricingConfigData.model_validate(data)
        PricingConfig.model_validate(data)
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print("Pricing config is valid.")


def _cmd_pricing_set(args: argparse.Namespace) -> None:
    from ducto.interface.models import PricingConfigData

    data = _load_pricing_file(args.file)
    try:
        config = PricingConfigData.model_validate(data)
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    store = _store_from_env()
    _retry_transient(lambda: store.set_active_pricing(config, label=args.label), what="set pricing")
    print("Pricing config set successfully.")


def _cmd_pricing_get(_args: argparse.Namespace) -> None:
    store = _store_from_env()
    result = _retry_transient(store.get_active_pricing, what="get pricing")
    if result is None:
        print("No active pricing config.", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result.model_dump(mode="json"), indent=2))


def _cmd_pricing_list(_args: argparse.Namespace) -> None:
    store = _store_from_env()
    rows = _retry_transient(store.get_pricing_history, what="list pricing")
    if not rows:
        print("No pricing configs found.", file=sys.stderr)
        raise SystemExit(1)
    for r in rows:
        marker = "*" if r.active else " "
        label = f"  {r.label}" if r.label else ""
        print(f"  {marker} v{r.version}  (id={r.id[:8]}...){label}  {r.created_at[:19]}")


def _cmd_pricing_activate(args: argparse.Namespace) -> None:
    store = _store_from_env()
    _retry_transient(lambda: store.activate_pricing(args.version), what="activate pricing")
    print(f"Pricing v{args.version} activated.")


def _cmd_pricing_export(args: argparse.Namespace) -> None:
    store = _store_from_env()
    result = _retry_transient(lambda: store.get_pricing_config(args.version), what="fetch pricing")
    if result is None:
        print(f"Version {args.version} not found.", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result.config.model_dump(mode="json", exclude_none=True), indent=2))


def _cmd_pricing_diff(args: argparse.Namespace) -> None:
    store = _store_from_env()

    def _fetch() -> tuple[PricingConfigResult | None, PricingConfigResult | None]:
        return store.get_pricing_config(args.version_a), store.get_pricing_config(args.version_b)

    a, b = _retry_transient(_fetch, what="fetch pricing configs")
    if a is None:
        print(f"Version {args.version_a} not found.", file=sys.stderr)
        raise SystemExit(1)
    if b is None:
        print(f"Version {args.version_b} not found.", file=sys.stderr)
        raise SystemExit(1)

    a_json = json.dumps(a.config.model_dump(mode="json"), indent=2)
    b_json = json.dumps(b.config.model_dump(mode="json"), indent=2)
    diff = difflib.unified_diff(
        a_json.splitlines(keepends=True),
        b_json.splitlines(keepends=True),
        fromfile=f"v{args.version_a}",
        tofile=f"v{args.version_b}",
    )
    sys.stdout.writelines(diff)


# ── Parser construction ──────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="ducto",
        description="ducto — credit calculation engine: migrations & pricing management.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # migrate
    p_migrate = sub.add_parser(
        "migrate",
        help="Run database migrations (ducto[postgres])",
        description=(
            "Run bundled SQL migrations. The connection string is read from the "
            "DATABASE_URL environment variable (recommended). A positional URL is "
            "accepted but discouraged because it leaks the password via the process "
            "list, shell history and CI logs."
        ),
    )
    p_migrate.add_argument(
        "database_url",
        nargs="?",
        default=None,
        metavar="DATABASE_URL",
        help="(discouraged) Postgres URL; prefer the DATABASE_URL env var.",
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    # pricing
    p_pricing = sub.add_parser(
        "pricing",
        help="Manage pricing config (ducto[supabase])",
        description="Manage immutable pricing-config versions via the Supabase store.",
    )
    psub = p_pricing.add_subparsers(dest="subcommand", metavar="<subcommand>")

    p_set = psub.add_parser("set", help="Apply config (always creates a new version)")
    p_set.add_argument("file", help="JSON/YAML pricing file, or '-' for stdin")
    p_set.add_argument("--label", default=None, help="Optional label/message for this version")
    p_set.set_defaults(func=_cmd_pricing_set)

    p_get = psub.add_parser("get", help="Show the active pricing config as JSON")
    p_get.set_defaults(func=_cmd_pricing_get)

    p_list = psub.add_parser("list", help="List all pricing versions (* = active)")
    p_list.set_defaults(func=_cmd_pricing_list)

    p_activate = psub.add_parser("activate", help="Switch the active version")
    p_activate.add_argument("version", type=int, help="Version number to activate")
    p_activate.set_defaults(func=_cmd_pricing_activate)

    p_validate = psub.add_parser("validate", help="Validate a pricing file without applying it")
    p_validate.add_argument("file", help="JSON/YAML pricing file, or '-' for stdin")
    p_validate.set_defaults(func=_cmd_pricing_validate)

    p_diff = psub.add_parser("diff", help="Unified diff between two versions")
    p_diff.add_argument("version_a", type=int, help="First version")
    p_diff.add_argument("version_b", type=int, help="Second version")
    p_diff.set_defaults(func=_cmd_pricing_diff)

    p_export = psub.add_parser("export", help="Dump a version as JSON")
    p_export.add_argument("version", type=int, help="Version number to export")
    p_export.set_defaults(func=_cmd_pricing_export)

    p_pricing.set_defaults(_pricing_parser=p_pricing)
    return parser


def main(argv: list[str] | None = None) -> None:
    _load_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        raise SystemExit(1)

    # `pricing` with no subcommand: show its help and exit non-zero.
    if not hasattr(args, "func"):
        sub_parser = getattr(args, "_pricing_parser", parser)
        sub_parser.print_help(sys.stderr)
        raise SystemExit(1)

    args.func(args)


if __name__ == "__main__":
    main()
