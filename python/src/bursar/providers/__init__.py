from __future__ import annotations

from importlib import import_module
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

_LAZY_EXPORTS = {
    "DodoProvider": ("bursar.providers.dodo.provider", "DodoProvider"),
    "MockPaymentProvider": ("bursar.providers.mock.provider", "MockPaymentProvider"),
    "StripeProvider": ("bursar.providers.stripe.provider", "StripeProvider"),
}


def __getattr__(name: str) -> object:
    """Lazy-import providers — each requires its own optional dependency (stripe, dodopayments)."""
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module_name, attribute_name = target
        value = getattr(import_module(module_name), attribute_name)
        globals()[name] = value
        return value
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
