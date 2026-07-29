from __future__ import annotations

import inspect
from typing import Any

from bursar.commerce.errors import ProviderSelectionError
from bursar.commerce.types import CommerceOptions, CommerceProviderFactoryContext
from bursar.config.types import CommerceOffer
from bursar.providers.types import PaymentProvider


class CommerceProviderRegistry:
    """Lazily constructs and selects application-registered providers."""

    def __init__(self, options: CommerceOptions, event_sink: Any) -> None:
        self._options = options
        self._context = CommerceProviderFactoryContext(
            event_sink=event_sink,
            identity_resolver=options.identity_resolver,
        )
        self._instances: dict[str, PaymentProvider] = {}

    async def get(self, name: str) -> PaymentProvider:
        if name in self._instances:
            return self._instances[name]
        factory = self._options.providers.get(name)
        if factory is None:
            raise ProviderSelectionError(f"Payment provider {name!r} is not registered")
        value = factory(self._context)
        provider = await value if inspect.isawaitable(value) else value
        if provider.provider != name:
            raise ProviderSelectionError(f"Provider factory {name!r} returned {provider.provider!r}")
        self._instances[name] = provider
        return provider

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
        if default is not None:
            if default not in compatible:
                raise ProviderSelectionError(f"Default provider {default!r} is incompatible with this operation")
            return await self.get(default)

        if not compatible:
            raise ProviderSelectionError("No compatible payment provider is registered")
        raise ProviderSelectionError(f"Payment provider selection is ambiguous: {', '.join(sorted(compatible))}")

    def clear(self) -> None:
        self._instances.clear()
