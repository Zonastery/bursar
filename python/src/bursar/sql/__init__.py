"""Bundled, grouped SQL migrations for bursar."""

import re
from pathlib import Path

_SQL_DIR = Path(__file__).resolve().parent
_MIGRATION_NAME = re.compile(r"^(?P<prefix>[0-9]{3})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")


def _get_sql_files() -> list[Path]:
    """Return bundled SQL migration file paths in apply order.

    Every migration must use a canonical ``NNN_snake_case.sql`` filename. The
    numeric prefixes are unique and contiguous from ``001`` so adding a bad or
    ambiguously ordered migration fails before any database connection is made.

    Migrations are applied transactionally by the ``bursar migrate`` CLI and
    tracked in ``bursar.schema_migrations`` with a SHA-256 checksum. Reusing a
    version with changed contents is rejected instead of silently replayed.
    """
    files = sorted(_SQL_DIR.glob("*.sql"), key=lambda path: path.name)
    if not files:
        raise RuntimeError("Bursar package contains no SQL migrations")

    prefixes: list[int] = []
    for path in files:
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"Invalid Bursar migration filename: {path.name}")
        prefixes.append(int(match.group("prefix")))

    if len(prefixes) != len(set(prefixes)):
        raise RuntimeError("Bursar migration numeric prefixes must be unique")
    expected = list(range(1, len(files) + 1))
    if prefixes != expected:
        raise RuntimeError(f"Bursar migration prefixes must be contiguous from 001; found {prefixes}")
    return files
