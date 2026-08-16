import { AUTO_RECHARGE_STATES } from "../src/browser.js";

declare global {
  var __bursarBrowserEntrySmoke: typeof AUTO_RECHARGE_STATES;
}

// Force the bundler to retain and resolve the browser entry's runtime surface.
globalThis.__bursarBrowserEntrySmoke = AUTO_RECHARGE_STATES;
