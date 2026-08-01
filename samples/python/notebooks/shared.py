"""Shared helper for bursar notebooks.

Starts a temporary Postgres cluster, runs Bursar migrations, and returns a
configured ``PostgresStore``.  Requires Postgres binaries on PATH.

Usage::

    from shared import start_postgres_store, cleanup
    store, pgdata = start_postgres_store()
    try:
        # ... bursar operations ...
    finally:
        cleanup(pgdata)
"""

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path


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
