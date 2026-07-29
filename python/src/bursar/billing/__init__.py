from typing import TYPE_CHECKING

from bursar.billing.auto_recharge_service import (
    AutoRechargeProcessResult,
    AutoRechargeService,
)
from bursar.billing.billing_service import BillingProvisioningPort
from bursar.billing.billing_store import BillingStore
from bursar.billing.types import (
    AllowanceGrant,
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingAutoRechargeStatus,
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
    BillingSubscriptionChangeInput,
    BillingSubscriptionChangeState,
    BillingSubscriptionInfo,
    BillingSubscriptionOfferContext,
    BillingSubscriptionProrationBehavior,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    CheckoutIntent,
    CheckoutIntentStatus,
    CycleGrant,
    ProviderRef,
)

if TYPE_CHECKING:
    from bursar.billing.postgres.store import PostgresBillingStore


def __getattr__(name: str):
    """Lazy-import PostgresBillingStore — psycopg2 optional unless used."""
    if name == "PostgresBillingStore":
        from bursar.billing.postgres.store import PostgresBillingStore

        return PostgresBillingStore
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "AutoRechargeService",
    "AutoRechargeProcessResult",
    "AllowanceGrant",
    "BillingAutoRechargeAttempt",
    "BillingAutoRechargeProfile",
    "BillingAutoRechargeStatus",
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
    "BillingSubscriptionChangeInput",
    "BillingSubscriptionChangeState",
    "BillingSubscriptionOfferContext",
    "BillingSubscriptionProrationBehavior",
    "BillingSubscriptionState",
    "BillingSubscriptionStatus",
    "CycleGrant",
    "PostgresBillingStore",
]
