from __future__ import annotations

from typing import Any

from bursar.errors import BursarError, BursarErrorCategory


class CommerceError(BursarError):
    """Base commerce-domain error with a stable transport-neutral code."""

    code = "COMMERCE_ERROR"
    category: BursarErrorCategory = "internal"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code.lower())


class CommerceNotConfiguredError(CommerceError):
    code = "COMMERCE_NOT_CONFIGURED"
    category: BursarErrorCategory = "unavailable"


class UnknownOfferError(CommerceError):
    code = "UNKNOWN_OFFER"
    category: BursarErrorCategory = "invalid_request"


class InvalidOfferQuantityError(CommerceError):
    code = "INVALID_OFFER_QUANTITY"
    category: BursarErrorCategory = "invalid_request"

    def __init__(
        self,
        message: str = "Invalid offer quantity",
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> None:
        super().__init__(message)
        self.minimum = minimum
        self.maximum = maximum


class ActiveSubscriptionError(CommerceError):
    code = "ACTIVE_SUBSCRIPTION"
    category: BursarErrorCategory = "conflict"


class CheckoutConflictError(CommerceError):
    code = "CHECKOUT_CONFLICT"
    category: BursarErrorCategory = "conflict"


class CheckoutCompletedError(CommerceError):
    code = "CHECKOUT_COMPLETED"
    category: BursarErrorCategory = "conflict"


class CommerceResourceNotFoundError(CommerceError):
    code = "COMMERCE_RESOURCE_NOT_FOUND"
    category: BursarErrorCategory = "not_found"


class ProviderSelectionError(CommerceError):
    code = "PROVIDER_SELECTION_FAILED"
    category: BursarErrorCategory = "unavailable"


class ProviderCapabilityNotSupportedError(CommerceError):
    code = "PROVIDER_CAPABILITY_NOT_SUPPORTED"
    category: BursarErrorCategory = "unavailable"

    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(f"Provider {provider!r} does not support {capability}")
        self.provider = provider
        self.capability = capability


class QuoteChangedError(CommerceError):
    code = "QUOTE_CHANGED"
    category: BursarErrorCategory = "conflict"

    def __init__(self, preview: Any) -> None:
        super().__init__("The financial preview changed")
        self.preview = preview


class MissingPaymentMethodError(CommerceError):
    code = "PAYMENT_METHOD_REQUIRED"
    category: BursarErrorCategory = "payment_required"


class MissingPlanChangePolicyError(CommerceError):
    code = "PLAN_CHANGE_POLICY_MISSING"
    category: BursarErrorCategory = "unavailable"

    def __init__(self, classification: str) -> None:
        super().__init__(f"No subscription-change policy exists for {classification!r}")
        self.classification = classification


class CoreBillingDataUnavailableError(CommerceError):
    code = "CORE_BILLING_DATA_UNAVAILABLE"
    category: BursarErrorCategory = "unavailable"
    retryable = True
