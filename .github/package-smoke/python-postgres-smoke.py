"""Exercise the installed wheel, its packaged SQL, and its PostgreSQL store."""

from __future__ import annotations

import os
from decimal import Decimal

from bursar.credits.postgres.store import PostgresStore
from bursar.credits.service import CreditsService

TENANT_ID = "00000000-0000-4000-8000-000000000201"
SUBJECT_ID = "00000000-0000-4000-8000-000000000211"


def main() -> None:
    store = PostgresStore(
        os.environ["DATABASE_URL"],
        tenant_id=TENANT_ID,
        provider_environment="test",
    )
    try:
        service = CreditsService(store=store)
        active = service.get_active_catalog()
        assert active is not None and active.version == 1

        first = service.add_credits(
            SUBJECT_ID,
            Decimal("7"),
            entry_type="purchase",
            idempotency_key="package-smoke:python:grant",
        )
        replay = service.add_credits(
            SUBJECT_ID,
            Decimal("7"),
            entry_type="purchase",
            idempotency_key="package-smoke:python:grant",
        )
        assert replay.entry_id == first.entry_id
        assert service.get_balance(SUBJECT_ID).balance == Decimal("7")
    finally:
        store.close()


if __name__ == "__main__":
    main()
