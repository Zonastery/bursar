import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { z } from "zod";

import { AccountService, Bursar, CatalogService } from "../src/bursar.js";
import { CreditsService } from "../src/credits/service.js";
import { BursarRuntime } from "../src/storage/runtime.js";

const contract = z
  .record(z.string(), z.array(z.object({ javascript: z.string(), python: z.string() })))
  .parse(
    JSON.parse(readFileSync(new URL("../../common/facade-contract.json", import.meta.url), "utf8")),
  );

const surfaces = {
  bursar: Bursar,
  catalog: CatalogService,
  accounts: AccountService,
  credits: CreditsService,
  runtime: BursarRuntime,
};

describe("shared facade contract", () => {
  for (const [surface, entries] of Object.entries(contract)) {
    it(`exposes every ${surface} operation`, () => {
      const target = Object.entries(surfaces).find(([name]) => name === surface)?.[1];
      if (!target) throw new Error(`Unknown facade surface '${surface}'`);
      for (const entry of entries) {
        expect(entry.javascript in target.prototype).toBe(true);
      }
    });
  }
});
