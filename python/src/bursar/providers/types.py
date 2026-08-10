from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from bursar.shared.idempotency import StableKey
from bursar.shared.numbers import NonNegativeSafeInteger, PositiveSafeInteger

ProviderEnvironment = Literal["live", "test", "sandbox"]


@runtime_checkable
class ProviderLogger(Protocol):
    def debug(self, msg: str, ctx: dict | None = None) -> None: ...
    def info(self, msg: str, ctx: dict | None = None) -> None: ...
    def warning(self, msg: str, ctx: dict | None = None) -> None: ...
    def error(self, msg: str, ctx: dict | None = None) -> None: ...


class StdlibProviderLogger:
    """Concrete ProviderLogger wrapper around a standard library logger."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, msg: str, ctx: dict | None = None) -> None:
        self._logger.debug(msg, extra={"ctx": ctx} if ctx else None)

    def info(self, msg: str, ctx: dict | None = None) -> None:
        self._logger.info(msg, extra={"ctx": ctx} if ctx else None)

    def warning(self, msg: str, ctx: dict | None = None) -> None:
        self._logger.warning(msg, extra={"ctx": ctx} if ctx else None)

    def error(self, msg: str, ctx: dict | None = None) -> None:
        self._logger.error(msg, extra={"ctx": ctx} if ctx else None)


def _noop(*args: Any, **kwargs: Any) -> None:
    pass


class _NormalizedProviderLogger:
    """Wraps a logger, filling missing methods with no-ops.

    Mirrors JS ``normalizeLogger`` in ``shared/logger.ts``.
    """

    def __init__(self, logger: Any = None) -> None:
        def method(name: str):
            candidate = getattr(logger, name, None)
            return candidate if callable(candidate) else _noop

        self._debug = method("debug")
        self._info = method("info")
        self._warning = method("warning")
        self._error = method("error")

    def debug(self, msg: str, ctx: dict | None = None) -> None:
        self._debug(msg, ctx)

    def info(self, msg: str, ctx: dict | None = None) -> None:
        self._info(msg, ctx)

    def warning(self, msg: str, ctx: dict | None = None) -> None:
        self._warning(msg, ctx)

    def error(self, msg: str, ctx: dict | None = None) -> None:
        self._error(msg, ctx)


def normalize_provider_logger(logger: Any = None) -> ProviderLogger:
    if isinstance(logger, logging.Logger):
        return StdlibProviderLogger(logger)
    return _NormalizedProviderLogger(logger)


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_trimmed_non_empty_string(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("value must be a trimmed non-empty string")
    return value


NonEmptyString = Annotated[
    str,
    Field(strict=True),
    AfterValidator(_require_trimmed_non_empty_string),
]
PositiveInt = PositiveSafeInteger
NonNegativeInt = NonNegativeSafeInteger


def _require_finite_number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise ValueError("value must be a finite number")
    return value


def _normalize_currency(value: str) -> str:
    if len(value) != 3 or not value.isascii() or not value.isalpha():
        raise ValueError("currency must be a three-letter ISO code")
    return value.upper()


def _normalize_instant(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("instant must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("instant must include a UTC offset")
    return parsed.astimezone(UTC).isoformat()


FiniteNumber = Annotated[int | float, BeforeValidator(_require_finite_number)]
CurrencyCode = Annotated[str, Field(strict=True), AfterValidator(_normalize_currency)]
ProviderInstant = Annotated[str, Field(strict=True), AfterValidator(_normalize_instant)]


class WebhookRequest(_ProviderModel):
    raw_body: str
    headers: dict[str, str]


class WebhookResult(_ProviderModel):
    received: bool = Field(strict=True)
    retryable: bool = Field(strict=True)
    provider: NonEmptyString
    event_id: NonEmptyString | None
    event_type: NonEmptyString | None


class CheckoutParams(_ProviderModel):
    account_id: NonEmptyString
    customer_id: NonEmptyString | None = None
    email: NonEmptyString | None = None
    product_id: NonEmptyString
    type: Literal["subscription", "credit_pack"]
    quantity: PositiveInt | None = None
    return_url: NonEmptyString
    cancel_url: str
    metadata: dict[str, str]
    idempotency_key: StableKey


CheckoutPaymentStatus = Literal[
    "succeeded",
    "failed",
    "cancelled",
    "processing",
    "requires_customer_action",
    "requires_merchant_action",
    "requires_payment_method",
    "requires_confirmation",
    "requires_capture",
    "partially_captured",
    "partially_captured_and_capturable",
]


class CheckoutSessionStatus(_ProviderModel):
    payment_status: CheckoutPaymentStatus | None


class CheckoutSessionResult(_ProviderModel):
    url: NonEmptyString
    customer_id: NonEmptyString | None = None
    provider_session_id: NonEmptyString | None = None


class ProviderUrlResult(_ProviderModel):
    url: NonEmptyString


class CreateCustomerResult(_ProviderModel):
    customer_id: NonEmptyString


class ChangePlanResult(_ProviderModel):
    provider_operation_id: NonEmptyString | None = None


class PortalParams(_ProviderModel):
    customer_id: NonEmptyString
    return_url: NonEmptyString


class UpdatePaymentMethodParams(_ProviderModel):
    customer_id: NonEmptyString
    subscription_id: NonEmptyString
    return_url: NonEmptyString
    product_id: NonEmptyString | None = None


class PaymentMethodSetupParams(_ProviderModel):
    customer_id: NonEmptyString
    return_url: NonEmptyString
    cancel_url: str | None = None
    product_id: NonEmptyString | None = None


class CreateCustomerParams(_ProviderModel):
    email: NonEmptyString
    name: NonEmptyString
    metadata: dict[str, str]
    idempotency_key: StableKey


class PaymentMethodInfo(_ProviderModel):
    id: NonEmptyString
    last4: str = Field(pattern=r"^[0-9]{4}$")
    brand: NonEmptyString
    expiry_month: int = Field(strict=True, ge=1, le=12)
    expiry_year: PositiveInt
    is_default: bool = Field(default=False, strict=True)


def deduplicate_payment_methods(methods: list[PaymentMethodInfo]) -> list[PaymentMethodInfo]:
    seen: set[str] = set()
    result: list[PaymentMethodInfo] = []
    for method in methods:
        key = f"{method.brand.strip().lower()}:{method.last4}:{method.expiry_month}:{method.expiry_year}"
        if key in seen:
            continue
        seen.add(key)
        result.append(method)
    return result


class SavedPaymentChargeParams(_ProviderModel):
    customer_id: NonEmptyString
    payment_method_id: NonEmptyString
    product_id: NonEmptyString
    quantity: PositiveInt
    metadata: dict[str, str]
    idempotency_key: NonEmptyString
    return_url: NonEmptyString | None = None


SavedPaymentChargeStatus = Literal[
    "succeeded",
    "processing",
    "failed",
    "cancelled",
    "requires_customer_action",
    "requires_merchant_action",
    "requires_payment_method",
    "requires_confirmation",
    "requires_capture",
    "partially_captured",
    "partially_captured_and_capturable",
]


class SavedPaymentChargeResult(_ProviderModel):
    """Validated provider charge result mirroring the JavaScript contract."""

    provider_payment_id: NonEmptyString | None = None
    status: SavedPaymentChargeStatus
    action_url: NonEmptyString | None = None
    amount_minor: NonNegativeInt | None = None
    currency: CurrencyCode | None = None


class SavedPaymentChargeQuote(_ProviderModel):
    amount_minor: NonNegativeInt
    currency: CurrencyCode
    tax_minor: NonNegativeInt | None = None
    expires_at: NonEmptyString | None = None


class ChangePlanParams(_ProviderModel):
    provider_subscription_id: NonEmptyString
    product_id: NonEmptyString
    proration_billing_mode: Literal[
        "prorated_immediately",
        "full_immediately",
        "difference_immediately",
        "do_not_bill",
    ]
    effective_at: Literal["immediately", "next_billing_date"] | None = None
    on_payment_failure: Literal["prevent_change", "apply_change"] | None = None
    quantity: PositiveInt = 1
    metadata: dict[str, str] | None = None
    idempotency_key: StableKey


class PlanSelection(_ProviderModel):
    plan_id: str
    interval: Literal["month", "year"]


class PreviewChangePlanParams(_ProviderModel):
    provider_subscription_id: NonEmptyString
    product_id: NonEmptyString
    proration_billing_mode: Literal[
        "prorated_immediately",
        "full_immediately",
        "difference_immediately",
        "do_not_bill",
    ]
    effective_at: Literal["immediately", "next_billing_date"] | None = None
    quantity: PositiveInt = 1


class ChangePlanLineItem(_ProviderModel):
    product_id: NonEmptyString
    name: NonEmptyString
    unit_price: FiniteNumber
    quantity: PositiveInt
    proration_factor: FiniteNumber
    currency: CurrencyCode
    tax: FiniteNumber
    subtotal: FiniteNumber


class ChangePlanPreview(_ProviderModel):
    total_amount: FiniteNumber
    settlement_amount: FiniteNumber
    currency: CurrencyCode
    line_items: list[ChangePlanLineItem]
    effective_at: ProviderInstant
    recurring_amount: FiniteNumber | None = None
    recurring_currency: CurrencyCode | None = None
    next_billing_date: ProviderInstant | None = None
    tax_amount: FiniteNumber | None = None
    customer_credits: FiniteNumber | None = None


@runtime_checkable
class PaymentProvider(Protocol):
    """Required structural contract for a payment provider."""

    provider: str

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult: ...

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult: ...


@runtime_checkable
class CheckoutStatusProvider(Protocol):
    async def get_checkout_session_status(self, provider_session_id: str) -> CheckoutSessionStatus | None: ...


@runtime_checkable
class CustomerPortalProvider(Protocol):
    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult: ...


@runtime_checkable
class UpdatePaymentMethodProvider(Protocol):
    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult: ...


@runtime_checkable
class PaymentMethodSetupProvider(Protocol):
    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult: ...


@runtime_checkable
class CustomerCreationProvider(Protocol):
    async def create_customer(self, params: CreateCustomerParams) -> CreateCustomerResult: ...


@runtime_checkable
class SubscriptionCancellationProvider(Protocol):
    async def cancel_subscription(self, subscription_id: str, idempotency_key: str) -> None: ...


@runtime_checkable
class SubscriptionReactivationProvider(Protocol):
    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str) -> None: ...


@runtime_checkable
class ScheduledPlanChangeCancellationProvider(Protocol):
    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        provider_operation_id: str | None = None,
        *,
        idempotency_key: str,
    ) -> None: ...


@runtime_checkable
class PaymentMethodsProvider(Protocol):
    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]: ...


@runtime_checkable
class SavedPaymentPreviewProvider(Protocol):
    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote: ...


@runtime_checkable
class SavedPaymentChargeProvider(Protocol):
    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult: ...


@runtime_checkable
class InvoiceUrlProvider(Protocol):
    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult | None: ...


@runtime_checkable
class PlanChangeProvider(Protocol):
    async def change_plan(self, params: ChangePlanParams) -> ChangePlanResult | None: ...


@runtime_checkable
class PlanChangePreviewProvider(Protocol):
    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview: ...
