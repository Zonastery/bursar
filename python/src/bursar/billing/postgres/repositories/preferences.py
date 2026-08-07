from __future__ import annotations

from uuid import UUID

from bursar.billing.types import BillingPreferences
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_boolean_result,
    validate_non_empty,
)
from bursar.errors import StoreError


class BillingPreferencesRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def get(self, user_id: str) -> BillingPreferences | None:
        validate_non_empty(user_id, "user_id")
        rows = self._execute("SELECT * FROM bursar.get_billing_preferences(%s::uuid)", [user_id])
        row = optional_mapping_row(rows, "BillingPreferencesRepository.get")
        if row is None or row.get("subject_id") is None:
            return None
        try:
            subject_id = str(UUID(str(row["subject_id"])))
        except (AttributeError, TypeError, ValueError) as error:
            raise StoreError("BillingPreferencesRepository.get: malformed subject identifier") from error
        boolean_fields = (
            "auto_recharge",
            "overage_protection",
            "email_notifications",
            "usage_alerts",
            "invoice_reminders",
        )
        if any(type(row.get(field)) is not bool for field in boolean_fields):
            raise StoreError("BillingPreferencesRepository.get: malformed boolean field")
        return BillingPreferences(
            user_id=subject_id,
            auto_recharge=row["auto_recharge"],
            overage_protection=row["overage_protection"],
            email_notifications=row["email_notifications"],
            usage_alerts=row["usage_alerts"],
            invoice_reminders=row["invoice_reminders"],
        )

    def upsert(self, prefs: BillingPreferences) -> None:
        user_id = prefs.user_id
        validate_non_empty(user_id, "user_id")
        rows = self._execute(
            "SELECT bursar.upsert_billing_preferences(%s::uuid, %s, %s, %s, %s, %s) AS updated",
            [
                user_id,
                prefs.auto_recharge,
                prefs.overage_protection,
                prefs.email_notifications,
                prefs.usage_alerts,
                prefs.invoice_reminders,
            ],
        )
        if not require_boolean_result(rows, "updated", "BillingPreferencesRepository.upsert"):
            raise StoreError(
                "BillingPreferencesRepository.upsert: update was rejected",
                indeterminate=True,
            )
