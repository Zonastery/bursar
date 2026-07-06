import { readFileSync, statSync } from "fs";
import { ConfigError, ImportError } from "./errors.js";

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
 * Read a file's contents, converting missing-file/directory/permission
 * failures into a clean {@link ConfigError} instead of a raw Node `fs` error
 * (mirrors the Python CLI's `_load_pricing_file`, `__main__.py:162-198`).
 */
function readFileClean(filepath: string): string {
  let stat: ReturnType<typeof statSync> | undefined;
  try {
    stat = statSync(filepath);
  } catch (cause) {
    const code = (cause as NodeJS.ErrnoException).code;
    if (code === "ENOENT") throw new ConfigError(`File not found: ${filepath}`);
    if (code === "EACCES") throw new ConfigError(`Permission denied: ${filepath}`);
    throw new ConfigError(`Could not read ${filepath}: ${(cause as Error).message}`);
  }
  if (stat.isDirectory()) {
    throw new ConfigError(`Not a file (is a directory): ${filepath}`);
  }
  try {
    return readFileSync(filepath, "utf-8");
  } catch (cause) {
    throw new ConfigError(`Could not read ${filepath}: ${(cause as Error).message}`);
  }
}

/** Guard against an empty file or a non-object parse result (e.g. an empty YAML document). */
function assertNonEmptyObject(data: unknown, filepath: string): Record<string, unknown> {
  if (data == null) {
    throw new ConfigError(`Pricing config is empty: ${filepath}`);
  }
  if (typeof data !== "object" || Array.isArray(data)) {
    throw new ConfigError(`Pricing config must be a JSON/YAML object, got ${typeof data}: ${filepath}`);
  }
  if (Object.keys(data).length === 0) {
    throw new ConfigError(`Pricing config is empty: ${filepath}`);
  }
  return data as Record<string, unknown>;
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
    const content = readFileClean(filepath);
    let parsed: unknown;
    try {
      parsed = yaml.load(content);
    } catch (cause) {
      throw new ConfigError(`Invalid YAML in ${filepath}: ${(cause as Error).message}`);
    }
    return assertNonEmptyObject(parsed, filepath);
  }

  const content = readFileClean(filepath);
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch (cause) {
    throw new ConfigError(`Invalid JSON in ${filepath}: ${(cause as Error).message}`);
  }
  return assertNonEmptyObject(parsed, filepath);
}
