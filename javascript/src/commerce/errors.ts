import { BursarError, type BursarErrorCategory, type BursarErrorOptions } from "../errors.js";

/** Base class for stable, transport-independent commerce failures. */
export class CommerceError extends BursarError {
  override readonly name: string = "CommerceError";

  constructor(
    message: string,
    override readonly code: string,
    override readonly category: BursarErrorCategory = "internal",
    override readonly retryable: boolean = false,
    options: BursarErrorOptions = {},
  ) {
    super(message, options);
  }
}

export class CommerceNotConfiguredError extends CommerceError {
  override readonly name = "CommerceNotConfiguredError";

  constructor(message = "Bursar commerce capability is not configured") {
    super(message, "COMMERCE_NOT_CONFIGURED", "unavailable");
  }
}

export class UnknownOfferError extends CommerceError {
  override readonly name = "UnknownOfferError";

  constructor(message = "Unknown commerce offer") {
    super(message, "UNKNOWN_OFFER", "invalid_request");
  }
}

export class InvalidOfferQuantityError extends CommerceError {
  override readonly name = "InvalidOfferQuantityError";

  constructor(
    message: string,
    readonly minimum?: number,
    readonly maximum?: number,
  ) {
    super(message, "INVALID_OFFER_QUANTITY", "invalid_request");
  }
}

export class ActiveSubscriptionError extends CommerceError {
  override readonly name = "ActiveSubscriptionError";

  constructor(message = "The account already has a blocking subscription") {
    super(message, "ACTIVE_SUBSCRIPTION", "conflict");
  }
}

export class CheckoutConflictError extends CommerceError {
  override readonly name = "CheckoutConflictError";

  constructor(message = "A checkout is already in progress") {
    super(message, "CHECKOUT_CONFLICT", "conflict");
  }
}

export class CheckoutCompletedError extends CommerceError {
  override readonly name = "CheckoutCompletedError";

  constructor(message = "The checkout has already completed") {
    super(message, "CHECKOUT_COMPLETED", "conflict");
  }
}

export class CommerceResourceNotFoundError extends CommerceError {
  override readonly name = "CommerceResourceNotFoundError";

  constructor(message = "Commerce resource not found") {
    super(message, "COMMERCE_RESOURCE_NOT_FOUND", "not_found");
  }
}

export class ProviderSelectionError extends CommerceError {
  override readonly name = "ProviderSelectionError";

  constructor(message: string) {
    super(message, "PROVIDER_SELECTION_FAILED", "unavailable");
  }
}

export class ProviderCapabilityNotSupportedError extends CommerceError {
  override readonly name = "ProviderCapabilityNotSupportedError";

  constructor(
    readonly provider: string,
    readonly capability: string,
  ) {
    super(
      `Payment provider '${provider}' does not support '${capability}'`,
      "PROVIDER_CAPABILITY_NOT_SUPPORTED",
      "unavailable",
    );
  }
}

export class QuoteChangedError<TPreview = unknown> extends CommerceError {
  override readonly name = "QuoteChangedError";

  constructor(readonly preview: TPreview) {
    super(
      "The financial preview changed; review the refreshed quote before confirming",
      "QUOTE_CHANGED",
      "conflict",
    );
  }
}

export class MissingPaymentMethodError extends CommerceError {
  override readonly name = "MissingPaymentMethodError";

  constructor(message = "A saved payment method is required") {
    super(message, "PAYMENT_METHOD_REQUIRED", "payment_required");
  }
}

export class MissingPlanChangePolicyError extends CommerceError {
  override readonly name = "MissingPlanChangePolicyError";

  constructor(readonly classification: string) {
    super(
      `The active catalog has no '${classification}' subscription-change policy`,
      "PLAN_CHANGE_POLICY_MISSING",
      "unavailable",
    );
  }
}

export class CoreBillingDataUnavailableError extends CommerceError {
  override readonly name = "CoreBillingDataUnavailableError";

  constructor(
    message = "Core credit and billing data is temporarily unavailable",
    cause?: unknown,
  ) {
    super(message, "CORE_BILLING_DATA_UNAVAILABLE", "unavailable", true, { cause });
  }
}
