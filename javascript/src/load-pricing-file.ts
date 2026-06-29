import { readFileSync } from "fs";
import { ImportError } from "./errors.js";

/** Minimal shape of the `js-yaml` module we rely on (loaded on demand). */
interface YamlModule {
  load(content: string): unknown;
}

/**
 * Narrow an unknown dynamic-import result to the `js-yaml` shape we use.
 *
 * The result of `import()` is treated as `unknown` and validated (L12): the
 * module may expose `load` directly or under a CJS-interop `default` export.
 */
function asYamlModule(mod: unknown): YamlModule {
  const candidate = mod as { load?: unknown; default?: { load?: unknown } };
  if (typeof candidate.load === "function") {
    return candidate as YamlModule;
  }
  if (candidate.default && typeof candidate.default.load === "function") {
    return candidate.default as YamlModule;
  }
  throw new ImportError("js-yaml is installed but does not export a `load` function");
}

/**
 * Read a JSON or YAML pricing config file from disk.
 *
 * Returns the raw parsed dict (suitable for ``loadConfigFromDict`` or
 * ``PricingEngine.fromDict``).
 *
 * For YAML files the optional peer dep ``js-yaml`` is loaded on demand. If it
 * is not installed, an {@link ImportError} is thrown so callers get a clear,
 * typed message (contract §4 / L4).
 */
export async function loadPricingFile(filepath: string): Promise<Record<string, unknown>> {
  if (filepath.endsWith(".yaml") || filepath.endsWith(".yml")) {
    let mod: unknown;
    try {
      mod = await import("js-yaml");
    } catch (cause) {
      throw new ImportError("js-yaml required for YAML files: npm install js-yaml", { cause });
    }
    const yaml = asYamlModule(mod);
    const content = readFileSync(filepath, "utf-8");
    return yaml.load(content) as Record<string, unknown>;
  }

  const content = readFileSync(filepath, "utf-8");
  return JSON.parse(content) as Record<string, unknown>;
}
