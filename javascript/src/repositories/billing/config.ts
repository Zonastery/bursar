import type { QueryFn } from "../types.js";

export class BillingConfigRepository {
  constructor(private query: QueryFn) {}

  async syncFromConfig(configJson: string): Promise<void> {
    await this.query("SELECT bursar.sync_billing_from_config($1::jsonb)", [configJson]);
  }
}
