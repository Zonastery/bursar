import type { CommerceOffer } from "../config/types.js";
import type { PaymentProvider } from "../providers/types.js";
import { ProviderSelectionError } from "./errors.js";
import type { CommerceOptions, CommerceProviderFactoryContext } from "./types.js";

export class CommerceProviderRegistry {
  private readonly instances = new Map<string, Promise<PaymentProvider>>();

  constructor(
    private readonly options: CommerceOptions,
    private readonly context: CommerceProviderFactoryContext,
  ) {
    if (
      options.defaultProvider != null &&
      !Object.prototype.hasOwnProperty.call(options.providers, options.defaultProvider)
    ) {
      throw new ProviderSelectionError(
        `Default payment provider '${options.defaultProvider}' is not registered`,
      );
    }
  }

  get configuredProviders(): string[] {
    return Object.keys(this.options.providers);
  }

  clear(): void {
    this.instances.clear();
  }

  async get(providerName: string): Promise<PaymentProvider> {
    const factory = this.options.providers[providerName];
    if (!factory) {
      throw new ProviderSelectionError(`Payment provider '${providerName}' is not registered`);
    }
    let loading = this.instances.get(providerName);
    if (!loading) {
      loading = Promise.resolve(factory(this.context))
        .then((provider) => {
          if (provider.provider !== providerName) {
            throw new ProviderSelectionError(
              `Provider factory '${providerName}' returned provider '${provider.provider}'`,
            );
          }
          return provider;
        })
        .catch((error: unknown) => {
          this.instances.delete(providerName);
          throw error;
        });
      this.instances.set(providerName, loading);
    }
    return loading;
  }

  async select(input: {
    requested?: string | null;
    current?: string | null;
    offer?: CommerceOffer | null;
  }): Promise<PaymentProvider> {
    const compatible = input.offer
      ? Object.keys(input.offer.providers).filter((name) =>
          Object.prototype.hasOwnProperty.call(this.options.providers, name),
        )
      : this.configuredProviders;

    const assertCompatible = (name: string): string => {
      if (!this.options.providers[name]) {
        throw new ProviderSelectionError(`Payment provider '${name}' is not registered`);
      }
      if (input.offer && !input.offer.providers[name]) {
        throw new ProviderSelectionError(
          `Offer is not available through payment provider '${name}'`,
        );
      }
      return name;
    };

    if (input.requested) return this.get(assertCompatible(input.requested));
    if (input.current) return this.get(assertCompatible(input.current));
    if (this.options.defaultProvider && compatible.includes(this.options.defaultProvider)) {
      return this.get(this.options.defaultProvider);
    }
    if (compatible.length === 1) return this.get(compatible[0]!);
    if (compatible.length === 0) {
      throw new ProviderSelectionError("No compatible payment provider is registered");
    }
    throw new ProviderSelectionError(
      `Provider selection is ambiguous: ${compatible.sort().join(", ")}`,
    );
  }
}
