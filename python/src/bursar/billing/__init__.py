from typing import TYPE_CHECKING

from bursar.billing.manager import BillingManager
from bursar.billing.models import (
    AllowanceGrant,
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
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
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
    "AllowanceGrant",
    "BillingConfig",
    "BillingCreditTopup",
    "BillingCustomerInfo",
    "BillingCustomerRecord",
    "BillingDisputeInfo",
    "BillingEvent",
    "BillingEventClaim",
    "BillingEventResult",
    "BillingEventType",
    "BillingInvoiceInfo",
    "BillingManager",
    "BillingOffer",
    "BillingOfferInterval",
    "BillingPaymentInfo",
    "BillingPreferences",
    "BillingProvider",
    "ProviderRef",
    "BillingRefundInfo",
    "BillingStore",
    "BillingSubscriptionInfo",
    "BillingSubscriptionState",
    "BillingSubscriptionStatus",
    "CycleGrant",
    "PostgresBillingStore",
]
