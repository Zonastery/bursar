from __future__ import annotations

from typing import Any

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty


class BillingPreferencesRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def get(self, user_id: str) -> dict[str, Any] | None:
        validate_non_empty(user_id, "user_id")
        rows = self._execute("SELECT * FROM bursar.get_billing_preferences(%s::uuid)", [user_id])
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def upsert(self, prefs: dict[str, Any]) -> None:
        user_id = str(prefs.get("user_id", ""))
        validate_non_empty(user_id, "user_id")
        self._execute(
            "SELECT bursar.upsert_billing_preferences(%s::uuid, %s, %s, %s, %s, %s)",
            [
                user_id,
                prefs.get("auto_recharge", False),
                prefs.get("overage_protection", True),
                prefs.get("email_notifications", True),
                prefs.get("usage_alerts", True),
                prefs.get("invoice_reminders", False),
            ],
        )
