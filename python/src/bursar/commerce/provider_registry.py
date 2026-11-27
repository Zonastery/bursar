from __future__ import annotations

import asyncio
import inspect

from bursar.commerce.errors import ProviderSelectionError
from bursar.commerce.types import CommerceOptions, CommerceProviderFactoryContext
from bursar.config.types import CommerceOffer, CustomObjectReference, DodoProductReference, StripePriceReference
from bursar.providers.types import PaymentProvider


class CommerceProviderRegistry:
    """Lazily constructs and selects application-registered providers."""

    def __init__(
        self,
        options: CommerceOptions,
        context: CommerceProviderFactoryContext,
    ) -> None:
        self._options = options
        self._context = context
        self._instances: dict[str, PaymentProvider] = {}
        self._loading: dict[str, asyncio.Task[PaymentProvider]] = {}
        self._generation = 0
        if options.default_provider is not None and options.default_provider not in options.providers:
            raise ProviderSelectionError(f"Default payment provider {options.default_provider!r} is not registered")

    @property
    def configured_providers(self) -> list[str]:
        return list(self._options.providers)

    async def get(self, name: str) -> PaymentProvider:
        if name in self._instances:
            return self._instances[name]
        factory = self._options.providers.get(name)
        if factory is None:
            raise ProviderSelectionError(f"Payment provider {name!r} is not registered")
        loading = self._loading.get(name)
        if loading is None:
            generation = self._generation

            async def load() -> PaymentProvider:
                value = factory(self._context)
                provider = await value if inspect.isawaitable(value) else value
                if not isinstance(provider, PaymentProvider):
                    raise ProviderSelectionError(f"Provider factory {name!r} did not return a valid payment provider")
                if provider.provider != name:
                    raise ProviderSelectionError(f"Provider factory {name!r} returned {provider.provider!r}")
                if generation == self._generation:
                    self._instances[name] = provider
                return provider

            loading = asyncio.create_task(load())
            self._loading[name] = loading

            def discard_completed(completed: asyncio.Task[PaymentProvider]) -> None:
                if self._loading.get(name) is completed:
                    self._loading.pop(name, None)
                if not completed.cancelled():
                    completed.exception()

            loading.add_done_callback(discard_completed)
        # One request being cancelled must not cancel the shared factory task
        # or force concurrent callers to construct duplicate provider clients.
        return await asyncio.shield(loading)

    async def select(
        self,
        *,
        requested: str | None = None,
        current: str | None = None,
        offer: CommerceOffer | None = None,
    ) -> PaymentProvider:
        def available(name: str) -> bool:
            if offer is None:
                return True
            reference = offer.providers.get(name)
            if isinstance(reference, StripePriceReference):
                value = reference.price_id
            elif isinstance(reference, DodoProductReference):
                value = reference.product_id
            elif isinstance(reference, CustomObjectReference):
                value = reference.external_id
            else:
                return False
            return isinstance(value, str) or self._context.provider_environment in value

        compatible = (
            [name for name in offer.providers if name in self._options.providers and available(name)]
            if offer is not None
            else list(self._options.providers)
        )

        for selected in (requested, current):
            if selected is None:
                continue
            if selected not in self._options.providers:
                raise ProviderSelectionError(f"Payment provider {selected!r} is not registered")
            if not available(selected):
                raise ProviderSelectionError(f"Offer is unavailable from payment provider {selected!r}")
            return await self.get(selected)

        if len(compatible) == 1:
            return await self.get(compatible[0])

        default = self._options.default_provider
        if default is not None and default in compatible:
            return await self.get(default)

        if not compatible:
            raise ProviderSelectionError("No compatible payment provider is registered")
        raise ProviderSelectionError(f"Payment provider selection is ambiguous: {', '.join(sorted(compatible))}")

    def clear(self) -> None:
        self._generation += 1
        self._instances.clear()
        self._loading.clear()
