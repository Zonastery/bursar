from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


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
        self._debug = getattr(logger, "debug", _noop)
        self._info = getattr(logger, "info", _noop)
        self._warning = getattr(logger, "warning", _noop)
        self._error = getattr(logger, "error", _noop)

    def debug(self, msg: str, ctx: dict | None = None) -> None:
        self._debug(msg, ctx)

    def info(self, msg: str, ctx: dict | None = None) -> None:
        self._info(msg, ctx)

    def warning(self, msg: str, ctx: dict | None = None) -> None:
        self._warning(msg, ctx)

    def error(self, msg: str, ctx: dict | None = None) -> None:
        self._error(msg, ctx)


def normalize_provider_logger(logger: Any = None) -> ProviderLogger:
    return _NormalizedProviderLogger(logger)


@runtime_checkable
class ResolveUserCallback(Protocol):
    def __call__(
        self,
        identity: ResolveIdentityInput,
    ) -> Awaitable[str | None]: ...


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveIdentityInput(_ProviderModel):
    provider: str
    provider_event_type: str
    normalized_event_type: str | None = None
    customer_id: str | None = None
    email: str | None = None
    metadata: dict[str, str]
    successful: bool
    checkout_kind: Literal["subscription", "credit_topup"] | None = None


class _ProviderResultModel(_ProviderModel):
    """Pydantic result with transitional mapping access for SDK internals."""

    def _field_name(self, key: str) -> str:
        if key in type(self).model_fields:
            return key
        for name, field in type(self).model_fields.items():
            if field.alias == key:
                return name
            camel = name.split("_")[0] + "".join(part.title() for part in name.split("_")[1:])
            if camel == key:
                return name
        raise KeyError(key)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, self._field_name(key))

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


class WebhookRequest(_ProviderModel):
    raw_body: str
    headers: dict[str, str]


class WebhookResult(_ProviderResultModel):
    received: bool
    retryable: bool
    provider: str
    event_id: str | None
    event_type: str | None


class CheckoutParams(_ProviderModel):
    user_id: str | None = None
    customer_id: str | None = None
    email: str | None = None
    product_id: str
    type: Literal["subscription", "credit_pack"]
    quantity: int | None = None
    return_url: str
    cancel_url: str
    metadata: dict[str, str]
    idempotency_key: str | None = None


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


class CheckoutSessionStatus(_ProviderResultModel):
    payment_status: CheckoutPaymentStatus | None


class CheckoutSessionResult(_ProviderResultModel):
    url: str
    customer_id: str | None = None
    provider_session_id: str | None = None


class ProviderUrlResult(_ProviderResultModel):
    url: str


class CreateCustomerResult(_ProviderResultModel):
    customer_id: str


class ChangePlanResult(_ProviderResultModel):
    provider_operation_id: str | None = None


class PortalParams(_ProviderModel):
    customer_id: str
    return_url: str


class UpdatePaymentMethodParams(_ProviderModel):
    customer_id: str
    subscription_id: str
    return_url: str
    product_id: str | None = None


class PaymentMethodSetupParams(_ProviderModel):
    customer_id: str
    return_url: str
    cancel_url: str | None = None
    product_id: str | None = None


class CreateCustomerParams(_ProviderModel):
    email: str
    name: str
    metadata: dict[str, str]


class PaymentMethodInfo(_ProviderModel):
    id: str
    last4: str
    brand: str
    expiry_month: int
    expiry_year: int
    is_default: bool = False


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
    customer_id: str
    payment_method_id: str
    product_id: str
    quantity: int
    metadata: dict[str, str]
    idempotency_key: str
    return_url: str | None = None


SavedPaymentChargeStatus = Literal[
    "succeeded",
    "processing",
    "failed",
    "requires_customer_action",
    "requires_payment_method",
]


class SavedPaymentChargeResult(_ProviderResultModel):
    """Validated provider charge result mirroring the JavaScript contract."""

    provider_payment_id: str | None = None
    status: SavedPaymentChargeStatus
    action_url: str | None = None
    amount_minor: int | None = None
    currency: str | None = None


class SavedPaymentChargeQuote(_ProviderResultModel):
    amount_minor: int
    currency: str
    tax_minor: int | None = None
    expires_at: str | None = None


class ChangePlanParams(_ProviderModel):
    provider_subscription_id: str
    product_id: str
    proration_billing_mode: Literal[
        "prorated_immediately",
        "full_immediately",
        "difference_immediately",
        "do_not_bill",
    ]
    effective_at: Literal["immediately", "next_billing_date"] | None = None
    on_payment_failure: Literal["prevent_change", "apply_change"] | None = None
    quantity: int = 1
    metadata: dict[str, str] | None = None
    idempotency_key: str | None = None


class PlanSelection(_ProviderModel):
    plan_id: str
    interval: Literal["month", "year"]


class PreviewChangePlanParams(_ProviderModel):
    provider_subscription_id: str
    product_id: str
    proration_billing_mode: Literal[
        "prorated_immediately",
        "full_immediately",
        "difference_immediately",
        "do_not_bill",
    ]
    effective_at: Literal["immediately", "next_billing_date"] | None = None
    quantity: int = 1


class ChangePlanLineItem(_ProviderResultModel):
    product_id: str
    name: str
    unit_price: int
    quantity: int
    proration_factor: float
    currency: str
    tax: int
    subtotal: int


class ChangePlanPreview(_ProviderResultModel):
    total_amount: int
    settlement_amount: int
    currency: str
    line_items: list[ChangePlanLineItem]
    effective_at: str
    recurring_amount: int | None = None
    recurring_currency: str | None = None
    next_billing_date: str | None = None
    tax_amount: int | None = None
    customer_credits: int | None = None


class PaymentProvider(ABC):
    provider: str

    @abstractmethod
    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult: ...

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        raise NotImplementedError("provider does not support create_customer_portal_session")

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        raise NotImplementedError("provider does not support create_update_payment_method_session")

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
        raise NotImplementedError("provider does not support create_payment_method_setup_session")

    async def create_customer(self, params: CreateCustomerParams) -> CreateCustomerResult:
        raise NotImplementedError("provider does not support create_customer")

    @abstractmethod
    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult: ...

    async def get_checkout_session_status(self, provider_session_id: str) -> CheckoutSessionStatus | None:
        return None

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        raise NotImplementedError("provider does not support cancel_subscription")

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        raise NotImplementedError("provider does not support reactivate_subscription")

    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        provider_operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Remove a pending plan switch while retaining the subscription."""
        raise NotImplementedError

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        raise NotImplementedError("provider does not support list_payment_methods")

    async def get_default_payment_method(self, customer_id: str) -> PaymentMethodInfo | None:
        methods = await self.list_payment_methods(customer_id)
        return methods[0] if len(methods) == 1 else None

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        raise NotImplementedError("provider does not support saved-payment previews")

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        raise NotImplementedError("provider does not support charge_saved_payment_method")

    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult | None:
        raise NotImplementedError("provider does not support get_invoice_url")

    async def change_plan(self, params: ChangePlanParams) -> ChangePlanResult | None:
        raise NotImplementedError("provider does not support change_plan")

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        raise NotImplementedError("provider does not support preview_change_plan")
