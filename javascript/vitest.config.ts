import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts", "../tests/parity/run_parity.ts"],
    globalSetup: ["tests/global-setup.ts"],
    testTimeout: 30000,
    // store-integration.test.ts and security-rls.test.ts each open their own
    // pg.Pool against the SAME real Postgres and assume exclusive control of
    // bursar's tables (DELETE/TRUNCATE between tests). Running test files in
    // parallel races those two files against each other on the shared DB
    // (cross-file FK violations / rows vanishing mid-assertion). Disable
    // cross-file parallelism rather than rearchitect DB isolation for a
    // suite that runs in a few seconds either way.
    fileParallelism: false,
    coverage: {
      provider: "v8",
      all: true,
      include: ["src/**/*.ts"],
      // Pure re-export barrels and type-only files (no runtime logic to test).
      exclude: ["src/index.ts", "src/node.ts", "src/types.ts", "src/metrics.ts"],
      reporter: ["text", "json", "html"],
      // Measured baseline (no DB) was ~92% for src/ and ~68% for src/stores/
      // (postgres-store.ts / supabase-store.ts need a real DB to exercise).
      // The CI job runs with a real Postgres, so its effective coverage is
      // higher. Ratchet these up as coverage improves — never lower without a
      // documented reason.
      thresholds: {
        statements: 77,
        branches: 80,
        functions: 78,
        lines: 77,
      },
    },
  },
});
