"""Which targets have a supervised-browser route, decided in one place.

The route policy may allow a family, but the route exists only once that family's browser adapter
does, and only for the targets whose capabilities that adapter serves. Every surface that offers
or refuses the browser route asks here, so adding an adapter opens its route everywhere at once,
and nowhere before.
"""

from __future__ import annotations

from solercia_connect.config import ConnectionType, target_spec
from solercia_connect.domain import ProviderFamily, official_provider_family
from solercia_connect.ports import ProviderDescriptor
from solercia_connect.providers.google_browser import GoogleBrowserAdapter
from solercia_connect.providers.m365_browser import Microsoft365BrowserAdapter

# The browser adapter of each family that has one. Google's serves Meet only; Jira joins when
# its adapter exists.
_BROWSER_ADAPTERS: dict[ProviderFamily, ProviderDescriptor] = {
    ProviderFamily.MICROSOFT: Microsoft365BrowserAdapter.descriptor,
    ProviderFamily.GOOGLE: GoogleBrowserAdapter.descriptor,
}

BROWSER_ROUTE_FAMILIES: frozenset[ProviderFamily] = frozenset(_BROWSER_ADAPTERS)
# Targets that never take the browser route, whatever their family offers: the Gmail writes run
# over the official API only (docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md).
OFFICIAL_ONLY_TARGETS: frozenset[ConnectionType] = frozenset({ConnectionType.GMAIL_COMPOSE})
BROWSER_PROVIDER_IDS: frozenset[str] = frozenset(
    descriptor.provider_id for descriptor in _BROWSER_ADAPTERS.values()
)


def target_family(target: ConnectionType) -> ProviderFamily:
    """Return the family a target belongs to, read off its official provider."""
    family = official_provider_family(target_spec(target).provider_id)
    if family is None:
        raise ValueError(f"Target has no declared provider family: {target.value}")
    return family


def has_browser_route(target: ConnectionType) -> bool:
    """A target has the route when its family's adapter serves at least one of its capabilities.

    The family alone is not enough: Google's adapter serves Meet, so Gmail and Calendar keep no
    browser route until an adapter serves their capabilities too.
    """
    if target in OFFICIAL_ONLY_TARGETS:
        return False
    descriptor = _BROWSER_ADAPTERS.get(target_family(target))
    if descriptor is None:
        return False
    served = {capability.value for capability in descriptor.capabilities}
    return not served.isdisjoint(target_spec(target).capabilities)
