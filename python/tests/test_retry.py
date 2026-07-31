from __future__ import annotations

import pytest

from bursar import (
    BursarRetryOptions,
    CapReachedError,
    StoreError,
    is_retryable_bursar_error,
    retry_bursar_operation,
)


def test_retry_taxonomy_and_executor() -> None:
    attempts = 0

    def transient() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StoreError("temporary")
        return "ok"

    assert is_retryable_bursar_error(StoreError("temporary")) is True
    assert is_retryable_bursar_error(CapReachedError("permanent")) is False
    assert (
        retry_bursar_operation(
            transient,
            retry_options=BursarRetryOptions(max_attempts=3, base_delay_seconds=0),
        )
        == "ok"
    )
    assert attempts == 2

    permanent_attempts = 0

    def permanent() -> None:
        nonlocal permanent_attempts
        permanent_attempts += 1
        raise CapReachedError("permanent")

    with pytest.raises(CapReachedError):
        retry_bursar_operation(
            permanent,
            retry_options=BursarRetryOptions(max_attempts=3, base_delay_seconds=0),
        )
    assert permanent_attempts == 1
