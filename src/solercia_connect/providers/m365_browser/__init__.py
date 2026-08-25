from solercia_connect.providers.m365_browser.adapter import (
    Microsoft365BrowserAdapter,
    browser_extraction_report,
    browser_surface_for_capability,
)
from solercia_connect.providers.m365_browser.driver import (
    BrowserDriver,
    BrowserSnapshot,
    PlaywrightBrowserDriver,
)

__all__ = [
    "BrowserDriver",
    "BrowserSnapshot",
    "Microsoft365BrowserAdapter",
    "PlaywrightBrowserDriver",
    "browser_extraction_report",
    "browser_surface_for_capability",
]
