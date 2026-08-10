import type { Decimal } from "decimal.js";

import type {
  BillingAutoRechargeStatus,
  BillingInvoiceRecord,
  BillingPreferences,
  BillingSubscriptionChange,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
} from "../billing/types/index.js";
import type {
  BucketBalance,
  GetUserPlanResult,
  LedgerEntry,
  UsageCharge,
} from "../credits/types/index.js";
import type {
  ChangePlanPreview,
  PaymentMethodInfo,
  PaymentProvider,
  SavedPaymentChargeResult,
  WebhookResult,
} from "../providers/types.js";
import type { BillingEventSink } from "../billing/contracts.js";
import type { AutoRechargeOutcome } from "../billing/auto-recharge-service.js";
import type { Logger } from "../shared/logger.js";
import type { ProviderEnvironment } from "../providers/environment.js";

export interface CommerceProviderFactoryContext {
  tenantId?: string;
  providerEnvironment: ProviderEnvironment;
  eventSink: BillingEventSink;
}

export type CommerceProviderFactory = (
  context: CommerceProviderFactoryContext,
) => PaymentProvider | Promise<PaymentProvider>;

export interface CommercePreferenceDefaults {
  autoRecharge: boolean;
  overageProtection: boolean;
  emailNotifications: boolean;
  usageAlerts: boolean;
  invoiceReminders: boolean;
}

export interface CommerceOptions {
  tenantId?: string;
  /** Explicit financial namespace shared with billing persistence. */
  providerEnvironment: ProviderEnvironment;
  providers: Record<string, CommerceProviderFactory>;
  defaultProvider?: string;
  checkoutIntentTtlMs?: number;
  preferenceDefaults?: Partial<CommercePreferenceDefaults>;
  logger?: Logger | null;
}

export type CommerceCheckoutKind = "subscription" | "credit_pack";

export interface CreateCheckoutInput {
  /** Authenticated actor that owns and may inspect the checkout intent. */
  subjectId: string;
  /** Financial subject that receives the subscription or purchased credits. */
  accountId: string;
  email?: string;
  offerKey: string;
  provider?: string;
  type?: CommerceCheckoutKind;
  quantity?: number;
  returnUrl: string;
  cancelUrl: string;
  operationKey: string;
  metadata?: Record<string, string>;
}

export interface CreateCheckoutResult {
  intentId: string;
  url: string;
  provider: string;
  offerKey: string;
}

export type CommerceCheckoutStatus = "pending" | "succeeded" | "failed" | "expired";

export interface CheckoutStatusResult {
  intentId: string;
  status: CommerceCheckoutStatus;
}

export interface PreferencePatch {
  autoRecharge?: boolean;
  overageProtection?: boolean;
  emailNotifications?: boolean;
  usageAlerts?: boolean;
  invoiceReminders?: boolean;
}

export interface SubscriptionCommandResult {
  ok: true;
  pending?: boolean;
}

export type SubscriptionAccessState = "entitled" | "grace" | "blocked" | "none";

export interface NormalizedPendingPlanChange {
  planKey: string;
  interval: "month" | "year";
  effectiveAt: string;
  scheduled: boolean;
  providerOperationId: string | null;
}

export interface AccountSubscriptionSummary {
  accountId: string;
  planKey: string | null;
  status: BillingSubscriptionStatus | null;
  lifecycleState: BillingSubscriptionStatus | "none";
  accessState: SubscriptionAccessState;
  isCurrent: boolean;
  isEntitled: boolean;
  isBlockingCheckout: boolean;
  isCancellable: boolean;
  isTerminal: boolean;
  subscription: BillingSubscriptionState | null;
  pendingChange: NormalizedPendingPlanChange | null;
}

export interface CancelSubscriptionResult {
  provider: string;
  providerSubscriptionId: string;
  canceled: boolean;
  error: string | null;
}

export interface CancelAllSubscriptionsResult {
  accountId: string;
  canceledCount: number;
  subscriptions: CancelSubscriptionResult[];
}

export type PlanChangeClassification =
  "unchanged" | "upgrade" | "downgrade" | "lateral" | "cadence_change";

interface PlanChangePreviewResultBase {
  planId: string;
  interval: "month" | "year";
}

/**
 * A plan-change preview. The discriminant makes it impossible to confirm an
 * unchanged plan or accidentally omit the provider quote from a real change.
 */
export type PlanChangePreviewResult =
  | (PlanChangePreviewResultBase & {
      unchanged: true;
      classification: "unchanged";
      scheduled: false;
    })
  | (PlanChangePreviewResultBase & {
      unchanged: false;
      classification: Exclude<PlanChangeClassification, "unchanged">;
      scheduled: boolean;
      preview: ChangePlanPreview;
      quoteFingerprint: string;
    });

export interface PreviewPlanChangeInput {
  accountId: string;
  offerKey: string;
}

export interface ConfirmPlanChangeInput extends PreviewPlanChangeInput {
  quoteFingerprint: string;
  operationKey: string;
}

export interface ConfirmPlanChangeResult {
  success: true;
  unchanged?: boolean;
  pending?: boolean;
  scheduled?: boolean;
  effectiveAt?: string;
  planId: string;
  interval: "month" | "year";
}

export interface PortalSessionInput {
  accountId: string;
  purpose?: "billing" | "payment-method";
  returnUrl: string;
  cancelUrl?: string;
}

export interface BillingDocumentInvoiceRef {
  kind: "provider_invoice";
  provider: string;
  providerDocumentId: string;
  status?: string | null;
  amountPaidMinor?: number | null;
  amountDueMinor?: number | null;
  currency?: string | null;
  periodStart?: string | null;
  periodEnd?: string | null;
}

export interface BillingDocumentLedgerRef {
  kind: "ledger_entry";
  ledgerEntryId: string;
  provider?: string | null;
  providerDocumentId?: string | null;
  createdAt: string;
  entryType: string;
  amount: Decimal;
}

export type BillingDocumentRef = BillingDocumentInvoiceRef | BillingDocumentLedgerRef;

export type BillingDocumentLocator =
  | Pick<BillingDocumentInvoiceRef, "kind" | "provider" | "providerDocumentId">
  | Pick<BillingDocumentLedgerRef, "kind" | "ledgerEntryId">;

export interface CommerceSectionAvailability {
  paymentMethods: boolean;
  documents: boolean;
  providerInvoices: boolean;
  transactions: boolean;
  usage: boolean;
  autoRecharge: boolean;
}

export interface CreditSpendSource {
  type: "allowance" | "bucket";
  key: string;
  label: string;
  priority: number;
}

export interface AccountCreditDisplay {
  currency: string;
  unitsPerMajor: Decimal;
}

export interface AccountCommerceOverview {
  accountId: string;
  credits: {
    ledgerBalance: Decimal;
    effectiveSpendableBalance: Decimal;
    lifetimePurchases: Decimal;
    allowance: {
      remaining: Decimal;
      limit: Decimal | null;
      periodStart: string | null;
      periodEnd: string | null;
    };
    buckets: BucketBalance[];
    bucketsByKey: Record<string, Decimal>;
    spendOrder: CreditSpendSource[];
    display: AccountCreditDisplay | null;
  };
  entitlement: GetUserPlanResult;
  subscriptionSummary: AccountSubscriptionSummary;
  subscription: BillingSubscriptionState | null;
  pendingChange: BillingSubscriptionChange | null;
  preferences: BillingPreferences;
  paymentMethods: PaymentMethodInfo[];
  documents: BillingDocumentRef[];
  providerInvoices: BillingInvoiceRecord[];
  transactions: LedgerEntry[];
  usage: UsageCharge[];
  autoRecharge: BillingAutoRechargeStatus | null;
  availability: CommerceSectionAvailability;
}

export interface GetInvoiceLinkInput {
  accountId: string;
  document: BillingDocumentLocator;
}

export interface CommerceWebhookInput {
  provider?: string;
  rawBody: string;
  headers: Record<string, string>;
}

export type CommerceWebhookResult = WebhookResult;

export interface AutoRechargeInput {
  accountId: string;
  returnUrl?: string;
}

export interface CommerceAutoRecharge {
  getStatus(input: Pick<AutoRechargeInput, "accountId">): Promise<BillingAutoRechargeStatus | null>;
  enable(input: AutoRechargeInput): Promise<BillingAutoRechargeStatus | null>;
  disable(input: Pick<AutoRechargeInput, "accountId">): Promise<void>;
  retry(input: AutoRechargeInput): Promise<BillingAutoRechargeStatus | null>;
  processIfNeeded(input: AutoRechargeInput): Promise<{
    outcome: AutoRechargeOutcome;
    charge?: SavedPaymentChargeResult | null;
  }>;
}
