import { describe, expect, it, vi } from "vitest";
import { BillingPreferencesRepository } from "../src/billing/postgres/repositories/preferences.js";

describe("BillingPreferencesRepository", () => {
  it("treats PostgreSQL's all-null composite sentinel as not found", async () => {
    const query = vi.fn().mockResolvedValue([
      {
        subject_id: null,
        auto_recharge: null,
        overage_protection: null,
        email_notifications: null,
        usage_alerts: null,
        invoice_reminders: null,
      },
    ]);
    const repository = new BillingPreferencesRepository(query);

    await expect(repository.get("00000000-0000-0000-0000-000000000099")).resolves.toBeNull();
  });
});
