from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import BillingEventRow

QueryFn = Callable[[str, list[Any]], list[Any]]


class BillingEventRepository:
    def __init__(self, execute: QueryFn) -> None:
        self._execute = execute

    def claim(
        self,
        provider: str,
        event_id: str,
        event_type: str,
        metadata: str,
    ) -> BillingEventRow | None:
        row = self._unwrap_jsonb(
            self._execute(
                "SELECT * FROM public.claim_billing_event(%s, %s, %s, %s)",
                [provider, event_id, event_type, metadata],
            )
        )
        return BillingEventRow.model_validate(row) if row else None

    def complete(self, provider: str, event_id: str) -> None:
        self._execute("SELECT * FROM public.complete_billing_event(%s, %s)", [provider, event_id])

    def fail(self, provider: str, event_id: str) -> None:
        self._execute("SELECT * FROM public.fail_billing_event(%s, %s)", [provider, event_id])

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
