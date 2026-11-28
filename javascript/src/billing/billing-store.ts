import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingCreditPostingResult,
  BillingEventClaim,
  BillingOfferResult,
  BillingPaymentRecord,
  BillingPreferences,
  BillingSubscriptionChange,
  BillingSubscriptionChangeInput,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  SubscriptionEntitlementOutcome,
  CheckoutIntent,
  BillingTopupResult,
  BillingInvoiceRecord,
} from "./types/index.js";
import type {
  AutoRechargeAttemptClaim,
  AutoRechargeAttemptUpdate,
  AutoRechargeProviderPaymentUpdate,
  BillingCreditGrantCreate,
  BillingDisputeUpsert,
  BillingInvoiceUpsert,
  BillingPaymentUpsert,
  BillingRefundUpsert,
  BillingSubscriptionChangeUpdate,
  BillingSubscriptionConflictCreate,
  CheckoutIntentCreate,
  CheckoutIntentUpdate,
} from "./contracts.js";
import type { Decimal } from "decimal.js";
import type { ProviderEnvironment } from "../providers/environment.js";
import type { JsonObject } from "../shared/json.js";

/**
 * Abstract billing store — provider-agnostic persistence layer for
 * subscription lifecycle state.
 *
 * Mirrors Python bursar/billing/store.py.
 */
export abstract class BillingStore {
  /** Exposed by environment-partitioned stores so composition can reject drift. */
  readonly providerEnvironment?: ProviderEnvironment;

  abstract createOrGetCheckoutIntent(input: CheckoutIntentCreate): Promise<CheckoutIntent>;

  abstract updateCheckoutIntent(id: string, update: CheckoutIntentUpdate): Promise<void>;

  abstract getCheckoutIntent(id: string, subjectId: string): Promise<CheckoutIntent | null>;

  abstract findCheckoutIntent(
    subjectId: string,
    operationKey: string,
    provider?: string,
  ): Promise<{ intent: CheckoutIntent; catalog: JsonObject } | null>;

  abstract resolveBillingOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
    checkout?: { accountId: string; intentId: string; lookupKey?: string | null },
  ): Promise<BillingOfferResult | null>;

  abstract claimBillingEvent(
    provider: string,
    eventId: string,
    eventType: string,
    envelope?: JsonObject,
  ): Promise<BillingEventClaim>;

  abstract completeBillingEvent(
    provider: string,
    eventId: string,
    claimToken: string,
  ): Promise<boolean>;

  abstract failBillingEvent(
    provider: string,
    eventId: string,
    claimToken: string,
    error?: string,
  ): Promise<boolean>;

  abstract upsertBillingCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void>;

  abstract upsertBillingSubscription(state: BillingSubscriptionState): Promise<void>;

  abstract getBillingCustomer(provider: string, providerCustomerId: string): Promise<string | null>;

  abstract getBillingSubscription(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionState | null>;

  abstract createBillingSubscriptionChange(
    input: BillingSubscriptionChangeInput,
  ): Promise<BillingSubscriptionChange>;

  abstract getOpenBillingSubscriptionChange(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null>;

  abstract listExpiredGraceSubscriptions(
    now: string,
    limit?: number,
  ): Promise<BillingSubscriptionState[]>;

  abstract reconcileSubscriptionEntitlement(
    subjectId: string,
    subscriptionId: string,
    billingEventId: string,
    expectedStatus: BillingSubscriptionStatus,
    expectedProviderUpdatedAt: string,
    planAssignedAt: Date | string | null,
    applyEntitlement: boolean,
    reason: string,
  ): Promise<SubscriptionEntitlementOutcome>;

  abstract expireSubscriptionGracePeriod(
    subjectId: string,
    subscriptionId: string,
    expectedGraceEndsAt: string,
    expiredAt: string,
  ): Promise<boolean>;

  abstract updateBillingSubscriptionChange(
    id: string,
    update: BillingSubscriptionChangeUpdate,
  ): Promise<void>;

  abstract getUserSubscription(
    userId: string,
    statuses?: string[],
  ): Promise<BillingSubscriptionState | null>;

  abstract resolveCreditTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
    checkoutIntentId?: string | null,
    subjectId?: string | null,
    providerPaymentId?: string | null,
    rechargeAttemptId?: string | null,
  ): Promise<BillingTopupResult | null>;
  abstract resolveCreditTopupByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingTopupResult | null>;

  abstract resolveBillingOfferByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingOfferResult | null>;

  abstract computeTopupCredits(
    amountMinor: number,
    topupConfig: BillingTopupResult,
  ): Promise<Decimal>;

  abstract upsertBillingPayment(options: BillingPaymentUpsert): Promise<string>;
  abstract createBillingCreditGrant(input: BillingCreditGrantCreate): Promise<string>;
  abstract grantBillingCredit(
    grantId: string,
    idempotencyKey: string,
  ): Promise<BillingCreditPostingResult>;
  abstract getBillingCreditGrantByPayment(paymentId: string): Promise<string | null>;
  abstract postBillingRefund(
    refundId: string,
    grantId: string,
    amountMinor: number,
    idempotencyKey: string,
  ): Promise<BillingCreditPostingResult>;

  abstract listBillingInvoices(userId: string): Promise<BillingInvoiceRecord[]>;

  abstract upsertBillingRefund(options: BillingRefundUpsert): Promise<string>;

  abstract upsertBillingInvoice(options: BillingInvoiceUpsert): Promise<void>;

  abstract upsertBillingDispute(options: BillingDisputeUpsert): Promise<void>;

  abstract getBillingPayment(
    provider: string,
    providerPaymentId: string,
  ): Promise<BillingPaymentRecord | null>;

  abstract getActiveCatalogDocument(): Promise<JsonObject | null>;
  abstract getActiveCatalogRevision(): Promise<{ id: string; config: JsonObject } | null>;

  abstract pseudonymizeFinancialSubject(userId: string): Promise<boolean>;

  abstract getUserSubscriptions(userId: string): Promise<BillingSubscriptionState[]>;

  abstract recordSubscriptionConflict(input: BillingSubscriptionConflictCreate): Promise<void>;

  abstract getBillingPreferences(userId: string): Promise<BillingPreferences | null>;

  abstract upsertBillingPreferences(prefs: BillingPreferences): Promise<void>;
  abstract getAutoRechargeProfile(userId: string): Promise<BillingAutoRechargeProfile | null>;
  abstract upsertAutoRechargeProfile(
    profile: BillingAutoRechargeProfile,
    options?: { resetCooldown?: boolean },
  ): Promise<void>;
  abstract resumeAutoRechargeProfile(profile: BillingAutoRechargeProfile): Promise<boolean>;
  abstract claimAutoRechargeAttempt(
    input: AutoRechargeAttemptClaim,
  ): Promise<BillingAutoRechargeAttempt | null>;
  abstract updateAutoRechargeAttempt(input: AutoRechargeAttemptUpdate): Promise<void>;
  abstract updateAutoRechargeAttemptByProviderPayment(
    input: AutoRechargeProviderPaymentUpdate,
  ): Promise<void>;
  abstract getAutoRechargeOperationalWindow(userId: string): Promise<{
    maxRecharges: number;
    windowStart: string;
    windowEnd: string;
    count: number;
  } | null>;
  abstract countAutoRechargeAttempts(userId: string, since: string | Date): Promise<number>;

  abstract getBillingCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null>;
}
