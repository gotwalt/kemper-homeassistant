"""Setting the entry up, tearing it down, and what that costs the device."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

from conftest import DEVICE_NAME, SERIAL, entity_id, make_entry, wait_until
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from libkp.session import PROTOCOL_CBOR_CONTROL, PROTOCOL_MIDI3_STREAM
from libkp.testing import FakeDevice
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.kemper.const import (
    CONF_ACTIVITY_THRESHOLD,
    CONF_ACTIVITY_WINDOW,
    CONF_SERIAL,
    CONF_SW_VERSION,
    DOMAIN,
)
from custom_components.kemper.coordinator import (
    RECONNECT_DELAYS,
    STALE_GRACE_SECONDS,
)
from custom_components.kemper.discovery import Found


async def test_setup_opens_exactly_one_session(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """One stream, and no control channel: the entities need nothing from it."""
    assert entry.state is ConfigEntryState.LOADED
    assert device.connection_count(PROTOCOL_MIDI3_STREAM) == 1
    assert device.connection_count(PROTOCOL_CBOR_CONTROL) == 0


async def test_setup_registers_the_device(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Discovery's name and version reach the device registry."""
    device_entry = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SERIAL)})
    assert device_entry is not None
    assert device_entry.name == DEVICE_NAME
    assert device_entry.manufacturer == "Kemper"
    assert device_entry.model == "Profiler"
    assert device_entry.sw_version == "1.2.3"


async def test_unload_hangs_up(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Unloading closes the socket; the fake sees the hangup."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    connection = device.connections[0]
    async with asyncio.timeout(5):
        await connection.closed.wait()


async def test_a_device_that_is_not_there_retries_later(
    hass: HomeAssistant, device: FakeDevice
) -> None:
    """A refused connection is ``ConfigEntryNotReady``, not a hard failure."""
    entry = make_entry(device)
    await device.stop()  # nothing is listening on that port any more
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_changing_options_does_not_reconnect(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """The detector is retuned in place: same model, same socket, new numbers."""
    coordinator = entry.runtime_data
    model = coordinator.model

    hass.config_entries.async_update_entry(
        entry, options={CONF_ACTIVITY_WINDOW: 1, CONF_ACTIVITY_THRESHOLD: 50}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.model is model
    assert device.connection_count(PROTOCOL_MIDI3_STREAM) == 1
    detector = coordinator.activity
    assert detector.window == 60.0
    assert detector.threshold == 8192


async def test_the_configured_port_is_the_one_dialed(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """The entry's port, not libkp's default, decides where the model dials."""
    assert entry.data[CONF_PORT] == device.port
    assert device.connections


async def test_setup_follows_the_serial_to_a_new_address(
    hass: HomeAssistant, device: FakeDevice
) -> None:
    """The lease moved: the entry knows the serial, so it finds the device again."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEVICE_NAME,
        unique_id=SERIAL,
        data={
            CONF_HOST: "10.0.0.99",  # where it used to be
            CONF_PORT: device.port,
            CONF_NAME: "Old Name",
            CONF_SERIAL: SERIAL,
            CONF_SW_VERSION: "1.0.0",
        },
    )
    entry.add_to_hass(hass)
    moved = Found(host="127.0.0.1", name="Studio Profiler", serial=SERIAL, version="10.5.3")

    with patch("custom_components.kemper.discovery.async_discover", return_value=[moved]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    try:
        assert entry.state is ConfigEntryState.LOADED
        # The entry followed the device, name and firmware version included.
        assert entry.data[CONF_HOST] == "127.0.0.1"
        assert entry.data[CONF_NAME] == "Studio Profiler"
        assert entry.data[CONF_SW_VERSION] == "10.5.3"
        # And it is the same device, with the same entities.
        assert hass.states.get(entity_id(hass, "sensor", "rig_name")).state != "unavailable"
        device_entry = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SERIAL)})
        assert device_entry is not None
        assert device_entry.name == "Studio Profiler"
        assert device_entry.sw_version == "10.5.3"
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_uses_the_stored_host_when_nothing_answers(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """A held discovery port must not stop a Profiler that has not moved."""
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_HOST] == "127.0.0.1"
    assert device.connection_count(PROTOCOL_MIDI3_STREAM) == 1


async def test_a_lost_stream_is_rebuilt_in_place(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """The session comes back underneath the entities, not through a reload."""
    coordinator = entry.runtime_data
    await device.hangup()
    await wait_until(lambda: coordinator.reconnecting)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_DELAYS[0] + 1))
    await hass.async_block_till_done()
    await wait_until(lambda: not coordinator.reconnecting)

    # Same entry, same coordinator, same detector: only the socket is new.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is coordinator
    assert device.connection_count(PROTOCOL_MIDI3_STREAM) == 2


async def test_the_readings_survive_the_gap(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """What the entities showed before the drop is what they show during it.

    This is the whole point of rebuilding in place: a blink must not reach the
    logbook as a rig that went unavailable, went unknown, and came back.
    """
    rig = entity_id(hass, "sensor", "rig_name")
    # Wait for the opening burst: a reading can only survive a gap once it
    # exists, and comparing "unknown" to itself would prove nothing.
    await wait_until(lambda: hass.states.get(rig).state not in ("unknown", "unavailable"))
    before = hass.states.get(rig).state

    coordinator = entry.runtime_data
    await device.hangup()
    await wait_until(lambda: coordinator.reconnecting)
    await hass.async_block_till_done()

    assert hass.states.get(rig).state == before


async def test_a_gap_that_outlives_the_grace_goes_unavailable(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Stale readings are worth showing for a while, not forever."""
    coordinator = entry.runtime_data
    device.pause_accepting()
    await device.hangup()
    await wait_until(lambda: coordinator.reconnecting)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=STALE_GRACE_SECONDS + 1))
    await hass.async_block_till_done()

    assert hass.states.get(entity_id(hass, "sensor", "rig_name")).state == "unavailable"
    assert entry.state is ConfigEntryState.LOADED  # still loaded, still trying


async def test_a_device_that_stays_away_keeps_being_dialed(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """A Profiler switched off overnight is found again without anyone helping."""
    coordinator = entry.runtime_data
    device.pause_accepting()
    await device.hangup()
    await wait_until(lambda: coordinator.reconnecting)

    for delay in RECONNECT_DELAYS[:3]:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=delay + 1))
        await hass.async_block_till_done()
    assert coordinator.reconnecting

    device.resume_accepting()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_DELAYS[-1] + 1))
    await hass.async_block_till_done()
    await wait_until(lambda: not coordinator.reconnecting)

    assert hass.states.get(entity_id(hass, "sensor", "rig_name")).state != "unavailable"


async def test_unloading_during_a_gap_stops_the_dialing(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Nothing dials after the entry is gone, and nothing is left armed."""
    coordinator = entry.runtime_data
    device.pause_accepting()
    await device.hangup()
    await wait_until(lambda: coordinator.reconnecting)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    device.resume_accepting()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_DELAYS[-1] + 1))
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert device.connection_count(PROTOCOL_MIDI3_STREAM) == 1


async def test_every_entity_is_keyed_by_the_serial(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Entity identity survives a move, because it never mentions the address."""
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    unique_ids = {item.unique_id for item in entities}
    assert unique_ids == {
        f"{SERIAL}_rig_name",
        f"{SERIAL}_amp_name",
        f"{SERIAL}_cabinet_name",
        f"{SERIAL}_last_activity",
        f"{SERIAL}_active",
    }
