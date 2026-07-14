"""Bundled SQL migrations for bursar."""

from pathlib import Path

_SQL_DIR = Path(__file__).resolve().parent


def _get_sql_files() -> list[Path]:
    """Return bundled SQL migration file paths in apply order.

    Conventions
    -----------
    - Each migration file MUST have a unique leading ``NNN_`` numeric prefix
      (zero-padded to the same width — currently 3 digits). A duplicate prefix
      (which caused a non-deterministic apply order between the Python and JS
      harnesses) is treated as a sequencing bug.
    - The JS harness (``javascript/tests/helpers/bootstrap.ts``) sorts the same
      directory with ``readdirSync(...).sort()`` (lexicographic on the full
      filename). To guarantee Python and JS apply migrations in the same order
      for every duplicate-free prefix set, the key here is ``(numeric_prefix,
      full_filename)`` — the lexicographic tie-break mirrors the JS sort.

    Migrations are idempotent (``CREATE OR REPLACE`` / ``IF NOT EXISTS`` /
    ``DO $$ ... EXCEPTION``) and ``PostgresStore.setup()`` re-applies them all
    on every invocation, so renumbering a file does not leave any persistent
    state on a previously-bootstrapped database.
    """
    return sorted(
        _SQL_DIR.glob("[0-9]*.sql"),
        key=lambda p: (int(p.stem.split("_", 1)[0]), p.name),
    )
