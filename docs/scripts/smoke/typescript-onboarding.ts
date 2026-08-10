/** Compile the TypeScript billing-onboarding contract without network or database I/O. */

import {
  BillingEventType,
  Bursar,
  type CommerceProviderFactory,
} from "../../../javascript/src/index.js";
import { StripeProvider } from "../../../javascript/src/providers/stripe/index.js";
import Stripe from "stripe";

const stripe = new Stripe("sk_test_docs_smoke");
const stripeFactory: CommerceProviderFactory = (context) =>
  new StripeProvider({
    getClient: () => stripe,
    webhookSecret: "whsec_docs_smoke",
    eventSink: context.eventSink,
  });

const bursar = new Bursar({
  creditStore: {} as never,
  billingStore: {} as never,
  commerceOptions: {
    tenantId: "018f7f5f-7b4a-7000-8000-000000000001",
    providerEnvironment: "test",
    defaultProvider: "stripe",
    providers: { stripe: stripeFactory },
  },
});
const commerce = bursar.requireCommerce();
const actorId = "actor_docs_smoke";
const accountId = "account_docs_smoke";

void commerce.createCheckout({
  subjectId: actorId,
  accountId,
  offerKey: "pro_monthly",
  returnUrl: "https://app.example.com/billing/success",
  cancelUrl: "https://app.example.com/billing",
  operationKey: "checkout:docs-smoke",
});

void bursar.credits.addCredits(
  "11111111-1111-4111-8111-111111111111",
  "100.000000",
  {
    type: "purchase",
    idempotencyKey: "purchase:docs-smoke",
  },
);
void bursar.credits.deduct(
  "11111111-1111-4111-8111-111111111111",
  {
    operation: "completion",
    measures: { input_tokens: "800" },
    dimensions: { model: "example-model" },
  },
  { idempotencyKey: "request:docs-smoke" },
);

export async function verifiedWebhookHandler(
  request: Request,
): Promise<Response> {
  const result = await commerce.handleWebhook({
    provider: "stripe",
    rawBody: await request.text(),
    headers: Object.fromEntries(request.headers.entries()),
  });
  const status = result.received ? 200 : result.retryable ? 503 : 400;
  return Response.json({ received: result.received }, { status });
}

void BillingEventType.SUBSCRIPTION_CREATED;
void BillingEventType.SUBSCRIPTION_RENEWED;
