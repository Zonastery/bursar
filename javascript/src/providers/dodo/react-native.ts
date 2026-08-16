import { DodoCheckout } from "@dodopayments/react-native-checkout";

import {
  createDodoReactNativeCheckoutCore,
  type DodoReactNativeCheckoutAdapter,
  type DodoReactNativeCheckoutOptions as DodoReactNativeCheckoutCoreOptions,
} from "./react-native-core.js";

export type * from "./react-native-core.js";
export interface DodoReactNativeCheckoutOptions extends Omit<
  DodoReactNativeCheckoutCoreOptions,
  "checkout"
> {
  /** Override the official SDK surface for custom platform hosts or tests. */
  checkout?: DodoReactNativeCheckoutAdapter;
}

/**
 * Compose a Bursar backend checkout intent with Dodo's official React Native
 * checkout SDK. This client never receives a Dodo API key.
 */
export function createDodoReactNativeCheckout(options: DodoReactNativeCheckoutOptions) {
  return createDodoReactNativeCheckoutCore({
    ...options,
    checkout: options.checkout ?? DodoCheckout,
  });
}
