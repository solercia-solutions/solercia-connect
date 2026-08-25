from __future__ import annotations

from solercia_connect.domain import Capability, ProviderFamily, ReasonCode, RouteKind
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import ProviderAdapter, ProviderDescriptor


class ProviderRegistry:
    """Explicit registry with one official adapter per ID and one browser fallback per family."""

    def __init__(self) -> None:
        self._official: dict[str, ProviderAdapter] = {}
        self._browser: dict[ProviderFamily, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        descriptor = adapter.descriptor
        if descriptor.route is RouteKind.OFFICIAL:
            if descriptor.provider_id in self._official:
                raise ValueError(f"Duplicate official provider: {descriptor.provider_id}")
            self._official[descriptor.provider_id] = adapter
            return
        if descriptor.family in self._browser:
            raise ValueError(f"Duplicate browser provider for family: {descriptor.family.value}")
        self._browser[descriptor.family] = adapter

    def official(self, provider_id: str) -> ProviderAdapter:
        try:
            return self._official[provider_id]
        except KeyError as exc:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                f"Official provider is not configured: {provider_id}",
            ) from exc

    def browser(self, family: ProviderFamily) -> ProviderAdapter | None:
        return self._browser.get(family)

    def browser_for_capability(self, capability: Capability) -> ProviderAdapter | None:
        """Find a browser adapter by capability, for when no official adapter exists at all.

        An unconfigured official provider is the very situation the fallback exists for, so the
        family cannot be read off an official descriptor that was never built.
        """
        for adapter in self._browser.values():
            if capability in adapter.descriptor.capabilities:
                return adapter
        return None

    def find(self, provider_id: str) -> ProviderAdapter:
        if provider_id in self._official:
            return self._official[provider_id]
        for adapter in self._browser.values():
            if adapter.descriptor.provider_id == provider_id:
                return adapter
        raise ConnectorFailure(
            ReasonCode.CONFIGURATION_INVALID,
            f"Provider is not configured: {provider_id}",
        )

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        adapters = [*self._official.values(), *self._browser.values()]
        return tuple(sorted((adapter.descriptor for adapter in adapters), key=_descriptor_key))


def _descriptor_key(descriptor: ProviderDescriptor) -> tuple[str, str]:
    return descriptor.provider_id, descriptor.route.value
