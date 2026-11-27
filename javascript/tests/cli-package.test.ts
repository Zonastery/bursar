import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { z } from "zod";

const root = resolve(import.meta.dirname, "..");
let temporary: string;
let cli: string;
const run = (...args: string[]) =>
  spawnSync(process.execPath, [cli, ...args], { cwd: temporary, encoding: "utf8" });

describe("npm package config executable", () => {
  beforeAll(() => {
    execFileSync("bun", ["run", "build"], { cwd: root, stdio: "pipe" });
    temporary = mkdtempSync(join(root, ".cli-test-"));
    const packed = z.array(z.object({ filename: z.string() })).parse(
      JSON.parse(
        execFileSync(
          "npm",
          [
            "pack",
            "--ignore-scripts",
            "--json",
            "--cache",
            join(temporary, "cache"),
            "--pack-destination",
            temporary,
          ],
          {
            cwd: root,
            encoding: "utf8",
          },
        ),
      ),
    );
    execFileSync("tar", ["-xzf", join(temporary, packed[0]!.filename), "-C", temporary]);
    cli = join(temporary, "package", "dist", "cli.js");
  }, 60_000);
  afterAll(() => rmSync(temporary, { recursive: true, force: true }));

  it("ships the exact starter and schema and quotes it offline", () => {
    expect(
      readFileSync(
        join(temporary, "package", "dist", "generated", "pricing-config.schema.json"),
        "utf8",
      ),
    ).toContain('"version"');
    expect(run("init").status).toBe(0);
    const starter = readFileSync(join(temporary, "bursar.yaml"), "utf8");
    expect(starter).toBe(readFileSync(resolve(root, "../python/src/bursar/starter.yaml"), "utf8"));
    expect(starter).toContain('on_signup: "20"');
    expect(JSON.parse(run("validate", "bursar.yaml").stdout)).toEqual({ valid: true, errors: [] });
    expect(
      JSON.parse(run("quote", "bursar.yaml", "--operation", "export_report").stdout).total,
    ).toBe("2.000000");
    expect(run("init").status).toBe(1);
    expect(readFileSync(join(temporary, "bursar.yaml"), "utf8")).toBe(starter);
  });

  it("reports errors as JSON and rejects unknown options", () => {
    writeFileSync(join(temporary, "invalid.yaml"), "version: 1\ninvalid: true\n");
    const invalid = run("validate", "invalid.yaml");
    expect(invalid.status).toBe(1);
    expect(JSON.parse(invalid.stderr).error.code).toBe("CONFIG_ERROR");
    const unknown = run("quote", "invalid.yaml", "--unknown");
    expect(unknown.status).toBe(1);
    expect(JSON.parse(unknown.stderr).error.message).toContain("--unknown");
    expect(run("--help").stdout).toContain("bursar-config");
  });
});
