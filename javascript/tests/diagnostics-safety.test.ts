import { describe, expect, it } from "vitest";

import { BursarError } from "../src/errors.js";
import { persistedDiagnosticSummary } from "../src/shared/diagnostics.js";

describe("persistedDiagnosticSummary", () => {
  it("keeps a structured Bursar code without its message", () => {
    const error = new BursarError("token=secret and account=123");

    expect(persistedDiagnosticSummary(error, "outbox_delivery_failed")).toBe(
      "outbox_delivery_failed:BURSAR_ERROR",
    );
  });

  it("keeps only a normalized native error type", () => {
    expect(
      persistedDiagnosticSummary(
        new Error("https://user:password@example.test/path?token=secret"),
        "billing event failed",
      ),
    ).toBe("billing_event_failed:Error");
  });

  it("does not persist arbitrary string diagnostics", () => {
    expect(persistedDiagnosticSummary("customer payload: secret")).toBe(
      "operation_failed:UnknownError",
    );
  });

  it("rejects attacker-controlled codes and throwing getters", () => {
    const arbitrary = Object.assign(new Error("private"), { code: "customer_12345" });
    const mutatedBursar = new BursarError("private");
    Object.defineProperty(mutatedBursar, "code", { value: "customer_12345" });
    const throwing = Object.create(Error.prototype, {
      name: {
        get: () => {
          throw new Error("name getter secret");
        },
      },
      code: {
        get: () => {
          throw new Error("code getter secret");
        },
      },
    });

    expect(persistedDiagnosticSummary(arbitrary)).toBe("operation_failed:Error");
    expect(persistedDiagnosticSummary(mutatedBursar)).toBe("operation_failed:BursarError");
    expect(persistedDiagnosticSummary(throwing)).toBe("operation_failed:Error");
  });
});
