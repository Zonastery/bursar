import assert from "node:assert/strict";
import { CreditsService, PostgresStore } from "@zonastery/bursar";
import { Decimal } from "decimal.js";

const tenantId = "00000000-0000-4000-8000-000000000201";
const subjectId = "00000000-0000-4000-8000-000000000212";
const store = new PostgresStore({
  postgres: process.env.DATABASE_URL,
  tenantId,
  providerEnvironment: "test",
  applicationName: "bursar-npm-package-smoke",
});

try {
  const service = new CreditsService(store);
  assert.equal((await service.getActiveCatalog())?.version, 1);
  const first = await service.addCredits(subjectId, new Decimal(11), {
    type: "purchase",
    idempotencyKey: "package-smoke:npm:grant",
  });
  const replay = await service.addCredits(subjectId, new Decimal(11), {
    type: "purchase",
    idempotencyKey: "package-smoke:npm:grant",
  });
  assert.equal(replay.entryId, first.entryId);
  assert.equal((await service.getBalance(subjectId)).balance.toString(), "11");
} finally {
  await store.close();
}
