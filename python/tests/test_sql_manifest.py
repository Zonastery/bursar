"""Unit checks for the packaged SQL migration manifest."""

from pathlib import Path

import pytest

from bursar import sql


def _write_migrations(directory: Path, names: list[str]) -> None:
    for name in names:
        (directory / name).write_text("SELECT 1;\n", encoding="utf-8")


def test_sql_manifest_returns_canonical_contiguous_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_migrations(tmp_path, ["002_second.sql", "001_first.sql"])
    monkeypatch.setattr(sql, "_SQL_DIR", tmp_path)

    assert [path.name for path in sql._get_sql_files()] == ["001_first.sql", "002_second.sql"]


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["migration.sql"], "Invalid Bursar migration filename"),
        (["001_first.sql", "001_duplicate.sql"], "prefixes must be unique"),
        (["001_first.sql", "003_gap.sql"], "prefixes must be contiguous"),
    ],
)
def test_sql_manifest_rejects_ambiguous_or_incomplete_sequences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    names: list[str],
    message: str,
) -> None:
    _write_migrations(tmp_path, names)
    monkeypatch.setattr(sql, "_SQL_DIR", tmp_path)

    with pytest.raises(RuntimeError, match=message):
        sql._get_sql_files()
