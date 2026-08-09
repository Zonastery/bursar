import { AUTO_RECHARGE_STATES } from "../src/browser.js";

// Force the bundler to retain and resolve the browser entry's runtime surface.
(globalThis as Record<string, unknown>).__bursarBrowserEntrySmoke = AUTO_RECHARGE_STATES;
