"""Update coordinator for Nespresso Smart machines."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import utcnow

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .device import VertuoData, VertuoDevice, VertuoError
from .protocol import MachineStatus, model_from_name, model_from_serial

_LOGGER = logging.getLogger(__name__)
_WAKE_RETRY_DELAY = 10.0
_STATUS_HISTORY_SIZE = 30


class VertuoCoordinator(DataUpdateCoordinator[VertuoData]):
    """Keeps one machine's state fresh.

    The machine pushes MachineStatus notifications while connected, so state
    changes (brewing, tank empty) show up immediately. A Bluetooth advertisement
    triggers recovery after standby, with the periodic poll as a fallback.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
        seed: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{address}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            config_entry=entry,
        )
        self.address = address
        self._seed = seed
        self._device: VertuoDevice | None = None
        self._has_live_status = False
        self._stopping = False
        self._refresh_task: asyncio.Task | None = None
        self._wake_task: asyncio.Task | None = None
        self._cancel_advertisements: Callable[[], None] | None = None
        self._next_wake_retry = 0.0
        self._status_history: deque[dict[str, Any]] = deque(maxlen=_STATUS_HISTORY_SIZE)
        self._last_status_at: str | None = None
        self._last_disconnect_at: str | None = None
        self._last_refresh_started_at: str | None = None
        self._last_refresh_finished_at: str | None = None
        self._last_failure: dict[str, str] | None = None

    def _get_ble_device(self) -> BLEDevice:
        """Ask HA's Bluetooth stack for the current BLEDevice."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(
                f"Nespresso Smart {self.address} is not in range of any Bluetooth "
                "adapter or proxy"
            )
        return ble_device

    @callback
    def async_listen_for_wake(self) -> None:
        """Refresh when Home Assistant next sees the disconnected machine."""
        self._cancel_advertisements = bluetooth.async_register_advertisement_callback(
            self.hass, self._handle_advertisement, self.address
        )

    @callback
    def _handle_advertisement(self, info: bluetooth.BluetoothServiceInfoBleak) -> None:
        if (
            self._stopping
            or not info.connectable
            or (self._device is not None and self._device.is_connected)
            or self._refresh_task is not None
            or (self._wake_task is not None and not self._wake_task.done())
            or self.hass.loop.time() < self._next_wake_retry
        ):
            return
        # Let the Bluetooth manager finish updating its cache before lookup.
        self._wake_task = self.hass.async_create_task(
            self.async_refresh(), f"{DOMAIN} wake refresh", eager_start=False
        )

    @property
    def has_live_status(self) -> bool:
        """Return whether the current link has produced a status snapshot."""
        return self._has_live_status

    @callback
    def _handle_disconnect(self) -> None:
        self._last_disconnect_at = utcnow().isoformat()
        was_live = self._has_live_status
        self._has_live_status = False
        if was_live and not self._stopping:
            self.async_update_listeners()

    @callback
    def _handle_status_push(self, status: MachineStatus) -> None:
        """Publish a notification-driven status without a full re-read."""
        if self._stopping or not self._has_live_status or self.data is None:
            return
        self._record_status(status)
        self.async_set_updated_data(replace(self.data, status=status))

    @property
    def device(self) -> VertuoDevice:
        """The BLE device, created on first use and reused thereafter."""
        if self._device is None:
            self._device = VertuoDevice(
                self._get_ble_device(),
                self._seed,
                disconnected_callback=self._handle_disconnect,
                refresh_ble_device=self._get_ble_device,
            )
            self._device.set_status_callback(self._handle_status_push)
        return self._device

    async def _async_update_data(self) -> VertuoData:
        # Home Assistant serializes its scheduled and requested refreshes.
        self._refresh_task = asyncio.current_task()
        self._last_refresh_started_at = utcnow().isoformat()
        try:
            data = await self.device.update(subscribe=True)
        except Exception as err:
            self._last_failure = {
                "stage": self._device.update_stage if self._device else "discovery",
                "at": utcnow().isoformat(),
            }
            await self._drop_link()
            self._next_wake_retry = self.hass.loop.time() + _WAKE_RETRY_DELAY
            if isinstance(err, UpdateFailed):
                raise
            if isinstance(err, VertuoError):
                raise UpdateFailed(str(err)) from err
            raise UpdateFailed(f"Error communicating with the machine: {err}") from err
        finally:
            self._last_refresh_finished_at = utcnow().isoformat()
            self._refresh_task = None

        self._next_wake_retry = 0.0
        self._record_status(data.status)
        self._has_live_status = True
        return data

    def _record_status(self, status: MachineStatus) -> None:
        observed = {
            "state": status.state.name.lower(),
            "bootloader_active": status.bootloader_active,
            "error_present": status.error_present,
            "led_signaling_active": status.led_signaling_active,
            "descaling_needed": status.descaling_needed,
            "cleaning_needed": status.cleaning_needed,
            "water_tank_empty": status.water_tank_empty,
            "brewing_unit_closed": status.brewing_unit_closed,
            "capsule_container_full": status.capsule_container_full,
            "manual_cup_programming": status.manual_cup_programming,
            "milk_frother_running": status.milk_frother_running,
        }
        self._last_status_at = utcnow().isoformat()
        if not self._has_live_status or self._status_history[-1]["status"] != observed:
            self._status_history.append(
                {"at": self._last_status_at, "status": observed}
            )

    @callback
    def async_diagnostics(self) -> dict[str, Any]:
        """Build a shareable snapshot from explicitly selected values."""
        data = self.data
        info = data.info if data is not None else None
        name = self._device.name if self._device is not None else None
        return {
            "model": model_from_name(name)
            or model_from_serial(data.serial if data is not None else None),
            "firmware": {
                "machine": info.firmware_version,
                "connectivity": info.connectivity_firmware_version,
                "recipe_database": info.recipe_database_version,
            }
            if info is not None
            else None,
            "connected": self._device is not None and self._device.is_connected,
            "has_live_status": self._has_live_status,
            "last_status_at": self._last_status_at,
            "last_disconnect_at": self._last_disconnect_at,
            "last_refresh_started_at": self._last_refresh_started_at,
            "last_refresh_finished_at": self._last_refresh_finished_at,
            "last_refresh_failure": self._last_failure,
            "status_history": list(self._status_history),
        }

    async def _drop_link(self) -> None:
        """Release the machine's only connection slot after failure or unload."""
        if self._device is None:
            return
        try:
            await self._device.disconnect()
        except Exception as err:
            _LOGGER.debug("Error closing the Bluetooth link: %s", err)

    async def async_shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._cancel_advertisements is not None:
            self._cancel_advertisements()
            self._cancel_advertisements = None
        await super().async_shutdown()
        tasks = {self._wake_task, self._refresh_task} - {None, asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._drop_link()
