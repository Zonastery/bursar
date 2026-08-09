import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { writeFileSync, unlinkSync, mkdirSync, rmdirSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { ConfigError } from "../src/errors.js";
import { PricingEngine } from "../src/engine.js";

const tmpDir = join(tmpdir(), "bursar-js-test-" + Date.now());

function objectField(value: Record<string, unknown>, key: string): Record<string, unknown> {
  const field = value[key];
  if (field == null || typeof field !== "object" || Array.isArray(field)) {
    throw new TypeError(`Expected '${key}' to be an object`);
  }
  return field as Record<string, unknown>;
}

beforeAll(() => {
  mkdirSync(tmpDir, { recursive: true });
  writeFileSync(join(tmpDir, "test.json"), JSON.stringify({ config: { values: { a: "1" } } }));
  writeFileSync(join(tmpDir, "test.yaml"), 'config:\n  values:\n    a: "1"\n');
  // YAML file with Unicode content.
  writeFileSync(
    join(tmpDir, "unicode.yaml"),
    'config:\n  names:\n    "gpt-4-türkçe": "fast"\n    "模型": "multilingual"\n',
  );
  // Valid JSON that is not a valid Bursar config.
  writeFileSync(join(tmpDir, "notconfig.json"), JSON.stringify({ hello: "world" }));
  writeFileSync(join(tmpDir, "empty.json"), "");
  writeFileSync(join(tmpDir, "empty.yaml"), "");
  writeFileSync(join(tmpDir, "empty-object.json"), "{}");
  writeFileSync(join(tmpDir, "duplicate.json"), '{"outer":{"a":1,"\\u0061":2}}');
  writeFileSync(join(tmpDir, "unsupported.txt"), "{}");
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
    "duplicate.json",
    "unsupported.txt",
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

describe("loadConfigFile", () => {
  it("loads JSON file", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    const result = await loadConfigFile(join(tmpDir, "test.json"));
    expect(objectField(objectField(result, "config"), "values")).toEqual({ a: "1" });
  });

  it("loads YAML file", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    const result = await loadConfigFile(join(tmpDir, "test.yaml"));
    expect(objectField(objectField(result, "config"), "values")).toEqual({ a: "1" });
  });

  it("throws a clean ConfigError on missing file", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    await expect(loadConfigFile(join(tmpDir, "nope.json"))).rejects.toThrow(ConfigError);
    await expect(loadConfigFile(join(tmpDir, "nope.json"))).rejects.toThrow(
      /Config file not found/,
    );
  });

  it("throws a clean ConfigError when the path is a directory", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    await expect(loadConfigFile(join(tmpDir, "a-directory.json"))).rejects.toThrow(
      /is a directory/,
    );
  });

  it("throws a clean ConfigError on an empty JSON file", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    await expect(loadConfigFile(join(tmpDir, "empty.json"))).rejects.toThrow(ConfigError);
  });

  it("throws a clean ConfigError on an empty YAML file", async () => {
    // An empty YAML document parses to `undefined` via js-yaml, not an object.
    const { loadConfigFile } = await import("../src/load-config-file.js");
    await expect(loadConfigFile(join(tmpDir, "empty.yaml"))).rejects.toThrow(ConfigError);
  });

  it("throws a clean ConfigError on an empty JSON object", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    await expect(loadConfigFile(join(tmpDir, "empty-object.json"))).rejects.toThrow(ConfigError);
  });

  it("rejects nested duplicate JSON keys after escape decoding", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    await expect(loadConfigFile(join(tmpDir, "duplicate.json"))).rejects.toThrow(
      "Invalid JSON in " + join(tmpDir, "duplicate.json") + ": duplicated mapping key",
    );
  });

  it("loads YAML with Unicode string values", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    const result = await loadConfigFile(join(tmpDir, "unicode.yaml"));
    const names = objectField(objectField(result, "config"), "names");
    // unicode keys are preserved
    expect(Object.keys(names)).toContain("gpt-4-türkçe");
    expect(Object.keys(names)).toContain("模型");
  });

  it("leaves schema validation to the config parser", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    const raw = await loadConfigFile(join(tmpDir, "notconfig.json"));
    // loadConfigFile itself only parses; validation happens at the engine level
    expect(() => PricingEngine.fromDict(raw)).toThrow(ConfigError);
  });

  it("rejects unsupported file extensions", async () => {
    const { loadConfigFile } = await import("../src/load-config-file.js");
    await expect(loadConfigFile(join(tmpDir, "unsupported.txt"))).rejects.toThrow(
      /Unsupported config file format/,
    );
  });
});
