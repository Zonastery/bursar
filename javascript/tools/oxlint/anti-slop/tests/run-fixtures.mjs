#!/usr/bin/env node

import { strict as assert } from "node:assert";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import { fixtures } from "./fixtures.mjs";

const pluginRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pluginPath = resolve(pluginRoot, "index.ts");
const oxlintPath = process.argv[2];

if (oxlintPath === undefined) {
  console.error("Usage: node tests/run-fixtures.mjs <path-to-oxlint>");
  process.exit(2);
}

const temporaryDirectory = mkdtempSync(resolve(tmpdir(), "anti-slop-fixtures-"));
let caseCount = 0;

function runOxlint(arguments_) {
  return new Promise((resolveResult, reject) => {
    const child = spawn(resolve(oxlintPath), arguments_, {
      cwd: pluginRoot,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      output += chunk;
    });
    child.stderr.on("data", (chunk) => {
      output += chunk;
    });
    child.on("error", reject);
    child.on("close", (status) => resolveResult({ output, status }));
  });
}

async function runFixture(rule, kind, index, source) {
  const fixturePath = resolve(temporaryDirectory, `${rule}-${kind}-${index}.ts`);
  const configPath = resolve(temporaryDirectory, `${rule}-${kind}-${index}.oxlintrc.json`);
  writeFileSync(fixturePath, source);
  writeFileSync(
    configPath,
    JSON.stringify({
      categories: { correctness: "off" },
      jsPlugins: [{ name: "anti-slop", specifier: pluginPath }],
      rules: { [`anti-slop/${rule}`]: "error" },
    }),
  );

  const { output, status } = await runOxlint(["--config", configPath, fixturePath]);

  if (kind === "valid") {
    assert.equal(status, 0, `Expected ${rule} valid case ${index} to pass:\n${output}`);
  } else {
    assert.notEqual(status, 0, `Expected ${rule} invalid case ${index} to fail`);
    assert.ok(
      output.includes(`anti-slop(${rule})`),
      `Expected ${rule} diagnostic for invalid case ${index}:\n${output}`,
    );
  }
  caseCount += 1;
}

try {
  const cases = [];
  for (const fixture of fixtures) {
    fixture.valid.forEach((source, index) =>
      cases.push({ rule: fixture.rule, kind: "valid", index, source }),
    );
    fixture.invalid.forEach((source, index) =>
      cases.push({ rule: fixture.rule, kind: "invalid", index, source }),
    );
  }

  let nextCase = 0;
  const workerCount = Math.min(8, cases.length);
  await Promise.all(
    Array.from({ length: workerCount }, async () => {
      while (nextCase < cases.length) {
        const fixtureCase = cases[nextCase];
        nextCase += 1;
        await runFixture(fixtureCase.rule, fixtureCase.kind, fixtureCase.index, fixtureCase.source);
      }
    }),
  );
} finally {
  rmSync(temporaryDirectory, { recursive: true, force: true });
}

console.log(`Passed ${caseCount} anti-slop rule fixtures across ${fixtures.length} rules.`);
