"""Google's supervised-browser package: Meet transcripts through Calendar web and Docs."""

from solercia_connect.providers.google_browser.adapter import GoogleBrowserAdapter
from solercia_connect.providers.google_browser.driver import (
    GoogleBrowserDriver,
    PlaywrightGoogleBrowserDriver,
)

__all__ = ["GoogleBrowserAdapter", "GoogleBrowserDriver", "PlaywrightGoogleBrowserDriver"]
