"""Downloadable diagnostics for Nespresso Smart."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import VertuoConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VertuoConfigEntry
) -> dict[str, Any]:
    """Return status and connection information safe to attach to an issue."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return {"entry_state": entry.state.value}
    return coordinator.async_diagnostics()
