"""Pydantic schemas for credit store operations.

All store methods accept and return typed Pydantic models rather than
raw dicts — validation at the boundary, clarity in the call sites.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Metadata ──────────────────────────────────────────────────────────


class CreditMetadata(BaseModel, extra="allow"):
    """Flexible metadata attached to credit transactions.

    Known fields are typed; arbitrary extras pass through to JSONB.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    reference_type: str | None = None
    reference_id: str | None = None
    idempotency_key: str | None = None
    fixed_job: str | None = None


# ── Pricing configuration ─────────────────────────────────────────────


class PricingConfigData(BaseModel):
    """Schema for a versioned pricing configuration.

    Mirrors the YAML config structure used by ``PricingEngine``.
    """

    version: int
    models: dict[str, str]
    tools: dict[str, str] | None = None
    search: dict[str, str] | None = None
    cache: dict[str, str] | None = None
    fixed: dict[str, int] | None = None
    min_balance: int | None = None


# ── Runtime results ───────────────────────────────────────────────────


class BalanceResult(BaseModel):
    """Current credit balance for a user."""

    user_id: str
    balance: int = 0
    lifetime_purchased: int = 0


class AddCreditsResult(BaseModel):
    """Result of adding credits to a user's account."""

    transaction_id: str
    user_id: str
    amount: int
    new_balance: int
    lifetime_purchased: int = 0


class ReserveResult(BaseModel):
    """Result of reserving credits for an operation."""

    reservation_id: str
    user_id: str
    amount: int
    balance: int = 0
    reserved_total: int = 0
    error: str | None = None


class DeductionResult(BaseModel):
    """Result of deducting credits after an operation completes.

    ``amount`` is negative for deductions, positive for refunds.
    """

    transaction_id: str
    user_id: str
    amount: int
    balance_after: int
    idempotent: bool = False
    error: str | None = None


class PricingConfigResult(BaseModel):
    """Versioned pricing configuration fetched from the store."""

    id: str
    config: PricingConfigData
    version: int = 1


class SetupResult(BaseModel):
    """Report of what the setup step created or updated."""

    tables_created: list[str] = Field(default_factory=list)
    rpcs_created: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0
