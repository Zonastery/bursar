import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts", "../tests/parity/run_parity.ts"],
    globalSetup: ["tests/global-setup.ts"],
    testTimeout: 30000,
    // store-integration.test.ts and security-rls.test.ts each open their own
    // pg.Pool against the SAME real Postgres and assume exclusive control of
    // bursar's tables (DELETE/TRUNCATE between tests). Parallel file execution
    // races those files against each other on the shared DB.
    //
    // Use a single fork so files run sequentially in one process. Earlier
    // attempts avoided `singleFork` because leaked pools (unguarded `try`
    // blocks in billing-integration tests, and a race in PostgresStore.close()
    // where `close()` could fire before the lazy pool initialised) accumulated
    // connections across file boundaries. Those leaks are now fixed:
    //   - billing-integration wraps every local pool creation in `try/finally`
    //   - PostgresStore.close() now awaits `poolPromise` before checking `pool`
    //   - All integration files use `max: 1` or `max: 3` — never the pg default
    //   - `fileParallelism: false` + `maxWorkers: 1` is a single process
    fileParallelism: false,
    pool: "forks",
    maxWorkers: 1,
    // config-parity.test.ts imports a fixture from config.test.ts; keep each
    // test file's module graph isolated so the fixture file is still
    // collected as a test suite when Vitest loads it separately.
    isolate: true,
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      // Pure re-export barrels and type-only files (no runtime logic to test).
      // Provider adapters have a separate focused coverage gate because their
      // dependency-heavy branches distort the package-wide domain-code signal.
      exclude: [
        "src/index.ts",
        "src/node.ts",
        "src/metrics.ts",
        "src/providers",
        // Abstract capability surface; concrete stores are covered instead.
        "src/credits/store.ts",
      ],
      reporter: ["text", "json", "html"],
      // PostgreSQL repositories need a real DB to exercise. CI supplies one,
      // so these thresholds enforce the production integration-suite result.
      // Keep modest branch/function headroom for Node/V8 remapper differences
      // across the supported runtime matrix.
      thresholds: {
        statements: 90,
        branches: 77,
        functions: 94,
        lines: 90,
      },
    },
  },
});
