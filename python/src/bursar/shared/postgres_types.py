"""Postgres type definitions — mirrors JS SDK's ``shared/postgres-types.ts``."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol


class QueryFn(Protocol):
    def __call__(self, text: str, params: list[Any] | None = None) -> list[Any]: ...


class AsyncQueryFn(Protocol):
    def __call__(self, text: str, params: list[Any] | None = None) -> Awaitable[list[Any]]: ...


class CallProc(Protocol):
    def __call__(self, name: str, params: list[Any]) -> list[Any]: ...
