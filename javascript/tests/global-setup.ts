/**
 * Vitest global setup — builds and starts Bursar's provider-neutral
 * PostgreSQL 17 image with pg_partman 5 and pg_jsonschema 0.3
 * testcontainer when `DATABASE_URL` isn't already set, so `bun run test`
 * exercises the real PostgresStore integration/concurrency suite by default
 * (Docker permitting) instead of silently skipping it. CI requires this path
 * to succeed unless the job supplies an existing database explicitly.
 *
 * The connection string is handed to test files via Vitest's `provide`/
 * `inject` context (not `process.env`): globalSetup runs in the main
 * process, but the fork/thread pool that actually executes test files can be
 * warmed up before globalSetup resolves, so a `process.env` mutation here is
 * not reliably visible in the worker. `provide`/`inject` is the mechanism
 * Vitest documents for exactly this — passing runtime values computed in
 * globalSetup down to test files.
 */
import type { TestProject } from "vitest/node";
import type { StartedPostgreSqlContainer } from "@testcontainers/postgresql";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

declare module "vitest" {
  export interface ProvidedContext {
    DATABASE_URL: string | undefined;
  }
}

let container: StartedPostgreSqlContainer | undefined;
const DEFAULT_POSTGRES_IMAGE = "bursar/postgres-test:17.10-pg-jsonschema-0.3.4";
const POSTGRES_BUILD_CONTEXT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../tests/postgres",
);

function postgresTestsAreRequired(): boolean {
  return process.env.BURSAR_REQUIRE_POSTGRES_TESTS === "1";
}

function assertExternalDatabaseResetAllowed(): void {
  if (process.env.BURSAR_ALLOW_DATABASE_RESET !== "1") {
    throw new Error(
      "Refusing to reset externally supplied DATABASE_URL. Set " +
        "BURSAR_ALLOW_DATABASE_RESET=1 only for a disposable test database.",
    );
  }
}

async function resolvePostgresImage(): Promise<string> {
  const configuredImage = process.env.BURSAR_TEST_PG_IMAGE;
  if (configuredImage) return configuredImage;

  const { GenericContainer } = await import("testcontainers");
  await GenericContainer.fromDockerfile(POSTGRES_BUILD_CONTEXT).build(DEFAULT_POSTGRES_IMAGE, {
    deleteOnExit: false,
  });
  return DEFAULT_POSTGRES_IMAGE;
}

export async function setup(project: TestProject): Promise<void> {
  const databaseUrl = process.env.DATABASE_URL?.trim();
  if (databaseUrl) {
    assertExternalDatabaseResetAllowed();
    project.provide("DATABASE_URL", databaseUrl);
    return;
  }

  const { PostgreSqlContainer } = await import("@testcontainers/postgresql");
  let image = process.env.BURSAR_TEST_PG_IMAGE ?? DEFAULT_POSTGRES_IMAGE;
  try {
    image = await resolvePostgresImage();
    container = await new PostgreSqlContainer(image).start();
    project.provide("DATABASE_URL", container.getConnectionUri());
  } catch (err) {
    // Covers both "container never started" and "something failed after it
    // did" (e.g. getConnectionUri()/provide() throwing) — stop it explicitly
    // here rather than relying on Vitest's teardown() running, since that
    // guarantee is unclear when setup() itself throws.
    if (container) {
      await container.stop().catch(() => {});
      container = undefined;
    }
    const message =
      `[global-setup] testcontainers could not start ${image} (${String(err)}). ` +
      "Set DATABASE_URL to point at an already-running Postgres instead.";
    if (postgresTestsAreRequired()) {
      throw new Error(`PostgreSQL integration tests are required. ${message}`, { cause: err });
    }
    console.warn(`${message} DB integration tests will skip.`);
    project.provide("DATABASE_URL", undefined);
  }
}

export async function teardown(): Promise<void> {
  if (container) await container.stop();
}
