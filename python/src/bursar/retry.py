"""Bounded retry helpers driven by Bursar's canonical error taxonomy."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from bursar.errors import is_retryable_bursar_error


@dataclass(frozen=True, slots=True)
class BursarRetryOptions:
    """Retry policy for a replay-safe Bursar operation."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0


def retry_bursar_operation[R](
    operation: Callable[..., R],
    *args: Any,
    retry_options: BursarRetryOptions | None = None,
    **kwargs: Any,
) -> R:
    """Run a synchronous operation, retrying only SDK-classified transient failures."""

    options = retry_options or BursarRetryOptions()
    max_attempts = max(1, options.max_attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            return operation(*args, **kwargs)
        except Exception as error:
            if attempt >= max_attempts or not is_retryable_bursar_error(error):
                raise
            delay = min(
                max(options.base_delay_seconds, options.max_delay_seconds),
                max(0.0, options.base_delay_seconds) * 2 ** (attempt - 1),
            )
            if delay > 0:
                time.sleep(delay)
    raise AssertionError("unreachable")


async def retry_bursar_operation_async[R](
    operation: Callable[..., Awaitable[R]],
    *args: Any,
    retry_options: BursarRetryOptions | None = None,
    **kwargs: Any,
) -> R:
    """Async counterpart to :func:`retry_bursar_operation`."""

    options = retry_options or BursarRetryOptions()
    max_attempts = max(1, options.max_attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation(*args, **kwargs)
        except Exception as error:
            if attempt >= max_attempts or not is_retryable_bursar_error(error):
                raise
            delay = min(
                max(options.base_delay_seconds, options.max_delay_seconds),
                max(0.0, options.base_delay_seconds) * 2 ** (attempt - 1),
            )
            if delay > 0:
                await asyncio.sleep(delay)
    raise AssertionError("unreachable")


__all__ = [
    "BursarRetryOptions",
    "retry_bursar_operation",
    "retry_bursar_operation_async",
]
