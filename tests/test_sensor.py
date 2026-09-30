"""The name sensors, driven by what the device actually pushes.

The messages come from libkp's own message builders, so what these tests put
on the wire is byte-for-byte what a Profiler puts there when a rig is loaded.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import entity_id, wait_for_state, wait_until
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from libkp import _generated as gen
from libkp.nrpn import PAGE_STRINGS, ext_encode, sysex
from libkp.testing import FakeDevice
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.kemper.coordinator import STALE_GRACE_SECONDS

#: The page-0 string tags a rig change pushes, by their spec names.
RIG_NAME = gen.STRING_RIG_NAME
AMP_NAME = gen.STRING_AMP_NAME
CABINET_NAME = gen.STRING_CABINET_NAME


def session_counter(seconds: int) -> bytes:
    """A ``$06`` extended-parameter push at the device's once-a-second counter."""
    # A `$06` spends the header's page/number bytes on the first two of its
    # five-byte address, so the rest of the address rides with the value.
    address = ext_encode(gen.SESSION_COUNTER_ADDRESS, 5)
    return sysex(
        0x00,
        0x00,
        gen.FN_EXT_PARAM,
        address[0],
        address[1],
        bytes(address[2:]) + ext_encode(seconds, 5),
    )


def string_tag(number: int, text: str) -> bytes:
    """A ``$03`` String Parameter push, as a rig change sends."""
    return sysex(0x00, 0x00, 0x03, PAGE_STRINGS, number, text.encode("ascii") + b"\x00")


async def test_the_names_follow_the_device(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Load a rig on the device and the three sensors say what it is."""
    await device.push(string_tag(RIG_NAME, "Crunchy Vox"))
    await device.push(string_tag(AMP_NAME, "Vintage Twin"))
    await device.push(string_tag(CABINET_NAME, "2x12 Alnico"))

    await wait_for_state(hass, entity_id(hass, "sensor", "rig_name"), "Crunchy Vox")
    await wait_for_state(hass, entity_id(hass, "sensor", "amp_name"), "Vintage Twin")
    await wait_for_state(hass, entity_id(hass, "sensor", "cabinet_name"), "2x12 Alnico")


async def test_a_second_rig_replaces_the_first(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Nothing is cached across rigs: the last push wins."""
    await device.push(string_tag(RIG_NAME, "Crunchy Vox"))
    await wait_for_state(hass, entity_id(hass, "sensor", "rig_name"), "Crunchy Vox")

    await device.push(string_tag(RIG_NAME, "Clean Twin"))
    await wait_for_state(hass, entity_id(hass, "sensor", "rig_name"), "Clean Twin")


async def test_a_stream_that_stays_lost_makes_the_sensors_unavailable(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Availability follows the readings, not the last value seen.

    A reading holds through the grace the coordinator rebuilds the session in;
    what it must not do is hold forever, once nothing is coming back.
    """
    await device.push(string_tag(RIG_NAME, "Crunchy Vox"))
    rig = entity_id(hass, "sensor", "rig_name")
    await wait_for_state(hass, rig, "Crunchy Vox")

    device.pause_accepting()
    await device.hangup()
    await wait_until(lambda: entry.runtime_data.reconnecting)
    assert hass.states.get(rig).state == "Crunchy Vox"  # still worth showing

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=STALE_GRACE_SECONDS + 1))
    await hass.async_block_till_done()

    await wait_for_state(hass, rig, "unavailable")
    await wait_for_state(hass, entity_id(hass, "sensor", "last_activity"), "unavailable")


async def test_the_session_counter_is_logged_when_a_session_is_lost(
    hass: HomeAssistant,
    device: FakeDevice,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped session says how long it lasted, once, in the log."""
    await device.push(session_counter(4242))
    coordinator = entry.runtime_data
    await wait_until(lambda: coordinator.model.state().session_counter == 4242)

    await device.hangup()
    await wait_until(lambda: coordinator.reconnecting)

    assert "after 4242s of session" in caplog.text


async def test_the_session_counter_never_reaches_the_entities(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Every poll restarts the counter, so an attribute carrying it would put a
    row in the recorder at every poll. It is kept out of the state entirely."""
    await device.push(session_counter(4242))
    await device.push(string_tag(RIG_NAME, "Crunchy Vox"))

    rig = entity_id(hass, "sensor", "rig_name")
    await wait_for_state(hass, rig, "Crunchy Vox")
    assert "session_seconds" not in hass.states.get(rig).attributes
