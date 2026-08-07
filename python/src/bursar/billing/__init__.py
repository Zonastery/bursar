from typing import TYPE_CHECKING

from bursar.billing.auto_recharge_service import (
    AutoRechargeProcessResult,
    AutoRechargeService,
)
from bursar.billing.billing_service import BillingService
from bursar.billing.billing_store import BillingStore
from bursar.billing.contracts import (
    AutoRechargeAttemptClaim,
    AutoRechargeAttemptUpdate,
    AutoRechargeProviderPaymentUpdate,
    BillingCreditGrantCreate,
    BillingDisputeUpsert,
    BillingEventSink,
    BillingInvoiceUpsert,
    BillingPaymentUpsert,
    BillingRefundUpsert,
    BillingSubscriptionChangeUpdate,
    BillingSubscriptionConflictCreate,
    CheckoutIntentCreate,
    CheckoutIntentUpdate,
)
from bursar.billing.service_types import (
    BillingProvisioningPort,
    BillingServiceOptions,
)
from bursar.billing.types import (
    AUTO_RECHARGE_STATES,
    AllowanceGrant,
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingAutoRechargeState,
    BillingAutoRechargeStatus,
    BillingCreditPostingResult,
    BillingCustomerInfo,
    BillingCustomerRecord,
    BillingDisputeInfo,
    BillingEvent,
    BillingEventClaim,
    BillingEventHandler,
    BillingEventResult,
    BillingEventType,
    BillingInvoiceInfo,
    BillingInvoiceRecord,
    BillingOfferInterval,
    BillingPaymentInfo,
    BillingPaymentRecord,
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
    EntitlementMode,
    ProviderRef,
    SubscriptionGrant,
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
    "AutoRechargeAttemptClaim",
    "AutoRechargeAttemptUpdate",
    "AutoRechargeProviderPaymentUpdate",
    "BillingCreditGrantCreate",
    "BillingDisputeUpsert",
    "BillingEventSink",
    "BillingInvoiceUpsert",
    "BillingPaymentUpsert",
    "BillingRefundUpsert",
    "BillingSubscriptionChangeUpdate",
    "BillingSubscriptionConflictCreate",
    "CheckoutIntentCreate",
    "CheckoutIntentUpdate",
    "AutoRechargeService",
    "AutoRechargeProcessResult",
    "AUTO_RECHARGE_STATES",
    "AllowanceGrant",
    "BillingAutoRechargeAttempt",
    "BillingAutoRechargeProfile",
    "BillingAutoRechargeStatus",
    "BillingAutoRechargeState",
    "BillingCreditPostingResult",
    "BillingCustomerInfo",
    "BillingCustomerRecord",
    "BillingDisputeInfo",
    "BillingEvent",
    "BillingEventClaim",
    "BillingEventHandler",
    "BillingEventResult",
    "BillingEventType",
    "BillingInvoiceInfo",
    "BillingInvoiceRecord",
    "BillingOfferInterval",
    "BillingPaymentInfo",
    "BillingPaymentRecord",
    "BillingPreferences",
    "BillingProvisioningPort",
    "BillingServiceOptions",
    "BillingProvider",
    "BillingService",
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
    "EntitlementMode",
    "PostgresBillingStore",
    "SubscriptionGrant",
]
