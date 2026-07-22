from __future__ import annotations

from bursar.providers._shared import call_billing_event_sink, parse_status
from bursar.providers.types import (
    CheckoutParams,
    CreateCustomerParams,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    ProviderLogger,
    ProviderResolveUserFn,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    StdlibProviderLogger,
    UpdatePaymentMethodParams,
    WebhookRequest,
)


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
    "CheckoutParams",
    "CreateCustomerParams",
    "DodoProvider",
    "MockPaymentProvider",
    "parse_status",
    "PaymentMethodInfo",
    "SavedPaymentChargeParams",
    "SavedPaymentChargeResult",
    "SavedPaymentChargeQuote",
    "PaymentMethodSetupParams",
    "PaymentProvider",
    "PortalParams",
    "ProviderLogger",
    "ProviderResolveUserFn",
    "StdlibProviderLogger",
    "StripeProvider",
    "UpdatePaymentMethodParams",
    "WebhookRequest",
]
