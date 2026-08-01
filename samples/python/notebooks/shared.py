"""Shared helpers for the bursar notebook series.

Every notebook in this series tells one chapter of the same story: you are
building *prompta*, an AI assistant SaaS, and integrating bursar to meter
usage, sell credits, and bill your users.

Helpers here:

- ``start_postgres_store`` / ``cleanup`` — spin up a throwaway Postgres
  cluster with the bursar schema migrated and one tenant provisioned.
- ``base_config`` — the canonical bursar configuration used across the
  series (models, rate cards, plans, offers). Notebooks mutate a copy.
- ``publish_config`` — validate + publish + activate a config on a store.
- ``USER_*`` — the recurring demo users of the story.
"""

import copy
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bursar import Bursar

# --------------------------------------------------------------------------
# The cast.  Every notebook refers to the same users so the story is coherent.
# --------------------------------------------------------------------------
USER_ADA = "11111111-1111-4111-8111-111111111111"   # individual Pro subscriber
USER_ALEX = "22222222-2222-4222-8222-222222222222"  # team member at Acme
USER_JAMAL = "33333333-3333-4333-8333-333333333333"  # team member at Acme
TEAM_ACME = "44444444-4444-4444-8444-444444444444"  # Acme's team credit pool


# --------------------------------------------------------------------------
# The canonical config.  One strict, versioned document drives pricing,
# credits, plans, entitlements, admission, and commerce for the whole SaaS.
# --------------------------------------------------------------------------
def base_config() -> dict:
    """Return a fresh copy of the canonical demo configuration.

    Notebooks that need a variation (e.g. adding a quota or an offer) mutate
    the copy instead of rebuilding the whole document.
    """
    return copy.deepcopy(_BASE_CONFIG)


_BASE_CONFIG = {
    "version": 1,
    "catalog": {"default_plan": "free"},
    "pricing": {
        "operations": {
            "completion": {
                "measures": {
                    "input_tokens": {"unit": "token"},
                    "output_tokens": {"unit": "token"},
                    "cache_read_tokens": {"unit": "token"},
                },
                "dimensions": {"model": {"type": "string"}},
            },
            "execution": {
                "measures": {
                    "jobs": {"unit": "job"},
                    "compute_seconds": {"unit": "second"},
                },
                "dimensions": {"model": {"type": "string"}},
            },
        },
        "rate_cards": {
            "standard": {
                "operations": {
                    "completion": {
                        "rules": [
                            {
                                "when": {
                                    "model": {
                                        "op": "in",
                                        "values": ["gpt-4o", "gpt-4o-mini"],
                                    }
                                },
                                "charge": {
                                    "type": "sum",
                                    "components": [
                                        {
                                            "type": "per_unit",
                                            "measure": "input_tokens",
                                            "rate": "0.0025",
                                            "unit_size": "1000000",
                                        },
                                        {
                                            "type": "per_unit",
                                            "measure": "output_tokens",
                                            "rate": "0.0100",
                                            "unit_size": "1000000",
                                        },
                                        {
                                            "type": "per_unit",
                                            "measure": "cache_read_tokens",
                                            "rate": "0.00125",
                                            "unit_size": "1000000",
                                        },
                                    ],
                                },
                            }
                        ],
                        "unmatched": {
                            "action": "charge",
                            "charge": {
                                "type": "expression",
                                "formula": "input_tokens * 0.005 + output_tokens * 0.015",
                            },
                        },
                    },
                    "execution": {
                        "rules": [
                            {
                                "when": {"model": {"op": "eq", "value": "gpt-4o"}},
                                "charge": {
                                    "type": "per_unit",
                                    "measure": "jobs",
                                    "rate": "0.04",
                                },
                            }
                        ],
                        "unmatched": {"action": "reject"},
                    },
                }
            }
        },
    },
    "credits": {
        "buckets": {
            "promotional": {"priority": 1},
            "purchased": {"priority": 10},
        },
        "default_bucket": "purchased",
    },
    "entitlements": {
        "features": {
            "voice_mode": {"type": "boolean", "default": False},
            "max_context": {
                "type": "integer",
                "default": 128000,
                "minimum": 8000,
                "maximum": 200000,
            },
        }
    },
    "admission": {"policies": {"default": {"max_in_flight": 4}}},
    "plans": {
        "free": {
            "display_name": "Free",
            "rank": 0,
            "rate_card": "standard",
            "allowed_operations": ["completion"],
            "credit_allowance": {
                "amount": "10000",
                "window": {"type": "calendar", "unit": "month", "count": 1},
            },
        },
        "pro": {
            "display_name": "Pro",
            "rank": 1,
            "rate_card": "standard",
            "allowed_operations": ["completion", "execution"],
            "features": {"voice_mode": True, "max_context": 200000},
            "quotas": {
                "daily_tokens": {
                    "operation": "completion",
                    "measure": "output_tokens",
                    "limit": "500000",
                    "window": {"type": "calendar", "unit": "day", "count": 1},
                    "enforcement": "block",
                    "emit_at_percent": [80, 100],
                }
            },
            "admission_policy": "default",
        },
    },
    "commerce": {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "pro_monthly": {
                "type": "subscription",
                "display_name": "Pro Monthly",
                "description": "Pro plan with a 50,000-credit monthly grant",
                "price": {"amount_minor": 2000, "currency": "USD", "tax_behavior": "unspecified"},
                "providers": {"stripe": {"type": "stripe_price", "price_id": "price_pro_monthly"}},
                "plan": "pro",
                "billing_interval": {"unit": "month", "count": 1},
                "cycle_grant": {
                    "amount": "50000",
                    "bucket": "purchased",
                    "renewal": "replace_previous",
                },
            },
            "credits_10k": {
                "type": "topup",
                "display_name": "10,000 Credits",
                "description": "Prepaid credit pack",
                "price": {"amount_minor": 500, "currency": "USD", "tax_behavior": "unspecified"},
                "providers": {"stripe": {"type": "stripe_price", "price_id": "price_credits_10k"}},
                "credits_per_unit": "10000",
                "quantity": {"minimum": 1, "maximum": 10, "default": 1},
                "bucket": "purchased",
                "lot_behavior": "separate_lots",
            },
        },
        "auto_recharge": {
            "eligible_topups": ["credits_10k"],
            "balance_below": {"minimum": "1000", "maximum": "5000", "default": "2000"},
            "rearm_above": "20000",
            "quantity": {"minimum": 1, "maximum": 10, "default": 1},
            "limits": {
                "max_purchases": 5,
                "window": {"type": "calendar", "unit": "day", "count": 1},
                "max_charge_minor": 5000,
                "cooldown": {"unit": "hour", "count": 1},
                "max_consecutive_failures": 3,
                "failure_action": "pause",
            },
        },
    },
}


def publish_config(store, config: dict, label: str = "notebooks") -> "Bursar":
    """Validate, publish, and activate ``config`` on ``store``'s tenant.

    Returns a ready-to-use ``Bursar`` facade bound to the same store.
    """
    from bursar import Bursar

    bursar = Bursar.create(credit_store=store)
    bursar.catalog.publish_and_activate(config, label=label)
    return bursar


# --------------------------------------------------------------------------
# Throwaway Postgres cluster (schema + tenant + store).
# --------------------------------------------------------------------------
def _find_pg() -> str:
    pg_ctl = shutil.which("pg_ctl")
    if not pg_ctl or not shutil.which("initdb") or not shutil.which("createdb"):
        raise RuntimeError(
            "Postgres binaries not found. Install:\n"
            "  macOS: brew install postgresql@17\n"
            "  Ubuntu/Debian: sudo apt install postgresql\n"
            "  Fedora: sudo dnf install postgresql-server"
        )
    return str(Path(pg_ctl).parent)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _preseed_supabase_objects(dsn: str) -> None:
    """Create the minimal Supabase objects bursar's SQL expects (idempotent).

    Mirrors what hosted Supabase provides automatically: the ``auth`` schema
    with ``uid()``/``role()``, a minimal ``auth.users`` table, the standard
    roles, and the platform-level grants. Without this, ``run_migrations``
    refuses to bootstrap on a bare Postgres.
    """
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS auth")
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
                LANGUAGE sql STABLE
                AS $$ SELECT coalesce(
                    nullif(current_setting('request.jwt.claim.sub', true), ''),
                    current_setting('request.jwt.claims', true)::jsonb ->> 'sub'
                )::uuid $$;
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION auth.role() RETURNS text
                LANGUAGE sql STABLE
                AS $$ SELECT coalesce(
                    nullif(current_setting('request.jwt.claim.role', true), ''),
                    'service_role'
                ) $$;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS auth.users (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    email TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            for role in ("anon", "authenticated", "service_role"):
                try:
                    cur.execute(f"CREATE ROLE {role}")
                except Exception:
                    conn.rollback()
            cur.execute("ALTER ROLE service_role BYPASSRLS")
            cur.execute("GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role")
            cur.execute("GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role")
            cur.execute("ALTER ROLE service_role CREATEROLE")
    finally:
        conn.close()


def start_postgres_store(pgdata: str | None = None) -> tuple:
    """Start a temporary Postgres cluster, run bursar schema setup.

    Returns
    -------
    tuple[PostgresStore, str]
        ``(store, pgdata_path)``.  Caller **must** call ``cleanup(pgdata_path)``
        when done (e.g. in a ``finally`` block or final notebook cell).
    """
    from bursar.credits.postgres.store import PostgresStore, run_migrations

    pg_bin = _find_pg()
    pgdata = pgdata or tempfile.mkdtemp(prefix="bursar_demo_")
    port = str(_free_port())
    user = os.environ.get("USER", os.environ.get("USERNAME", "postgres"))
    pg_ctl = os.path.join(pg_bin, "pg_ctl")

    print("Initialising Postgres cluster …")
    subprocess.run(
        [os.path.join(pg_bin, "initdb"), "-D", pgdata, "-E", "UTF8", "--no-locale"],
        check=True,
        capture_output=True,
    )

    with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
        f.write(f"port={port}\nlisten_addresses='localhost'\n")
    with open(os.path.join(pgdata, "pg_hba.conf"), "w") as f:
        f.write("local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n")

    subprocess.run(
        [pg_ctl, "start", "-w", "-D", pgdata, "-l", os.path.join(pgdata, "log")],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [os.path.join(pg_bin, "createdb"), "-h", "localhost", "-p", port, "bursar_demo"],
        check=True,
        capture_output=True,
    )

    dsn = f"host=localhost port={port} dbname=bursar_demo user={user}"
    _preseed_supabase_objects(dsn)
    run_migrations(dsn)
    tenant_id = os.environ.get(
        "BURSAR_TENANT_ID",
        "00000000-0000-0000-0000-000000000001",
    )
    import psycopg2

    with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT bursar.create_tenant(%s, %s, %s)",
            (tenant_id, "notebook-demo", "Notebook demo"),
        )
    store = PostgresStore(dsn, tenant_id=tenant_id)
    return store, pgdata


def cleanup(pgdata: str) -> None:
    """Stop the Postgres cluster and remove the data directory."""
    if not pgdata or not Path(pgdata).is_dir():
        return
    pg_bin = _find_pg()
    subprocess.run(
        [os.path.join(pg_bin, "pg_ctl"), "stop", "-D", pgdata],
        capture_output=True,
    )
    shutil.rmtree(pgdata, ignore_errors=True)
    print("Cleaned up.")
