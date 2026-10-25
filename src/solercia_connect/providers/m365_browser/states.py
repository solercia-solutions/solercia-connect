"""Compatibility re-export: the browser state machine is vendor-neutral and lives in the core."""

from solercia_connect.providers.browser_core.states import BrowserState, BrowserStateMachine

__all__ = ["BrowserState", "BrowserStateMachine"]
