import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { z } from "zod";

import { loadConfigFromDict } from "../src/config.js";
import {
  InvalidOfferQuantityError,
  ProviderCapabilityNotSupportedError,
  QuoteChangedError,
  UnknownOfferError,
  classifySubscriptionChange,
} from "../src/commerce/index.js";
import { CommerceProviderRegistry } from "../src/commerce/provider-registry.js";
import type { BillingEventSink } from "../src/billing/contracts.js";
import type { PaymentProvider } from "../src/providers/types.js";

const parityFixtureSchema = z
  .object({
    catalog: z.record(z.string(), z.json()),
    transitions: z.array(
      z
        .object({
          current_plan: z.string(),
          current_interval: z.string(),
          target_offer: z.string(),
          classification: z.string(),
          effective: z.string().nullable(),
          proration: z.string().nullable(),
        })
        .strict(),
    ),
    error_codes: z.record(z.string(), z.string()),
    public_contract: z.record(z.string(), z.string()),
  })
  .strict();

const fixture = parityFixtureSchema.parse(
  JSON.parse(readFileSync(new URL("../../common/commerce-parity.json", import.meta.url), "utf8")),
);

const eventSink: BillingEventSink = {
  ingestBillingEvent: async () => ({ handled: true }),
};

function invalidProvider(): PaymentProvider {
  // SAFETY: This deliberately incomplete provider exercises registry validation.
  return { provider: "alpha" } as PaymentProvider;
}

function minimalProvider(): PaymentProvider {
  return {
    provider: "alpha",
    async createCheckoutSession(params) {
      return { url: params.returnUrl };
    },
    async handleWebhook() {
      return {
        received: true,
        retryable: false,
        provider: "alpha",
        eventId: null,
        eventType: null,
      };
    },
  };
}

describe("shared commerce parity fixture", () => {
  it("classifies transitions from rank and cadence", () => {
    const config = loadConfigFromDict(fixture.catalog);
    for (const transition of fixture.transitions) {
      const offer = config.commerce.offers[transition.target_offer];
      expect(offer?.type).toBe("subscription");
      if (!offer || offer.type !== "subscription") continue;
      const result = classifySubscriptionChange(
        config,
        transition.current_plan,
        transition.current_interval,
        offer,
      );
      expect(result.classification).toBe(transition.classification);
      expect(result.policy?.effective ?? null).toBe(transition.effective);
      expect(result.policy?.proration ?? null).toBe(transition.proration);
    }
  });

  it("keeps stable error codes and a provider-agnostic public contract", () => {
    expect(new UnknownOfferError("Unknown offer").code).toBe(fixture.error_codes.unknown_offer);
    expect(new InvalidOfferQuantityError("Invalid offer quantity").code).toBe(
      fixture.error_codes.invalid_quantity,
    );
    expect(new QuoteChangedError({}).code).toBe(fixture.error_codes.quote_changed);
    expect(new ProviderCapabilityNotSupportedError("alpha", "portal").code).toBe(
      fixture.error_codes.provider_capability,
    );
    expect(fixture.public_contract).toEqual({
      offer_input: "offerKey",
      quote_field: "quoteFingerprint",
      provider_product_ids: "provider_internal",
    });
  });

  it("keeps application names and provider identifiers out of public Commerce inputs", () => {
    const serviceSource = readFileSync(
      new URL("../src/commerce/service.ts", import.meta.url),
      "utf8",
    );
    const typesSource = readFileSync(new URL("../src/commerce/types.ts", import.meta.url), "utf8");

    expect(serviceSource).not.toMatch(
      /\b(Zonastery|seeker|monk|sage|gifted|purchased)\b|from ["']next|https?:\/\//,
    );
    expect(serviceSource).not.toMatch(/defaultProvider\s*(?:\?\?|=)\s*["']dodo["']/);
    expect(typesSource).not.toMatch(/\b(productId|quoteHash)\b/);
  });

  it("validates provider factories and preserves newer loads when a cleared load fails", async () => {
    const invalid = new CommerceProviderRegistry(
      { providerEnvironment: "test", providers: { alpha: () => invalidProvider() } },
      { providerEnvironment: "test", eventSink },
    );
    await expect(invalid.get("alpha")).rejects.toThrow("did not return a valid payment provider");

    let calls = 0;
    let rejectFirst!: () => void;
    const firstRelease = new Promise<void>((resolve) => {
      rejectFirst = resolve;
    });
    const registry = new CommerceProviderRegistry(
      {
        providerEnvironment: "test",
        providers: {
          alpha: async () => {
            calls += 1;
            if (calls === 1) {
              await firstRelease;
              throw new Error("stale load failed");
            }
            return minimalProvider();
          },
        },
      },
      { providerEnvironment: "test", eventSink },
    );

    const stale = registry.get("alpha");
    registry.clear();
    const current = await registry.get("alpha");
    rejectFirst();
    await expect(stale).rejects.toThrow("stale load failed");

    expect(await registry.get("alpha")).toBe(current);
    expect(calls).toBe(2);
  });

  it("rejects ambiguous provider registry configuration at construction", () => {
    expect(
      () =>
        new CommerceProviderRegistry(
          { providerEnvironment: "test", providers: {} },
          { providerEnvironment: "test", eventSink },
        ),
    ).toThrow("At least one payment provider must be registered");
    expect(
      () =>
        new CommerceProviderRegistry(
          { providerEnvironment: "test", providers: { " ": () => minimalProvider() } },
          { providerEnvironment: "test", eventSink },
        ),
    ).toThrow("Payment provider names must not be empty");
    expect(
      () =>
        new CommerceProviderRegistry(
          {
            providerEnvironment: "test",
            providers: { alpha: () => minimalProvider() },
            defaultProvider: " ",
          },
          { providerEnvironment: "test", eventSink },
        ),
    ).toThrow("Default payment provider must not be empty");
  });
});
