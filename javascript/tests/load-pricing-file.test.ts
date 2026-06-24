import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { writeFileSync, unlinkSync, mkdirSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

const tmpDir = join(tmpdir(), "ducto-js-test-" + Date.now());

beforeAll(() => {
  mkdirSync(tmpDir, { recursive: true });
  writeFileSync(join(tmpDir, "test.json"), JSON.stringify({ models: { a: "1" } }));
  writeFileSync(join(tmpDir, "test.yaml"), 'models:\n  a: "1"\n');
});

afterAll(() => {
  try {
    unlinkSync(join(tmpDir, "test.json"));
  } catch {
    /* ignore */
  }
  try {
    unlinkSync(join(tmpDir, "test.yaml"));
  } catch {
    /* ignore */
  }
});

describe("loadPricingFile", () => {
  it("loads JSON file", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    const result = await loadPricingFile(join(tmpDir, "test.json"));
    expect(result.models).toEqual({ a: "1" });
  });

  it("loads YAML file", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    const result = await loadPricingFile(join(tmpDir, "test.yaml"));
    expect(result.models).toEqual({ a: "1" });
  });

  it("throws on missing file", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    await expect(loadPricingFile(join(tmpDir, "nope.json"))).rejects.toThrow();
  });
});
