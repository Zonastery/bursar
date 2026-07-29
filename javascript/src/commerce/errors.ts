/** Base class for stable, transport-independent commerce failures. */
export class CommerceError extends Error {
  override readonly name: string = "CommerceError";

  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
  }
}

export class CommerceNotConfiguredError extends CommerceError {
  override readonly name = "CommerceNotConfiguredError";

  constructor(message = "Bursar commerce capability is not configured") {
    super(message, "COMMERCE_NOT_CONFIGURED");
  }
}

export class UnknownOfferError extends CommerceError {
  override readonly name = "UnknownOfferError";

  constructor(message = "Unknown commerce offer") {
    super(message, "UNKNOWN_OFFER");
  }
}

export class InvalidOfferQuantityError extends CommerceError {
  override readonly name = "InvalidOfferQuantityError";

  constructor(
    message: string,
    readonly minimum?: number,
    readonly maximum?: number,
  ) {
    super(message, "INVALID_OFFER_QUANTITY");
  }
}

export class ActiveSubscriptionError extends CommerceError {
  override readonly name = "ActiveSubscriptionError";

  constructor(message = "The account already has a blocking subscription") {
    super(message, "ACTIVE_SUBSCRIPTION");
  }
}

export class CheckoutConflictError extends CommerceError {
  override readonly name = "CheckoutConflictError";

  constructor(message = "A checkout is already in progress") {
    super(message, "CHECKOUT_CONFLICT");
  }
}

export class CheckoutCompletedError extends CommerceError {
  override readonly name = "CheckoutCompletedError";

  constructor(message = "The checkout has already completed") {
    super(message, "CHECKOUT_COMPLETED");
  }
}

export class CommerceResourceNotFoundError extends CommerceError {
  override readonly name = "CommerceResourceNotFoundError";

  constructor(message = "Commerce resource not found") {
    super(message, "COMMERCE_RESOURCE_NOT_FOUND");
  }
}

export class ProviderSelectionError extends CommerceError {
  override readonly name = "ProviderSelectionError";

  constructor(message: string) {
    super(message, "PROVIDER_SELECTION_FAILED");
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
    );
  }
}

export class QuoteChangedError<TPreview = unknown> extends CommerceError {
  override readonly name = "QuoteChangedError";

  constructor(readonly preview: TPreview) {
    super(
      "The financial preview changed; review the refreshed quote before confirming",
      "QUOTE_CHANGED",
    );
  }
}

export class MissingPaymentMethodError extends CommerceError {
  override readonly name = "MissingPaymentMethodError";

  constructor(message = "A saved payment method is required") {
    super(message, "PAYMENT_METHOD_REQUIRED");
  }
}

export class MissingPlanChangePolicyError extends CommerceError {
  override readonly name = "MissingPlanChangePolicyError";

  constructor(readonly classification: string) {
    super(
      `The active catalog has no '${classification}' subscription-change policy`,
      "PLAN_CHANGE_POLICY_MISSING",
    );
  }
}

export class CoreBillingDataUnavailableError extends CommerceError {
  override readonly name = "CoreBillingDataUnavailableError";

  constructor(
    message = "Core credit and billing data is temporarily unavailable",
    readonly cause?: unknown,
  ) {
    super(message, "CORE_BILLING_DATA_UNAVAILABLE");
  }
}
