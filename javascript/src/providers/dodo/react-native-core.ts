import type {
  AbandonedSession,
  CheckoutEvent,
  CheckoutResult,
} from "@dodopayments/react-native-checkout";

export type DodoReactNativeConfirmationStatus = "pending" | "succeeded" | "failed" | "expired";

const TERMINAL_CONFIRMATION_STATUSES = new Set<DodoReactNativeConfirmationStatus>([
  "succeeded",
  "failed",
  "expired",
]);

export type DodoReactNativeCheckoutEvent = CheckoutEvent;
export type DodoReactNativeCheckoutResult = CheckoutResult;
export type DodoReactNativeAbandonedSession = AbandonedSession;
export type DodoReactNativeCheckoutAdapter = Pick<
  typeof import("@dodopayments/react-native-checkout").DodoCheckout,
  "start" | "getAbandonedSession" | "clearAbandonedSession" | "handleOpenURL"
>;

export interface DodoReactNativeCheckoutSession {
  /** Bursar checkout intent returned by the application's backend. */
  intentId: string;
  /** Dodo hosted checkout URL returned by the application's backend. */
  url: string;
}

export interface DodoReactNativePendingCheckout {
  intentId: string;
  startedAt: string;
}

/** App-owned persistence, typically SecureStore or AsyncStorage. */
export interface DodoReactNativeCheckoutStore {
  getPendingCheckout(): Promise<DodoReactNativePendingCheckout | null>;
  setPendingCheckout(checkout: DodoReactNativePendingCheckout): Promise<void>;
  clearPendingCheckout(): Promise<void>;
}

export interface DodoReactNativeCheckoutOptions {
  /** Dedicated callback URL registered through Dodo's Expo/native plugin. */
  returnUrl: string;
  store: DodoReactNativeCheckoutStore;
  /** Official SDK surface supplied by the platform adapter. */
  checkout: DodoReactNativeCheckoutAdapter;
  /** Resolve the authoritative status from Bursar on the backend. */
  getCheckoutStatus(intentId: string): Promise<DodoReactNativeConfirmationStatus>;
  onEvent?: (event: DodoReactNativeCheckoutEvent) => void;
}

export interface DodoReactNativeReconciliation {
  pending: DodoReactNativePendingCheckout | null;
  abandoned: DodoReactNativeAbandonedSession | null;
  confirmationStatus: DodoReactNativeConfirmationStatus | null;
}

export interface DodoReactNativeCheckoutClient {
  /**
   * Opens Dodo's official native checkout. The returned result is a UI hint;
   * call `reconcile` and grant access only from the backend confirmation.
   */
  start(session: DodoReactNativeCheckoutSession): Promise<{
    intentId: string;
    result: DodoReactNativeCheckoutResult;
  }>;
  /** Reconcile persisted/abandoned checkout state against Bursar. */
  reconcile(): Promise<DodoReactNativeReconciliation | null>;
  /** Forward React Native Linking URLs for the official iOS SDK. */
  handleOpenURL(url: string): Promise<boolean>;
  getAbandonedSession(): Promise<DodoReactNativeAbandonedSession | null>;
  clearRecoveryState(): Promise<void>;
}

/** Compose checkout behavior around an injected platform SDK adapter. */
export function createDodoReactNativeCheckoutCore(
  options: DodoReactNativeCheckoutOptions,
): DodoReactNativeCheckoutClient {
  const returnUrl = options.returnUrl.trim();
  if (!returnUrl) throw new TypeError("returnUrl must not be empty");
  const checkout = options.checkout;

  const clearRecoveryState = async (): Promise<void> => {
    await Promise.all([options.store.clearPendingCheckout(), checkout.clearAbandonedSession()]);
  };

  return {
    async start(session) {
      const intentId = session.intentId.trim();
      if (!intentId) throw new TypeError("intentId must not be empty");
      await options.store.setPendingCheckout({
        intentId,
        startedAt: new Date().toISOString(),
      });
      const checkoutOptions: Parameters<DodoReactNativeCheckoutAdapter["start"]>[0] = {
        checkoutUrl: session.url,
        returnUrl,
      };
      if (options.onEvent) checkoutOptions.onEvent = options.onEvent;
      const result = await checkout.start(checkoutOptions);
      return { intentId, result };
    },

    async reconcile() {
      const [pending, abandoned] = await Promise.all([
        options.store.getPendingCheckout(),
        checkout.getAbandonedSession(),
      ]);
      if (!pending) {
        return abandoned ? { pending: null, abandoned, confirmationStatus: null } : null;
      }

      const confirmationStatus = await options.getCheckoutStatus(pending.intentId);
      if (TERMINAL_CONFIRMATION_STATUSES.has(confirmationStatus)) {
        await clearRecoveryState();
      }
      return { pending, abandoned, confirmationStatus };
    },

    handleOpenURL(url) {
      return checkout.handleOpenURL(url);
    },

    getAbandonedSession() {
      return checkout.getAbandonedSession();
    },

    clearRecoveryState,
  };
}
