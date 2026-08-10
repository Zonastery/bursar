"""Application-facing Bursar SDK surface.

Detailed credit, billing, commerce, provider, and configuration contracts live
in their focused subpackages. The package root intentionally exposes only the
facade and the types most applications need to compose and operate it.
"""

from importlib import import_module
from importlib.metadata import version
from typing import TYPE_CHECKING

from bursar.billing import (
    AUTO_RECHARGE_STATES,
    BillingEvent,
    BillingEventResult,
    BillingEventType,
    BillingStore,
)
from bursar.billing.service_types import BillingServiceOptions
from bursar.bursar import (
    AccountCreatedResult,
    AccountService,
    BillingCapability,
    BillingEventSink,
    Bursar,
    CatalogService,
    CreditsCapability,
)
from bursar.catalog import PublicCatalog, project_public_catalog
from bursar.commerce import (
    ActiveSubscriptionError,
    AutoRechargeInput,
    CancelAllSubscriptionsResult,
    CancelSubscriptionResult,
    CheckoutCompletedError,
    CheckoutConflictError,
    CheckoutStatusResult,
    CommerceError,
    CommerceNotConfiguredError,
    CommerceOptions,
    CommerceProviderFactory,
    CommerceProviderFactoryContext,
    CommerceResourceNotFoundError,
    CommerceRuntimeOptions,
    CommerceService,
    CommerceWebhookInput,
    CommerceWebhookResult,
    ConfirmPlanChangeInput,
    ConfirmPlanChangeResult,
    CoreBillingDataUnavailableError,
    CreateCheckoutInput,
    CreateCheckoutResult,
    GetInvoiceLinkInput,
    InvalidOfferQuantityError,
    MissingPlanChangePolicyError,
    PaymentMethodRequiredError,
    PlanChangePreviewResult,
    PortalSessionInput,
    PreviewPlanChangeInput,
    ProviderCapabilityNotSupportedError,
    ProviderSelectionError,
    QuoteChangedError,
    UnknownOfferError,
)
from bursar.config import (
    BursarConfig,
    BursarConfigData,
    CatalogRollout,
    ConfigError,
    ParsedBursarConfig,
    canonical_bursar_config_dict,
    canonical_catalog_rollout_dict,
    load_catalog_rollout,
    load_config_from_dict,
    validate_catalog_rollout,
)
from bursar.credits import (
    BeginBilledOperationOptions,
    CanAffordOptions,
    CreditEvent,
    CreditEventEmitter,
    CreditsServiceOptions,
    CreditStore,
    ExactAmount,
    GrantSubscriptionCycleOptions,
    LowBalanceConfig,
    MetricsOrAmount,
    ReserveOptions,
    RunBilledAsyncOptions,
    RunBilledOptions,
    SettleOptions,
)
from bursar.credits.service import (
    CatalogNotLoadedError,
    ConcurrencyLimitError,
    CreditError,
    FeatureNotEntitledError,
    InsufficientCreditsError,
    LeaseExpiredError,
    LeaseNotFoundError,
    OperationNotAllowedError,
    QuotaExceededError,
)
from bursar.credits.store import (
    CapabilityNotSupportedError,
    CapReachedError,
    RefundError,
    StoreError,
)
from bursar.credits.types import CreditMetadata, RefundResult
from bursar.engine import PricingEngine
from bursar.errors import (
    AutoRechargeDisabledError,
    AutoRechargeNotConfiguredError,
    BillingError,
    BursarError,
    BursarErrorCategory,
    BursarImportError,
    CapabilityNotConfiguredError,
    ProviderResponseError,
    StoreClosedError,
    StoreTimeoutError,
    StoreUnavailableError,
    bursar_error_http_status,
    bursar_error_public_message,
    is_bursar_error,
    is_retryable_bursar_error,
)
from bursar.expr import ExpressionError
from bursar.load_config_file import load_config_file
from bursar.metrics import UsageMetrics
from bursar.providers.types import ProviderEnvironment
from bursar.retry import (
    BursarRetryOptions,
    retry_bursar_operation,
    retry_bursar_operation_async,
)

__version__ = version("bursar")

if TYPE_CHECKING:
    from bursar.billing import PostgresBillingStore
    from bursar.credits.postgres.store import PostgresStore
    from bursar.shared.postgres_client import PostgresConnectionOptions

_LAZY_EXPORTS = {
    "PostgresBillingStore": ("bursar.billing", "PostgresBillingStore"),
    "PostgresStore": ("bursar.credits.postgres.store", "PostgresStore"),
    "PostgresConnectionOptions": ("bursar.shared.postgres_client", "PostgresConnectionOptions"),
}


def __getattr__(name: str) -> object:
    """Lazy-import Postgres stores, which require the optional Postgres extra."""
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module_name, attribute_name = target
        value = getattr(import_module(module_name), attribute_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AUTO_RECHARGE_STATES",
    "AccountCreatedResult",
    "AccountService",
    "ActiveSubscriptionError",
    "AutoRechargeDisabledError",
    "AutoRechargeInput",
    "AutoRechargeNotConfiguredError",
    "BeginBilledOperationOptions",
    "BillingCapability",
    "BillingError",
    "BillingEvent",
    "BillingEventResult",
    "BillingEventSink",
    "BillingEventType",
    "BillingServiceOptions",
    "BillingStore",
    "Bursar",
    "BursarConfig",
    "BursarConfigData",
    "BursarError",
    "BursarErrorCategory",
    "BursarImportError",
    "BursarRetryOptions",
    "CanAffordOptions",
    "CancelAllSubscriptionsResult",
    "CancelSubscriptionResult",
    "CapabilityNotConfiguredError",
    "CapabilityNotSupportedError",
    "CapReachedError",
    "CatalogNotLoadedError",
    "CatalogRollout",
    "CatalogService",
    "CheckoutCompletedError",
    "CheckoutConflictError",
    "CheckoutStatusResult",
    "CommerceError",
    "CommerceNotConfiguredError",
    "CommerceOptions",
    "CommerceProviderFactory",
    "CommerceProviderFactoryContext",
    "CommerceResourceNotFoundError",
    "CommerceRuntimeOptions",
    "CommerceService",
    "CommerceWebhookInput",
    "CommerceWebhookResult",
    "ConcurrencyLimitError",
    "ConfigError",
    "ConfirmPlanChangeInput",
    "ConfirmPlanChangeResult",
    "CoreBillingDataUnavailableError",
    "CreateCheckoutInput",
    "CreateCheckoutResult",
    "CreditError",
    "CreditEvent",
    "CreditEventEmitter",
    "CreditMetadata",
    "CreditStore",
    "CreditsCapability",
    "CreditsServiceOptions",
    "ExactAmount",
    "ExpressionError",
    "FeatureNotEntitledError",
    "GetInvoiceLinkInput",
    "GrantSubscriptionCycleOptions",
    "InsufficientCreditsError",
    "InvalidOfferQuantityError",
    "LeaseExpiredError",
    "LeaseNotFoundError",
    "LowBalanceConfig",
    "MetricsOrAmount",
    "MissingPlanChangePolicyError",
    "OperationNotAllowedError",
    "ParsedBursarConfig",
    "PaymentMethodRequiredError",
    "PlanChangePreviewResult",
    "PortalSessionInput",
    "PostgresBillingStore",
    "PostgresConnectionOptions",
    "PostgresStore",
    "PreviewPlanChangeInput",
    "PricingEngine",
    "ProviderCapabilityNotSupportedError",
    "ProviderEnvironment",
    "ProviderResponseError",
    "ProviderSelectionError",
    "PublicCatalog",
    "QuotaExceededError",
    "QuoteChangedError",
    "RefundError",
    "RefundResult",
    "ReserveOptions",
    "RunBilledAsyncOptions",
    "RunBilledOptions",
    "SettleOptions",
    "StoreClosedError",
    "StoreError",
    "StoreTimeoutError",
    "StoreUnavailableError",
    "UnknownOfferError",
    "UsageMetrics",
    "bursar_error_http_status",
    "bursar_error_public_message",
    "canonical_bursar_config_dict",
    "canonical_catalog_rollout_dict",
    "is_bursar_error",
    "is_retryable_bursar_error",
    "load_catalog_rollout",
    "load_config_file",
    "load_config_from_dict",
    "project_public_catalog",
    "retry_bursar_operation",
    "retry_bursar_operation_async",
    "validate_catalog_rollout",
]
