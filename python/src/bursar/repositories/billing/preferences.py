from __future__ import annotations

from typing import Any

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_non_empty


class BillingPreferencesRepository:
    """Repository for billing preferences operations.

    All methods call Postgres via raw SQL queries through the query function.
    Returns None when the query returns no rows.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def get(self, user_id: str) -> dict[str, Any] | None:
        """Get billing preferences for a user.

        Args:
            user_id: The user ID.

        Returns:
            Dict with preference fields if found, None otherwise.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._execute(
            """SELECT user_id, auto_recharge, overage_protection,
                      email_notifications, usage_alerts, invoice_reminders, usage_limit_alerts
               FROM public.billing_preferences WHERE user_id = %s""",
            [user_id],
        )
        if not rows:
            return None
        row = rows[0]
        return row if isinstance(row, dict) else None

    def upsert(self, prefs: dict[str, Any]) -> None:
        """Insert or update billing preferences.

        Args:
            prefs: Dict with user_id and preference fields.
        """
        validate_non_empty(str(prefs.get("user_id", "")), "user_id")
        self._execute(
            """INSERT INTO public.billing_preferences (
                   user_id, auto_recharge, overage_protection,
                   email_notifications, usage_alerts, invoice_reminders, usage_limit_alerts
               )
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                   auto_recharge = COALESCE(EXCLUDED.auto_recharge,
                       billing_preferences.auto_recharge),
                   overage_protection = COALESCE(EXCLUDED.overage_protection,
                       billing_preferences.overage_protection),
                   email_notifications = COALESCE(EXCLUDED.email_notifications,
                       billing_preferences.email_notifications),
                   usage_alerts = COALESCE(EXCLUDED.usage_alerts,
                       billing_preferences.usage_alerts),
                   invoice_reminders = COALESCE(EXCLUDED.invoice_reminders,
                       billing_preferences.invoice_reminders),
                   usage_limit_alerts = COALESCE(EXCLUDED.usage_limit_alerts,
                       billing_preferences.usage_limit_alerts),
                   updated_at = now()""",
            [
                prefs["user_id"],
                prefs.get("auto_recharge", False),
                prefs.get("overage_protection", True),
                prefs.get("email_notifications", True),
                prefs.get("usage_alerts", True),
                prefs.get("invoice_reminders", False),
                prefs.get("usage_limit_alerts", True),
            ],
        )
