"""Brew cycle events for Nespresso Smart."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VertuoConfigEntry
from .coordinator import VertuoCoordinator
from .entity import VertuoEntity
from .protocol import MachineState, MachineStatus

_BREW_ENDED = "brew_ended"
_READY_STATES = frozenset({MachineState.READY, MachineState.READY_OLD_CAPSULE})
_FAULT_STATES = frozenset({MachineState.DEVICE_ERROR, MachineState.TANK_EMPTY})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VertuoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the brew cycle event entity."""
    async_add_entities([VertuoBrewEvent(entry.runtime_data)])


class VertuoBrewEvent(VertuoEntity, EventEntity):
    """Report the end of a brew observed on one continuous connection."""

    _attr_translation_key = "brew_cycle"

    def __init__(self, coordinator: VertuoCoordinator) -> None:
        super().__init__(coordinator, "brew_cycle")
        self._attr_event_types = [_BREW_ENDED]
        self._previous_state: MachineState | None = None
        self._brewing = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.available:
            self._previous_state = self.coordinator.data.status.state

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.available:
            self._observe_status(self.coordinator.data.status)
        else:
            self._previous_state = None
            self._brewing = False
        super()._handle_coordinator_update()

    @callback
    def _observe_status(self, status: MachineStatus) -> None:
        state = status.state
        if status.error_present or status.water_tank_empty or state in _FAULT_STATES:
            self._brewing = False
        elif state is MachineState.BREWING:
            if self._previous_state not in (None, MachineState.BREWING):
                self._brewing = True
        elif state is not MachineState.COOLDOWN:
            if self._brewing and state in _READY_STATES:
                self._trigger_event(_BREW_ENDED)
            self._brewing = False
        self._previous_state = state
