from __future__ import annotations

from uuid import UUID

from bursar.billing.types import BillingCustomerRecord
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_identifier_result,
    validate_non_empty,
)
from bursar.errors import StoreError


class BillingCustomerRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(self, provider: str, provider_customer_id: str, user_id: str, email: str | None) -> None:
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_customer_id, "provider_customer_id")
        rows = self._execute(
            "SELECT bursar.upsert_billing_customer(%s::uuid, %s, %s, %s) AS id",
            [user_id, provider, provider_customer_id, email],
        )
        require_identifier_result(rows, "id", "BillingCustomerRepository.upsert")

    def get(self, provider: str, provider_customer_id: str) -> str | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_customer_by_provider(%s, %s)",
            [provider, provider_customer_id],
        )
        row = optional_mapping_row(rows, "BillingCustomerRepository.get")
        if row is None:
            return None
        try:
            subject_id = str(UUID(str(row.get("subject_id"))))
        except (AttributeError, TypeError, ValueError) as error:
            raise StoreError("BillingCustomerRepository.get: malformed customer row") from error
        return subject_id

    def get_by_user_id(self, user_id: str, provider: str | None = None) -> BillingCustomerRecord | None:
        rows = self._execute("SELECT * FROM bursar.get_billing_customer(%s::uuid, %s)", [user_id, provider])
        row = optional_mapping_row(rows, "BillingCustomerRepository.get_by_user_id")
        if row is None:
            return None
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("provider"), str)
            or not row["provider"]
            or not isinstance(row.get("provider_customer_id"), str)
            or not row["provider_customer_id"]
        ):
            raise StoreError("BillingCustomerRepository.get_by_user_id: malformed customer row")
        return BillingCustomerRecord(
            provider=row["provider"],
            provider_customer_id=row["provider_customer_id"],
        )
