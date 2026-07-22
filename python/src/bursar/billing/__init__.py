from typing import TYPE_CHECKING

from bursar.billing.auto_recharge import AutoRechargeService
from bursar.billing.billing_service import BillingProvisioningPort
from bursar.billing.models import (
    AllowanceGrant,
    AutoRechargeLimit,
    AutoRechargeTopup,
    AutoRechargeTrigger,
    BillingAutoRechargeAttempt,
    BillingAutoRechargeConfig,
    BillingAutoRechargePolicy,
    BillingAutoRechargeProfile,
    BillingConfig,
    BillingCreditTopup,
    BillingCustomerInfo,
    BillingCustomerRecord,
    BillingDisputeInfo,
    BillingEvent,
    BillingEventClaim,
    BillingEventResult,
    BillingEventType,
    BillingInvoiceInfo,
    BillingOffer,
    BillingOfferInterval,
    BillingPaymentInfo,
    BillingPreferences,
    BillingProvider,
    BillingRefundInfo,
    BillingSubscriptionChange,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    CheckoutIntent,
    CheckoutIntentStatus,
    CycleGrant,
    ProviderRef,
)
from bursar.billing.store import BillingStore

if TYPE_CHECKING:
    from bursar.billing.postgres import PostgresBillingStore


def __getattr__(name: str):
    """Lazy-import PostgresBillingStore — psycopg2 optional unless used."""
    if name == "PostgresBillingStore":
        from bursar.billing.postgres import PostgresBillingStore

        return PostgresBillingStore
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "AutoRechargeService",
    "AllowanceGrant",
    "BillingConfig",
    "BillingAutoRechargeAttempt",
    "BillingAutoRechargeProfile",
    "BillingAutoRechargeConfig",
    "BillingAutoRechargePolicy",
    "AutoRechargeTrigger",
    "AutoRechargeTopup",
    "AutoRechargeLimit",
    "BillingCreditTopup",
    "BillingCustomerInfo",
    "BillingCustomerRecord",
    "BillingDisputeInfo",
    "BillingEvent",
    "BillingEventClaim",
    "BillingEventResult",
    "BillingEventType",
    "BillingInvoiceInfo",
    "BillingOffer",
    "BillingOfferInterval",
    "BillingPaymentInfo",
    "BillingPreferences",
    "BillingProvisioningPort",
    "BillingProvider",
    "CheckoutIntent",
    "CheckoutIntentStatus",
    "ProviderRef",
    "BillingRefundInfo",
    "BillingStore",
    "BillingSubscriptionInfo",
    "BillingSubscriptionChange",
    "BillingSubscriptionState",
    "BillingSubscriptionStatus",
    "CycleGrant",
    "PostgresBillingStore",
]
