from __future__ import annotations

import asyncio
import inspect

from bursar.commerce.errors import ProviderSelectionError
from bursar.commerce.types import CommerceOptions, CommerceProviderFactoryContext
from bursar.config.types import CommerceOffer
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
        try:
            return await loading
        finally:
            if self._loading.get(name) is loading:
                self._loading.pop(name, None)

    async def select(
        self,
        *,
        requested: str | None = None,
        current: str | None = None,
        offer: CommerceOffer | None = None,
    ) -> PaymentProvider:
        compatible = (
            [name for name in offer.providers if name in self._options.providers]
            if offer is not None
            else list(self._options.providers)
        )

        for selected in (requested, current):
            if selected is None:
                continue
            if selected not in self._options.providers:
                raise ProviderSelectionError(f"Payment provider {selected!r} is not registered")
            if offer is not None and selected not in offer.providers:
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
