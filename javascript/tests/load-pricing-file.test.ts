import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { writeFileSync, unlinkSync, mkdirSync, rmdirSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { ConfigError } from "../src/errors.js";
import { PricingEngine } from "../src/engine.js";

const tmpDir = join(tmpdir(), "bursar-js-test-" + Date.now());

beforeAll(() => {
  mkdirSync(tmpDir, { recursive: true });
  writeFileSync(join(tmpDir, "test.json"), JSON.stringify({ metering: { models: { a: "1" } } }));
  writeFileSync(join(tmpDir, "test.yaml"), 'metering:\n  models:\n    a: "1"\n');
  // LPF1: YAML file with unicode content
  writeFileSync(
    join(tmpDir, "unicode.yaml"),
    'metering:\n  models:\n    "gpt-4-türkçe": "input_tokens * 1"\n    "模型": "output_tokens * 2"\n',
  );
  // LPF2: valid JSON but not a pricing config (missing version and models)
  writeFileSync(join(tmpDir, "notconfig.json"), JSON.stringify({ hello: "world" }));
  writeFileSync(join(tmpDir, "empty.json"), "");
  writeFileSync(join(tmpDir, "empty.yaml"), "");
  writeFileSync(join(tmpDir, "empty-object.json"), "{}");
  mkdirSync(join(tmpDir, "a-directory.json"), { recursive: true });
});

afterAll(() => {
  for (const name of [
    "test.json",
    "test.yaml",
    "unicode.yaml",
    "notconfig.json",
    "empty.json",
    "empty.yaml",
    "empty-object.json",
  ]) {
    try {
      unlinkSync(join(tmpDir, name));
    } catch {
      /* ignore */
    }
  }
  try {
    rmdirSync(join(tmpDir, "a-directory.json"));
  } catch {
    /* ignore */
  }
});

describe("loadPricingFile", () => {
  it("loads JSON file", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    const result = await loadPricingFile(join(tmpDir, "test.json"));
    expect(result.metering.models).toEqual({ a: "1" });
  });

  it("loads YAML file", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    const result = await loadPricingFile(join(tmpDir, "test.yaml"));
    expect(result.metering.models).toEqual({ a: "1" });
  });

  it("throws a clean ConfigError on missing file", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    await expect(loadPricingFile(join(tmpDir, "nope.json"))).rejects.toThrow(ConfigError);
    await expect(loadPricingFile(join(tmpDir, "nope.json"))).rejects.toThrow(/File not found/);
  });

  it("throws a clean ConfigError when the path is a directory", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    await expect(loadPricingFile(join(tmpDir, "a-directory.json"))).rejects.toThrow(
      /is a directory/,
    );
  });

  it("throws a clean ConfigError on an empty JSON file", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    await expect(loadPricingFile(join(tmpDir, "empty.json"))).rejects.toThrow(ConfigError);
  });

  it("throws a clean ConfigError on an empty YAML file", async () => {
    // An empty YAML document parses to `undefined` via js-yaml, not an object.
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    await expect(loadPricingFile(join(tmpDir, "empty.yaml"))).rejects.toThrow(ConfigError);
  });

  it("throws a clean ConfigError on an empty JSON object", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    await expect(loadPricingFile(join(tmpDir, "empty-object.json"))).rejects.toThrow(ConfigError);
  });

  // LPF1 — YAML file with unicode content in model names
  it("LPF1: loads YAML file with unicode string values without error", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    const result = await loadPricingFile(join(tmpDir, "unicode.yaml"));
    expect(result.metering.models).toBeDefined();
    // unicode keys are preserved
    expect(Object.keys(result.metering.models as object)).toContain("gpt-4-türkçe");
    expect(Object.keys(result.metering.models as object)).toContain("模型");
  });

  // LPF2 — Valid JSON but not a pricing config → ConfigError when passed to engine
  it("LPF2: file with {hello: world} (no models) causes ConfigError when loaded into engine", async () => {
    const { loadPricingFile } = await import("../src/load-pricing-file.js");
    const raw = await loadPricingFile(join(tmpDir, "notconfig.json"));
    // loadPricingFile itself only parses; validation happens at the engine level
    expect(() => PricingEngine.fromDict(raw)).toThrow(ConfigError);
  });
});
