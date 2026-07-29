from __future__ import annotations

from typing import Any


class CommerceError(Exception):
    """Base commerce-domain error with a stable transport-neutral code."""

    code = "COMMERCE_ERROR"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code.lower())


class CommerceNotConfiguredError(CommerceError):
    code = "COMMERCE_NOT_CONFIGURED"


class UnknownOfferError(CommerceError):
    code = "UNKNOWN_OFFER"


class InvalidOfferQuantityError(CommerceError):
    code = "INVALID_OFFER_QUANTITY"

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


class CheckoutConflictError(CommerceError):
    code = "CHECKOUT_CONFLICT"


class CheckoutCompletedError(CommerceError):
    code = "CHECKOUT_COMPLETED"


class CommerceResourceNotFoundError(CommerceError):
    code = "COMMERCE_RESOURCE_NOT_FOUND"


class ProviderSelectionError(CommerceError):
    code = "PROVIDER_SELECTION_FAILED"


class ProviderCapabilityNotSupportedError(CommerceError):
    code = "PROVIDER_CAPABILITY_NOT_SUPPORTED"

    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(f"Provider {provider!r} does not support {capability}")
        self.provider = provider
        self.capability = capability


class QuoteChangedError(CommerceError):
    code = "QUOTE_CHANGED"

    def __init__(self, preview: Any) -> None:
        super().__init__("The financial preview changed")
        self.preview = preview


class MissingPaymentMethodError(CommerceError):
    code = "PAYMENT_METHOD_REQUIRED"


class MissingPlanChangePolicyError(CommerceError):
    code = "PLAN_CHANGE_POLICY_MISSING"

    def __init__(self, classification: str) -> None:
        super().__init__(f"No subscription-change policy exists for {classification!r}")
        self.classification = classification


class CoreBillingDataUnavailableError(CommerceError):
    code = "CORE_BILLING_DATA_UNAVAILABLE"
