"""Database-level regression coverage for catalog plan evolution."""

from pathlib import Path

import psycopg2
import pytest

pytestmark = [pytest.mark.integration]


def test_plan_evolution_rollouts_preserve_assignment_state(
    pg_database_url: str,
) -> None:
    sql = Path(__file__).with_name("sql_plan_evolution_regressions.sql").read_text(encoding="utf-8")
    connection = psycopg2.connect(pg_database_url)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql)
    finally:
        connection.close()
