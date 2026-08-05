"""Bounded retry helpers driven by Bursar's canonical error taxonomy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from typing import Any

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    stop_any,
    wait_exponential,
    wait_random_exponential,
)

from bursar.errors import is_retryable_bursar_error


@dataclass(frozen=True, slots=True)
class BursarRetryOptions:
    """Retry policy for a replay-safe Bursar operation."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    factor: float = 2.0
    jitter: bool = True
    max_elapsed_seconds: float = 30.0
    should_retry: Callable[[BaseException], bool] | None = None
    on_retry: Callable[[BaseException, int, float], None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        _finite_non_negative(self.base_delay_seconds, "base_delay_seconds")
        _finite_non_negative(self.max_delay_seconds, "max_delay_seconds")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be greater than or equal to base_delay_seconds")
        if not isfinite(self.factor) or self.factor <= 0:
            raise ValueError("factor must be a finite positive number")
        if not isinstance(self.jitter, bool):
            raise TypeError("jitter must be a boolean")
        _finite_non_negative(self.max_elapsed_seconds, "max_elapsed_seconds")
        if self.should_retry is not None and not callable(self.should_retry):
            raise TypeError("should_retry must be callable")
        if self.on_retry is not None and not callable(self.on_retry):
            raise TypeError("on_retry must be callable")


def _finite_non_negative(value: float, name: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")


def _retry_decider(options: BursarRetryOptions) -> Callable[[BaseException], bool]:
    return options.should_retry or is_retryable_bursar_error


def _before_sleep(options: BursarRetryOptions, state: RetryCallState) -> None:
    if options.on_retry is None or state.outcome is None:
        return
    error = state.outcome.exception()
    if error is None:
        return
    delay = state.next_action.sleep if state.next_action is not None else 0.0
    options.on_retry(error, state.attempt_number + 1, delay)


def _retrying(options: BursarRetryOptions) -> Retrying:
    wait = (
        wait_random_exponential(
            multiplier=options.base_delay_seconds,
            exp_base=options.factor,
            max=options.max_delay_seconds,
        )
        if options.jitter
        else wait_exponential(
            multiplier=options.base_delay_seconds,
            exp_base=options.factor,
            max=options.max_delay_seconds,
        )
    )
    return Retrying(
        retry=retry_if_exception(_retry_decider(options)),
        wait=wait,
        stop=stop_any(stop_after_attempt(options.max_attempts), stop_after_delay(options.max_elapsed_seconds)),
        before_sleep=lambda state: _before_sleep(options, state),
        reraise=True,
    )


def _async_retrying(options: BursarRetryOptions) -> AsyncRetrying:
    wait = (
        wait_random_exponential(
            multiplier=options.base_delay_seconds,
            exp_base=options.factor,
            max=options.max_delay_seconds,
        )
        if options.jitter
        else wait_exponential(
            multiplier=options.base_delay_seconds,
            exp_base=options.factor,
            max=options.max_delay_seconds,
        )
    )
    return AsyncRetrying(
        retry=retry_if_exception(_retry_decider(options)),
        wait=wait,
        stop=stop_any(stop_after_attempt(options.max_attempts), stop_after_delay(options.max_elapsed_seconds)),
        before_sleep=lambda state: _before_sleep(options, state),
        reraise=True,
    )


def retry_bursar_operation[R](
    operation: Callable[..., R],
    *args: Any,
    retry_options: BursarRetryOptions | None = None,
    **kwargs: Any,
) -> R:
    """Run a synchronous operation, retrying only SDK-classified transient failures."""

    options = retry_options or BursarRetryOptions()
    if not callable(operation):
        raise TypeError("operation must be callable")
    return _retrying(options)(operation, *args, **kwargs)


async def retry_bursar_operation_async[R](
    operation: Callable[..., Awaitable[R]],
    *args: Any,
    retry_options: BursarRetryOptions | None = None,
    **kwargs: Any,
) -> R:
    """Async counterpart to :func:`retry_bursar_operation`."""

    options = retry_options or BursarRetryOptions()
    if not callable(operation):
        raise TypeError("operation must be callable")
    retrying = _async_retrying(options)
    async for attempt in retrying:
        with attempt:
            return await operation(*args, **kwargs)
    raise AssertionError("unreachable")


__all__ = [
    "BursarRetryOptions",
    "retry_bursar_operation",
    "retry_bursar_operation_async",
]
