"""Async billing helpers must not run synchronous stores on the event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast
from unittest.mock import Mock

import pytest

from bursar.credits.service import CreditsService
from bursar.credits.service_types import RunBilledAsyncOptions
from bursar.credits.types import DeductionResult
from bursar.retry import retry_bursar_operation


def _deduction() -> DeductionResult:
    return DeductionResult(
        entry_id="entry-1",
        user_id="user-1",
        amount=Decimal(1),
        balance_after=Decimal(9),
        allowance_consumed=Decimal(0),
        idempotent=False,
    )


@pytest.mark.asyncio
async def test_run_billed_async_offloads_reservation_and_settlement(monkeypatch) -> None:
    service = cast(Any, object.__new__(CreditsService))
    operation = Mock()
    operation.settle.return_value = _deduction()
    begin = Mock(return_value=operation)
    service.begin_billed_operation = begin
    offloaded: list[Callable[..., Any]] = []

    async def tracked_to_thread(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracked_to_thread)

    async def work() -> tuple[str, Decimal]:
        return "done", Decimal(1)

    result = await CreditsService.run_billed_async(
        service,
        "user-1",
        RunBilledAsyncOptions(
            estimate=Decimal(1),
            operation_key="job:1",
            do_work=work,
        ),
    )

    assert result.result == "done"
    assert offloaded == [begin, retry_bursar_operation]
    operation.settle.assert_called_once_with(Decimal(1))


@pytest.mark.asyncio
async def test_run_billed_async_offloads_release_after_work_failure(monkeypatch) -> None:
    service = cast(Any, object.__new__(CreditsService))
    operation = Mock()
    begin = Mock(return_value=operation)
    service.begin_billed_operation = begin
    offloaded: list[Callable[..., Any]] = []

    async def tracked_to_thread(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracked_to_thread)

    async def work() -> tuple[str, Decimal]:
        raise RuntimeError("work failed")

    with pytest.raises(RuntimeError, match="work failed"):
        await CreditsService.run_billed_async(
            service,
            "user-1",
            RunBilledAsyncOptions(
                estimate=Decimal(1),
                operation_key="job:2",
                do_work=work,
            ),
        )

    assert offloaded == [begin, operation.release]
    operation.release.assert_called_once_with()
