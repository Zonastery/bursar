"""Framework-neutral contracts for metered AI provider calls."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from bursar.credits.types import CreditMetadata
from bursar.metrics import UsageMetrics


class ProviderReceipt(BaseModel):
    """The accounting fields from one completed provider call.

    Framework adapters use this compact contract instead of depending on a
    provider SDK's response type. Operational telemetry remains in the host's
    tracing backend; only pricing inputs and financial correlation identifiers
    belong here.
    """

    model_config = ConfigDict(extra="forbid")

    metrics: UsageMetrics
    metadata: CreditMetadata | None = None


class ProviderReceiptSource(Protocol):
    """Request-local bridge to an authoritative provider response."""

    def begin(self) -> None:
        """Start capturing the provider call in the current execution context."""

    def finish(self) -> ProviderReceipt | None:
        """Finish capture and return the provider receipt when one exists."""


__all__ = ["ProviderReceipt", "ProviderReceiptSource"]
