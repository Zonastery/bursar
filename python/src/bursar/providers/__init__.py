from __future__ import annotations

from typing import TYPE_CHECKING

from bursar.providers._shared import call_billing_event_sink, parse_status
from bursar.providers.types import (
    ChangePlanLineItem,
    ChangePlanParams,
    ChangePlanPreview,
    ChangePlanResult,
    CheckoutParams,
    CheckoutPaymentStatus,
    CheckoutSessionResult,
    CheckoutSessionStatus,
    CreateCustomerParams,
    CreateCustomerResult,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PlanSelection,
    PortalParams,
    PreviewChangePlanParams,
    ProviderLogger,
    ProviderUrlResult,
    ResolveIdentityInput,
    ResolveUserCallback,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    SavedPaymentChargeStatus,
    StdlibProviderLogger,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
    deduplicate_payment_methods,
    normalize_provider_logger,
)

if TYPE_CHECKING:
    from bursar.providers.dodo.provider import DodoProvider
    from bursar.providers.mock.provider import MockPaymentProvider
    from bursar.providers.stripe.provider import StripeProvider


def __getattr__(name: str):
    """Lazy-import providers — each requires its own optional dependency (stripe, dodopayments)."""
    if name == "DodoProvider":
        from bursar.providers.dodo.provider import DodoProvider  # pyright: ignore[reportUnsupportedDunderAll]

        return DodoProvider
    if name == "MockPaymentProvider":
        from bursar.providers.mock.provider import MockPaymentProvider  # pyright: ignore[reportUnsupportedDunderAll]

        return MockPaymentProvider
    if name == "StripeProvider":
        from bursar.providers.stripe.provider import StripeProvider  # pyright: ignore[reportUnsupportedDunderAll]

        return StripeProvider
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "call_billing_event_sink",
    "ChangePlanLineItem",
    "ChangePlanParams",
    "ChangePlanPreview",
    "ChangePlanResult",
    "CheckoutPaymentStatus",
    "CheckoutParams",
    "CheckoutSessionResult",
    "CheckoutSessionStatus",
    "CreateCustomerResult",
    "CreateCustomerParams",
    "deduplicate_payment_methods",
    "DodoProvider",
    "MockPaymentProvider",
    "parse_status",
    "PaymentMethodInfo",
    "PlanSelection",
    "PreviewChangePlanParams",
    "ProviderUrlResult",
    "SavedPaymentChargeParams",
    "SavedPaymentChargeResult",
    "SavedPaymentChargeQuote",
    "SavedPaymentChargeStatus",
    "PaymentMethodSetupParams",
    "PaymentProvider",
    "PortalParams",
    "ProviderLogger",
    "ResolveUserCallback",
    "ResolveIdentityInput",
    "normalize_provider_logger",
    "StdlibProviderLogger",
    "StripeProvider",
    "UpdatePaymentMethodParams",
    "WebhookRequest",
    "WebhookResult",
]
