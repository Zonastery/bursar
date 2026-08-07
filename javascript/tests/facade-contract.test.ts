import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { AccountService, Bursar, CatalogService } from "../src/bursar.js";
import { CreditsService } from "../src/credits/service.js";
import { BursarRuntime } from "../src/storage/runtime.js";

interface ContractEntry {
  javascript: string;
  python: string;
}

const contract = JSON.parse(
  readFileSync(new URL("../../common/facade-contract.json", import.meta.url), "utf8"),
) as Record<string, ContractEntry[]>;

const surfaces: Record<string, { prototype: object }> = {
  bursar: Bursar,
  catalog: CatalogService,
  accounts: AccountService,
  credits: CreditsService,
  runtime: BursarRuntime,
};

describe("shared facade contract", () => {
  for (const [surface, entries] of Object.entries(contract)) {
    it(`exposes every ${surface} operation`, () => {
      for (const entry of entries) {
        expect(entry.javascript in surfaces[surface]!.prototype).toBe(true);
      }
    });
  }
});
