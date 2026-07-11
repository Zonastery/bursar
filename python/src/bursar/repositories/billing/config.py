from __future__ import annotations

from collections.abc import Callable
from typing import Any

QueryFn = Callable[[str, list[Any]], list[Any]]


class BillingConfigRepository:
    def __init__(self, execute: QueryFn) -> None:
        self._execute = execute

    def sync_from_config(self, config_json: str) -> None:
        self._execute(
            "SELECT public.sync_billing_from_config(%s::jsonb)",
            [config_json],
        )
