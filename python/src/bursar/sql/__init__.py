"""Bundled SQL migrations for bursar."""

from pathlib import Path

_SQL_DIR = Path(__file__).resolve().parent


def _get_sql_files() -> list[Path]:
    """Return bundled SQL file paths in order by leading numeric prefix."""
    return sorted(
        _SQL_DIR.glob("[0-9]*.sql"),
        key=lambda p: int(p.stem.split("_", 1)[0]),
    )
