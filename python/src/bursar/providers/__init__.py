from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from bursar.providers.types import (
    ChangePlanLineItem,
    ChangePlanParams,
    ChangePlanPreview,
    ChangePlanResult,
    CheckoutParams,
    CheckoutPaymentStatus,
    CheckoutSessionResult,
    CheckoutSessionStatus,
    CheckoutStatusProvider,
    CreateCustomerParams,
    CreateCustomerResult,
    CustomerCreationProvider,
    CustomerPortalProvider,
    InvoiceUrlProvider,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentMethodSetupProvider,
    PaymentMethodsProvider,
    PaymentProvider,
    PlanChangePreviewProvider,
    PlanChangeProvider,
    PlanSelection,
    PortalParams,
    PreviewChangePlanParams,
    ProviderEnvironment,
    ProviderLogger,
    ProviderUrlResult,
    SavedPaymentChargeParams,
    SavedPaymentChargeProvider,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    SavedPaymentChargeStatus,
    SavedPaymentPreviewProvider,
    ScheduledPlanChangeCancellationProvider,
    StdlibProviderLogger,
    SubscriptionCancellationProvider,
    SubscriptionReactivationProvider,
    UpdatePaymentMethodParams,
    UpdatePaymentMethodProvider,
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
    "ChangePlanLineItem",
    "ChangePlanParams",
    "ChangePlanPreview",
    "ChangePlanResult",
    "CheckoutPaymentStatus",
    "CheckoutParams",
    "CheckoutSessionResult",
    "CheckoutSessionStatus",
    "CheckoutStatusProvider",
    "CustomerCreationProvider",
    "CustomerPortalProvider",
    "CreateCustomerResult",
    "CreateCustomerParams",
    "deduplicate_payment_methods",
    "DodoProvider",
    "MockPaymentProvider",
    "InvoiceUrlProvider",
    "PaymentMethodInfo",
    "PaymentMethodsProvider",
    "PlanSelection",
    "PlanChangePreviewProvider",
    "PlanChangeProvider",
    "PreviewChangePlanParams",
    "ProviderUrlResult",
    "SavedPaymentChargeParams",
    "SavedPaymentChargeProvider",
    "SavedPaymentChargeResult",
    "SavedPaymentChargeQuote",
    "SavedPaymentChargeStatus",
    "SavedPaymentPreviewProvider",
    "PaymentMethodSetupParams",
    "PaymentMethodSetupProvider",
    "PaymentProvider",
    "PortalParams",
    "ProviderLogger",
    "ProviderEnvironment",
    "normalize_provider_logger",
    "StdlibProviderLogger",
    "ScheduledPlanChangeCancellationProvider",
    "StripeProvider",
    "SubscriptionCancellationProvider",
    "SubscriptionReactivationProvider",
    "UpdatePaymentMethodProvider",
    "UpdatePaymentMethodParams",
    "WebhookRequest",
    "WebhookResult",
]
