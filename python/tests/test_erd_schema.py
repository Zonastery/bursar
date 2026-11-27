"""The committed ERD must describe the migrated PostgreSQL catalog."""

import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_erd_matches_migrated_database(migrated_pg_database_url: str) -> None:
    generator = runpy.run_path(str(ROOT / "docs/scripts/generate_erd.py"))
    diagram = generator["render"](migrated_pg_database_url)
    document = (ROOT / "docs/docs/concepts/database-schema.md").read_text()
    assert "```mermaid\n" + diagram + "\n```" in document
    assert "tenant_id, subject_id to tenant_id, id" in diagram

    assert 'catalog_topups |o--o{ billing_payments : "tenant_id, topup_id to tenant_id, id"' in diagram
    assert 'subjects ||--o{ billing_payments : "tenant_id, subject_id to tenant_id, id"' in diagram
    assert 'credit_accounts ||--o| account_plan_assignments : "tenant_id, account_id to tenant_id, id"' in diagram
