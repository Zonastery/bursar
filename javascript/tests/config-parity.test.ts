/**
 * Cross-SDK config-validation parity test — JavaScript side.
 *
 * Loads `tests/parity/config_validation_cases.json` and asserts
 * `loadConfigFromDict` accepts/rejects each case exactly as documented. The
 * Python counterpart (`python/tests/test_config_parity.py`) runs the same
 * fixture through `load_config_from_dict` — this is the guard against the
 * Python<->JS validation drift found in the config schema review (missing
 * `version` check, unvalidated `signup_bonus`/`allowance_amount` sign,
 * silently-ignored unknown keys, dropped `per_operation`, etc).
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { loadConfigFromDict } from "../src/config.js";
import { ConfigError } from "../src/errors.js";
import { z } from "zod";

const __dirname = dirname(fileURLToPath(import.meta.url));
const fixturePath = resolve(__dirname, "../../tests/parity/config_validation_cases.json");

const configCaseSchema = z.object({
  name: z.string(),
  expect: z.enum(["accept", "reject"]),
  reason: z.string().optional(),
  config: z.record(z.string(), z.json()),
});
const fixture = z
  .object({ cases: z.array(configCaseSchema) })
  .parse(JSON.parse(readFileSync(fixturePath, "utf8")));

describe("parity fixture — config_validation_cases", () => {
  for (const c of fixture.cases) {
    it(c.name, () => {
      if (c.expect === "accept") {
        expect(() => loadConfigFromDict(c.config)).not.toThrow();
      } else {
        expect(() => loadConfigFromDict(c.config)).toThrow(ConfigError);
      }
    });
  }
});
