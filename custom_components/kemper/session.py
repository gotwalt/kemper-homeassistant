"""Opening a session to the Profiler: where it is, and dialing it.

Both the setup path and the coordinator's reconnect need this, and the
coordinator cannot import the package root, so it lives here.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from libkp import ConnectOptions, ControlPolicy, DeviceModel, RecyclePolicy
from libkp.protocol import PORT

from .const import CONF_SERIAL, CONF_SW_VERSION
from .discovery import async_find_serial

_LOGGER = logging.getLogger(__name__)

#: How the integration connects. The CBOR control channel is deliberately off:
#: the only thing it adds over the stream is the morph position, which nothing
#: here surfaces, and it would cost the device a second socket for as long as
#: Home Assistant runs. libkp's own reconnect is off too — it would redial the
#: address it was given, and the coordinator wants discovery in that loop.
CONNECT_CONTROL = ControlPolicy.OFF

#: How long one session lives. This is libkp's default, named here because it
#: is the whole reason a Home Assistant session is safe to leave running: a
#: Profiler asked to hold one connection for hours has been seen to stop
#: serving and flash its LEDs red, so libkp retires the session every ten
#: minutes and opens another in its place. Entities never see it — the tree
#: and the readings survive the swap, and the second or so it takes falls well
#: inside the coordinator's stale grace. If a swap cannot reopen, libkp reports
#: ``Disconnected`` and the coordinator's own loop, discovery and all, takes
#: over from there.
CONNECT_RECYCLE = RecyclePolicy()


async def async_locate(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """The address to dial, after asking the network where the serial is.

    Returns the stored host unchanged when the entry predates serial-keying,
    when nothing answers, or when the device is where it was; otherwise the
    entry is updated in place — the address, and the name and version, which a
    firmware update or a rename changes just as quietly.
    """
    host: str = entry.data[CONF_HOST]
    serial: str | None = entry.data.get(CONF_SERIAL)
    if not serial:
        return host

    found = await async_find_serial(serial)
    if found is None:
        return host

    updates = {
        key: value
        for key, value in (
            (CONF_HOST, found.host),
            (CONF_NAME, found.name),
            (CONF_SW_VERSION, found.version),
        )
        if entry.data.get(key) != value
    }
    if not updates:
        return host
    if CONF_HOST in updates:
        _LOGGER.info(
            "Profiler %s answered from %s instead of %s; following it",
            serial,
            found.host,
            host,
        )
    hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
    return found.host


async def async_open(
    hass: HomeAssistant, entry: ConfigEntry, *, locate: bool = True
) -> DeviceModel:
    """Dial the Profiler and return the connected model.

    ``locate`` decides whether the network is asked where the serial is first.
    A reconnect skips it for its first attempts: a device that dropped a second
    ago is nearly always still at the address it was at, and discovery costs a
    broadcast and a port that Rig Manager may be holding.

    Raises whatever the connect raises — ``LibKPError`` or ``OSError`` — for
    the caller to turn into a retry or a failed setup.
    """
    host = await async_locate(hass, entry) if locate else entry.data[CONF_HOST]
    options = ConnectOptions(
        port=entry.data.get(CONF_PORT, PORT),
        control=CONNECT_CONTROL,
        recycle=CONNECT_RECYCLE,
    )
    return await DeviceModel.connect(host, options=options)
