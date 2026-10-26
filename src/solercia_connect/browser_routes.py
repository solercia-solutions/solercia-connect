"""Which targets have a supervised-browser route, decided in one place.

The route policy may allow a family, but the route exists only once that family's browser adapter
does. Every surface that offers or refuses the browser route asks here, so adding a family's
adapter opens its route everywhere at once, and nowhere before.
"""

from __future__ import annotations

from solercia_connect.config import ConnectionType, target_spec
from solercia_connect.domain import ProviderFamily, official_provider_family
from solercia_connect.ports import ProviderDescriptor
from solercia_connect.providers.m365_browser import Microsoft365BrowserAdapter

# The browser adapter of each family that has one. Google and Jira join when theirs exist.
_BROWSER_ADAPTERS: dict[ProviderFamily, ProviderDescriptor] = {
    ProviderFamily.MICROSOFT: Microsoft365BrowserAdapter.descriptor,
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
    return target not in OFFICIAL_ONLY_TARGETS and target_family(target) in BROWSER_ROUTE_FAMILIES
