import type Decimal from "decimal.js";

import type {
  BillingAutoRechargeStatus,
  BillingInvoiceInfo,
  BillingPreferences,
  BillingSubscriptionChange,
  BillingSubscriptionState,
} from "../billing/types/index.js";
import type { BucketBalance, GetUserPlanResult, LedgerEntry } from "../credits/types/index.js";
import type {
  ChangePlanPreview,
  PaymentMethodInfo,
  PaymentProvider,
  ResolveUserCallback,
  SavedPaymentChargeResult,
  WebhookResult,
} from "../providers/types.js";
import type { BillingEventSink } from "../billing/contracts.js";
import type { Logger } from "../shared/logger.js";

export interface CommerceProviderFactoryContext {
  eventSink: BillingEventSink;
  identityResolver?: ResolveUserCallback;
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
  providers: Record<string, CommerceProviderFactory>;
  defaultProvider?: string;
  checkoutIntentTtlMs?: number;
  preferenceDefaults?: Partial<CommercePreferenceDefaults>;
  identityResolver?: ResolveUserCallback;
  logger?: Logger | null;
}

export type CommerceCheckoutKind = "subscription" | "credit_pack";

export interface CreateCheckoutInput {
  subjectId: string;
  accountId?: string;
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

export type PlanChangeClassification =
  "unchanged" | "upgrade" | "downgrade" | "lateral" | "cadence_change";

export interface PlanChangePreviewResult {
  unchanged: boolean;
  classification: PlanChangeClassification;
  scheduled: boolean;
  planId: string;
  interval: "month" | "year";
  preview?: ChangePlanPreview;
  quoteFingerprint?: string;
}

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
  transactions: boolean;
  usage: boolean;
  autoRecharge: boolean;
}

export interface AccountCommerceOverview {
  accountId: string;
  credits: {
    ledgerBalance: Decimal;
    effectiveSpendableBalance: Decimal;
    lifetimePurchases: Decimal;
    allowance: {
      remaining: Decimal;
      limit: Decimal;
      periodStart: string | null;
      periodEnd: string | null;
    };
    buckets: BucketBalance[];
  };
  entitlement: GetUserPlanResult;
  subscription: BillingSubscriptionState | null;
  pendingChange: BillingSubscriptionChange | null;
  preferences: BillingPreferences;
  paymentMethods: PaymentMethodInfo[];
  documents: BillingDocumentRef[];
  providerInvoices: BillingInvoiceInfo[];
  transactions: LedgerEntry[];
  usage: LedgerEntry[];
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
    outcome: string;
    charge?: SavedPaymentChargeResult | null;
  }>;
}
