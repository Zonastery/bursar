from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import BillingTopupRow

QueryFn = Callable[[str, list[Any]], list[Any]]


class BillingTopupRepository:
    def __init__(self, execute: QueryFn) -> None:
        self._execute = execute

    def resolve_by_price(
        self,
        provider: str,
        price_id: str | None,
        product_id: str | None,
    ) -> BillingTopupRow | None:
        row = self._unwrap_jsonb(
            self._execute(
                "SELECT * FROM public.resolve_credit_topup_by_price(%s, %s, %s)",
                [provider, price_id, product_id],
            )
        )
        return BillingTopupRow.model_validate(row) if row else None

    def resolve_by_lookup(self, provider: str, lookup_key: str) -> BillingTopupRow | None:
        row = self._unwrap_jsonb(
            self._execute(
                "SELECT * FROM public.resolve_credit_topup_by_lookup(%s, %s)",
                [provider, lookup_key],
            )
        )
        return BillingTopupRow.model_validate(row) if row else None

    @staticmethod
    def _unwrap_jsonb(rows: list[Any]) -> dict[str, Any] | None:
        if not rows or len(rows) != 1:
            return None
        row = rows[0]
        if isinstance(row, dict):
            keys = list(row.keys())
            if len(keys) == 1:
                v = row[keys[0]]
                if isinstance(v, dict):
                    return v
            return row
        return None
