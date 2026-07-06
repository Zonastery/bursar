/**
 * Vitest global setup — starts a disposable `postgres:16` testcontainer when
 * `DATABASE_URL` isn't already set, so `npm test` exercises the real
 * PostgresStore integration/concurrency suite by default (Docker permitting)
 * instead of silently skipping it. CI sets `DATABASE_URL` to its own service
 * container, so this is a no-op there — same fast path as before.
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

declare module "vitest" {
  export interface ProvidedContext {
    DATABASE_URL: string | undefined;
  }
}

let container: StartedPostgreSqlContainer | undefined;

export async function setup(project: TestProject): Promise<void> {
  if (process.env.DATABASE_URL) {
    project.provide("DATABASE_URL", process.env.DATABASE_URL);
    return;
  }

  const { PostgreSqlContainer } = await import("@testcontainers/postgresql");
  try {
    container = await new PostgreSqlContainer("postgres:16").start();
    project.provide("DATABASE_URL", container.getConnectionUri());
  } catch (err) {
    // Covers both "container never started" and "something failed after it
    // did" (e.g. getConnectionUri()/provide() throwing) — stop it explicitly
    // here rather than relying on Vitest's teardown() running, since that
    // guarantee is unclear when setup() itself throws.
    if (container) {
      await container.stop().catch(() => {});
    }
    console.warn(
      `[global-setup] testcontainers could not start postgres:16 (${String(err)}); ` +
        "DB integration tests will skip. Set DATABASE_URL to point at an already-running " +
        "Postgres instead.",
    );
    project.provide("DATABASE_URL", undefined);
  }
}

export async function teardown(): Promise<void> {
  if (container) await container.stop();
}
