from __future__ import annotations

from importlib.metadata import entry_points

from ..errors import RouterError
from .base import ProviderAdapterV1

AdapterType = type[ProviderAdapterV1]


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AdapterType] = {}
        self._load_builtins()
        self._load_plugins()

    def _load_builtins(self) -> None:
        from .kis import KisAdapter
        from .namuh import NamuhAdapter
        from .simulated import SimulatedAdapter

        for adapter in (SimulatedAdapter, KisAdapter, NamuhAdapter):
            self.register(adapter)

    def _load_plugins(self) -> None:
        for item in entry_points(group="trade_router.providers"):
            try:
                loaded = item.load()
                if isinstance(loaded, type) and issubclass(loaded, ProviderAdapterV1):
                    self.register(loaded)
            except Exception:
                # One broken third-party plugin must not prevent the daemon from starting.
                continue

    def register(self, adapter: AdapterType) -> None:
        provider_id = getattr(adapter, "provider_id", "").strip().lower()
        if not provider_id:
            raise ValueError("provider adapter must define provider_id")
        self._adapters[provider_id] = adapter

    def get(self, provider_id: str) -> AdapterType:
        try:
            return self._adapters[provider_id.strip().lower()]
        except KeyError as exc:
            raise RouterError(
                "PROVIDER_NOT_FOUND",
                f"Provider '{provider_id}' is not installed.",
                status_code=404,
                details={"installed": sorted(self._adapters)},
            ) from exc

    def all(self) -> dict[str, AdapterType]:
        return dict(self._adapters)
