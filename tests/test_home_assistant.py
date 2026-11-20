"""Integration regressions against Home Assistant with a simulated BLE link."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant", reason="requires Home Assistant 2026.10 or newer")

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.nespresso_smart import (
    async_setup_entry,
    async_unload_entry,
    binary_sensor,
    config_flow,
    device,
    number,
    sensor,
)
from custom_components.nespresso_smart.const import CONF_PAIRING_SEED, DOMAIN
from custom_components.nespresso_smart.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.nespresso_smart.event import VertuoBrewEvent
from custom_components.nespresso_smart.protocol import MachineState, derive_secret

ADDRESS = "AA:BB:CC:DD:EE:FF"
SEED = "0123456789abcdef0123456789abcdef"
SERIAL = "12345CV1privateSerial"
NAME = f"CV1_{ADDRESS.replace(':', '')}"


def status_frame(state: MachineState, flags: int = 0) -> bytearray:
    return bytearray([0x40 | flags, state & 0x0F, state & 0xF0])


class Client:
    def __init__(self, on_disconnect, state: MachineState) -> None:
        self.is_connected = True
        self.on_disconnect = on_disconnect
        self.state = state
        self.notify = None
        self.push_during_settings = None
        self.operations = []

    async def pair(self) -> None:
        pass

    async def write_gatt_char(self, char, data, *, response) -> None:
        assert char == device.CHAR_CMID
        assert data == derive_secret(SEED)

    async def start_notify(self, char, handler) -> None:
        self.operations.append("subscribe")
        self.notify = handler

    async def read_gatt_char(self, char) -> bytearray:
        if char == device.CHAR_MACHINE_STATUS:
            self.operations.append("status")
            return status_frame(self.state)
        if char == device.CHAR_MACHINE_INFO:
            return bytearray.fromhex("0064006400c800640064aabbccddeeff")
        if char == device.CHAR_SERIAL_NUMBER:
            return bytearray(SERIAL.encode())
        assert char == device.CHAR_USER_SETTINGS
        if self.push_during_settings is not None:
            self.push(self.push_during_settings)
            self.push_during_settings = None
        return bytearray([42, 0, 3, 0])

    def push(self, state: MachineState, flags: int = 0) -> None:
        self.state = state
        self.notify(None, status_frame(state, flags))

    def drop(self) -> None:
        self.is_connected = False
        self.on_disconnect(self)

    async def disconnect(self) -> None:
        self.drop()


class Radio:
    def __init__(self, hass, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lookup = Mock(return_value=SimpleNamespace(address=ADDRESS, name=NAME))
        self.unsubscribe = Mock()
        self.register_callback = Mock(return_value=self.unsubscribe)
        self.connect = AsyncMock(side_effect=self.open)
        self.clients: list[Client] = []
        self.initial_state = MachineState.READY
        self.block_connect = False
        self.connect_started = asyncio.Event()
        monkeypatch.setattr(bluetooth, "async_ble_device_from_address", self.lookup)
        monkeypatch.setattr(
            bluetooth, "async_register_advertisement_callback", self.register_callback
        )
        monkeypatch.setattr(device, "establish_connection", self.connect)
        self.hass = hass

    async def open(
        self, _factory, ble_device, _name, *, disconnected_callback, timeout
    ) -> Client:
        assert ble_device is self.lookup.return_value
        if self.block_connect:
            self.connect_started.set()
            await asyncio.Future()
        client = Client(disconnected_callback, self.initial_state)
        self.clients.append(client)
        return client

    def advertise(self, *, connectable: bool = True) -> None:
        handler = self.register_callback.call_args.args[1]
        handler(SimpleNamespace(connectable=connectable))


async def setup(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    entry = ConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        title=NAME,
        data={CONF_ADDRESS: ADDRESS, CONF_PAIRING_SEED: SEED},
        options={},
        source="user",
        unique_id=ADDRESS,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        state=ConfigEntryState.SETUP_IN_PROGRESS,
    )
    radio = Radio(hass, monkeypatch)
    assert await async_get_config_entry_diagnostics(hass, entry) == {
        "entry_state": "setup_in_progress"
    }
    assert await async_setup_entry(hass, entry)
    return entry, entry.runtime_data, radio


def test_poll_survives_cache_expiry_and_orders_status(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        _, coordinator, radio = await setup(tmp_path, monkeypatch)
        try:
            client = radio.clients[0]
            assert client.operations == ["status", "subscribe", "status"]
            radio.lookup.reset_mock(return_value=True)
            radio.lookup.return_value = None
            client.push_during_settings = MachineState.BREWING
            await coordinator.async_refresh()
            assert coordinator.has_live_status
            assert coordinator.last_update_success
            assert coordinator.data.status.state is MachineState.BREWING
            assert coordinator.data.settings.water_hardness == 3
            assert coordinator.data.info.firmware_version == "2.0"
            radio.lookup.assert_not_called()
            radio.connect.assert_awaited_once()
        finally:
            await coordinator.async_shutdown()

    asyncio.run(scenario())


def test_wake_refresh_and_retired_callbacks(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        _, coordinator, radio = await setup(tmp_path, monkeypatch)
        try:
            assert coordinator.update_interval == timedelta(seconds=60)
            assert radio.register_callback.call_args.args[2] == ADDRESS
            first = radio.clients[0]
            first.drop()
            assert not coordinator.has_live_status
            radio.advertise(connectable=False)
            assert coordinator._wake_task is None
            for _ in range(5):
                radio.advertise()
            await coordinator._wake_task
            assert radio.connect.await_count == 2
            assert coordinator.has_live_status
            first.notify(None, status_frame(MachineState.BREWING))
            first.on_disconnect(first)
            assert coordinator.has_live_status
            assert coordinator.data.status.state is MachineState.READY
        finally:
            await coordinator.async_shutdown()

    asyncio.run(scenario())


def test_failed_wake_backs_off_and_poll_still_recovers(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        _, coordinator, radio = await setup(tmp_path, monkeypatch)
        try:
            radio.clients[0].drop()
            radio.connect.side_effect = RuntimeError("adapter is busy")
            radio.advertise()
            await coordinator._wake_task
            assert not coordinator.has_live_status
            assert 9 < coordinator._next_wake_retry - radio.hass.loop.time() <= 10
            radio.connect.side_effect = radio.open
            radio.advertise()
            assert radio.connect.await_count == 2
            await coordinator._handle_refresh_interval()
            assert coordinator.has_live_status
            assert radio.connect.await_count == 3
        finally:
            await coordinator.async_shutdown()

    asyncio.run(scenario())


def test_unload_cancels_in_flight_reconnect(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        entry, coordinator, radio = await setup(tmp_path, monkeypatch)
        radio.clients[0].drop()
        radio.block_connect = True
        radio.advertise()
        await asyncio.wait_for(radio.connect_started.wait(), 5)
        wake_task = coordinator._wake_task
        radio.advertise()
        assert await asyncio.wait_for(async_unload_entry(radio.hass, entry), 5)
        assert wake_task.cancelled()
        radio.unsubscribe.assert_called_once()
        radio.advertise()
        assert radio.connect.await_count == 2
        assert not coordinator.has_live_status

    asyncio.run(scenario())


def test_diagnostics_allowlist_and_history_limit(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        entry, coordinator, radio = await setup(tmp_path, monkeypatch)
        try:
            client = radio.clients[0]
            for _ in range(20):
                client.push(MachineState.BREWING)
                client.push(MachineState.READY)
            client.push(MachineState.READY)
            client.drop()
            radio.connect.side_effect = RuntimeError(f"failure for {NAME} with {SEED}")
            await coordinator.async_refresh()
            report = await async_get_config_entry_diagnostics(radio.hass, entry)
            serialized = json.dumps(report).lower()
            for private in (ADDRESS, SEED, SERIAL, NAME, derive_secret(SEED).hex()):
                assert private.lower() not in serialized
            assert report["model"] == "Vertuo Next"
            assert report["firmware"]["machine"] == "2.0"
            assert report["last_refresh_failure"]["stage"] == "connect"
            assert report["last_disconnect_at"] is not None
            states = [item["status"]["state"] for item in report["status_history"]]
            assert states == ["brewing", "ready"] * 15
        finally:
            await coordinator.async_shutdown()

    asyncio.run(scenario())


def test_brew_event_uses_one_continuous_link(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        entry, coordinator, radio = await setup(tmp_path, monkeypatch)
        try:
            for platform in (binary_sensor, number, sensor):
                add_entities = Mock()
                await platform.async_setup_entry(radio.hass, entry, add_entities)
                add_entities.assert_called_once()

            event = VertuoBrewEvent(coordinator)
            event.async_write_ha_state = Mock()
            trigger_event = Mock(wraps=event._trigger_event)
            event._trigger_event = trigger_event
            await event.async_added_to_hass()
            client = radio.clients[0]

            client.push(MachineState.CAPSULE_READING)
            client.push(MachineState.READY)
            client.push(MachineState.BREWING)
            client.push(MachineState.COOLDOWN, flags=0x10)
            client.push(MachineState.READY)
            client.push(MachineState.BREWING)
            client.push(MachineState.COOLDOWN, flags=0x01)
            client.push(MachineState.READY)
            trigger_event.assert_not_called()

            client.push(MachineState.BREWING)
            client.push(MachineState.COOLDOWN)
            client.push(MachineState.READY_OLD_CAPSULE)
            client.push(MachineState.READY)
            trigger_event.assert_called_once_with("brew_ended")
            assert event.state_attributes["event_type"] == "brew_ended"

            client.push(MachineState.BREWING)
            client.drop()
            assert not event.available
            radio.initial_state = MachineState.BREWING
            radio.advertise()
            await coordinator._wake_task
            assert event.available
            radio.clients[-1].push(MachineState.READY)
            trigger_event.assert_called_once()
            radio.clients[-1].push(MachineState.BREWING)
            radio.clients[-1].push(MachineState.READY)
            assert trigger_event.call_count == 2
        finally:
            await coordinator.async_shutdown()

    asyncio.run(scenario())


def test_blank_pairing_retry_reuses_the_generated_key(monkeypatch) -> None:
    async def scenario() -> None:
        flow = config_flow.VertuoConfigFlow()
        flow.flow_id = "pairing-retry"
        flow.handler = DOMAIN
        flow.context = {}
        flow._address = ADDRESS
        flow._try_onboard = AsyncMock(side_effect=["timeout", None])
        generate_seed = Mock(return_value=SEED)
        monkeypatch.setattr(config_flow, "generate_pairing_seed", generate_seed)

        failed = await flow.async_step_pair({})
        assert failed["errors"] == {"base": "timeout"}
        paired = await flow.async_step_pair({})
        assert paired["type"] is FlowResultType.CREATE_ENTRY
        assert paired["data"][CONF_PAIRING_SEED] == SEED
        generate_seed.assert_called_once()
        assert [call.args for call in flow._try_onboard.await_args_list] == [
            (SEED,),
            (SEED,),
        ]

    asyncio.run(scenario())
