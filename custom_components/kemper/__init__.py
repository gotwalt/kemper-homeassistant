"""The Kemper Profiler integration: one config entry, one device, one session.

Setting an entry up opens exactly one MIDI3 stream to the Profiler and keeps
it. The device tolerates a session; what it does not tolerate is connection
*churn* (``docs/06``, ``docs/11``), so nothing here dials in a loop.

**Where the device is** is decided fresh at every setup. The entry's identity
is the serial the Profiler advertises, not its address: an entry that knows a
serial broadcasts once, and if that serial answers from somewhere else the
entry is updated to the new address (and to the name and firmware version,
which change too) before anything is dialed. Discovery finding nothing — the
port held by Rig Manager, a quiet network, a device on another subnet — is not
an error; the stored address is used as it stands.

**Losing the stream** does not come back through this door. Reloading the
entry would rebuild every entity from an empty tree and fill the logbook with
readings that never changed, so the coordinator rebuilds the session in place
instead, with discovery in its own retry loop once the address is worth
doubting (``coordinator``). Setup is therefore the *first* connection only: a
device that is switched off when Home Assistant starts fails with
:class:`ConfigEntryNotReady`, which is Home Assistant's own spaced retry.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from libkp import LibKPError

from .coordinator import KemperConfigEntry, KemperCoordinator
from .session import async_open

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: KemperConfigEntry) -> bool:
    """Find the Profiler, connect to it, and bring its entities up."""
    try:
        model = await async_open(hass, entry)
    except (LibKPError, OSError) as err:
        raise ConfigEntryNotReady(f"could not connect to the Profiler: {err}") from err

    coordinator = KemperCoordinator(hass, entry, model)
    entry.runtime_data = coordinator
    try:
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Whatever went wrong, the socket does not get to outlive the attempt.
        await coordinator.async_shutdown()
        raise

    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KemperConfigEntry) -> bool:
    """Tear the entities down and hang up on the device."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options in place — never by reloading the entry.

    A reload would close the session and open another one, which is a real cost
    to the device; the two options that exist only steer the activity detector,
    and it can be retuned while it runs. The same listener sees the address
    updates :func:`async_locate` makes, which need no action at all: they are
    already what the running session was dialed with.
    """
    coordinator: KemperCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        coordinator.apply_options()
