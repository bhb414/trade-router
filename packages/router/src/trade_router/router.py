from __future__ import annotations

from collections.abc import Iterable

from .errors import RouterError, RouterErrorCode
from .models import BrokerCapabilities, InstrumentId, Quote, Venue
from .providers import ProviderAdapterV1


class TradeRouter:
    """In-process broker router owned by the local Studio process."""

    def __init__(self, providers: Iterable[ProviderAdapterV1] = ()) -> None:
        self._providers = {provider.name: provider for provider in providers}

    def add_provider(self, provider: ProviderAdapterV1) -> None:
        if provider.name in self._providers:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                f"Provider '{provider.name}' is already registered.",
            )
        self._providers[provider.name] = provider

    async def capabilities(self) -> dict[str, BrokerCapabilities]:
        return {
            name: await provider.capabilities()
            for name, provider in self._providers.items()
        }

    async def quote(
        self,
        provider: str,
        instrument: InstrumentId,
        venue: Venue = Venue.INTEGRATED,
    ) -> Quote:
        adapter = self._providers.get(provider)
        if adapter is None:
            raise RouterError(
                RouterErrorCode.PROVIDER_NOT_FOUND,
                f"Provider '{provider}' is not registered.",
            )
        capabilities = await adapter.capabilities()
        if not capabilities.quotes or venue not in capabilities.venues:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                f"Provider '{provider}' does not support {venue.value} quotes.",
            )
        return await adapter.quote(instrument, venue)

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
