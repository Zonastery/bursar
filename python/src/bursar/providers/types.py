from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol


class ProviderLogger(Protocol):
    def debug(self, msg: str, ctx: dict | None = None) -> None: ...
    def warning(self, msg: str, ctx: dict | None = None) -> None: ...
    def error(self, msg: str, ctx: dict | None = None) -> None: ...


class StdlibProviderLogger:
    """Concrete ProviderLogger wrapper around a standard library logger."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, msg: str, ctx: dict | None = None) -> None:
        self._logger.debug(msg, extra={"ctx": ctx} if ctx else None)

    def warning(self, msg: str, ctx: dict | None = None) -> None:
        self._logger.warning(msg, extra={"ctx": ctx} if ctx else None)

    def error(self, msg: str, ctx: dict | None = None) -> None:
        self._logger.error(msg, extra={"ctx": ctx} if ctx else None)


ProviderResolveUserFn = Callable[[dict[str, Any], dict[str, str]], Awaitable[str | None]]


@dataclass
class WebhookRequest:
    raw_body: str
    headers: dict[str, str]


@dataclass
class CheckoutParams:
    user_id: str | None = None
    customer_id: str | None = None
    email: str | None = None
    product_id: str = ""
    type: Literal["subscription", "credit_pack"] = "subscription"
    quantity: int | None = None
    return_url: str = ""
    cancel_url: str = ""
    metadata: dict[str, str] | None = None


@dataclass
class PortalParams:
    customer_id: str = ""
    return_url: str = ""


@dataclass
class UpdatePaymentMethodParams:
    customer_id: str = ""
    subscription_id: str = ""
    return_url: str = ""
    product_id: str | None = None


@dataclass
class PaymentMethodSetupParams:
    customer_id: str = ""
    return_url: str = ""
    cancel_url: str | None = None
    product_id: str | None = None


@dataclass
class CreateCustomerParams:
    email: str = ""
    name: str = ""
    metadata: dict[str, str] | None = None


@dataclass
class PaymentMethodInfo:
    id: str = ""
    last4: str = ""
    brand: str = ""
    expiry_month: int = 0
    expiry_year: int = 0


class PaymentProvider(ABC):
    provider: str

    @abstractmethod
    async def create_checkout_session(self, params: CheckoutParams) -> dict: ...

    @abstractmethod
    async def create_customer_portal_session(self, params: PortalParams) -> dict: ...

    @abstractmethod
    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> dict: ...

    @abstractmethod
    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> dict: ...

    @abstractmethod
    async def create_customer(self, params: CreateCustomerParams) -> dict: ...

    @abstractmethod
    async def handle_webhook(self, req: WebhookRequest) -> dict: ...

    @abstractmethod
    async def cancel_subscription(self, subscription_id: str) -> None: ...

    @abstractmethod
    async def reactivate_subscription(self, subscription_id: str) -> None: ...

    @abstractmethod
    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]: ...

    @abstractmethod
    async def get_invoice_url(self, provider_payment_id: str) -> dict | None: ...
