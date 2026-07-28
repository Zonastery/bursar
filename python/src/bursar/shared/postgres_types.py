"""Postgres type definitions — mirrors JS SDK's ``shared/postgres-types.ts``."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

QueryFn = Callable[..., Any]
AsyncQueryFn = Callable[..., Coroutine[Any, Any, Any]]
