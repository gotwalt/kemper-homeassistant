"""The push coordinator: one :class:`DeviceModel`, one state tree, one device.

libkp's model is already a store — it holds the device state and hands out a
fresh snapshot whenever *slow* state changes, coalesced to at most one per
ingested chunk. So there is nothing to poll here and no update interval: the
coordinator is a :class:`DataUpdateCoordinator` whose data arrives from a
background task that does nothing but drain the model's snapshot queue.

That task is also where a lost stream is noticed, and it is the coordinator
that gets it back. **The entry is never reloaded for a lost stream.** A reload
tears every entity down and builds it again from an empty tree, which reaches
the logbook as a burst of ``unavailable`` and ``unknown`` rows for readings
that never actually changed — and a Profiler drops a session often enough
(idle, a network blink) for that to be most of what the log says. So the
session is rebuilt underneath the entities instead: same coordinator, same
detector, same values on screen, one line in the log.

Rebuilding paces itself with :data:`RECONNECT_DELAYS` and keeps going for as
long as the entry is loaded — a Profiler that is switched off overnight is
found again in the morning without anyone touching Home Assistant. The first
attempts dial the address as it stands, because a device that hiccuped is
nearly always still there; from :data:`DISCOVERY_FROM_ATTEMPT` discovery joins
in, because one that has been gone this long may have come back on another
DHCP lease. Only then does the address get looked up — which is what the
reload used to be for.

Two things keep the entity layer quiet across all that:

- readings stay live for :data:`STALE_GRACE_SECONDS` after a drop, so an
  ordinary blip never reaches the dashboard at all; and
- a new session's snapshots are held back until it has named a rig, so the
  half-second before the opening burst lands cannot blank the sensors.

The fast lane (meters, beat pulse, tuner deviance) never reaches this class.
It is read only by :class:`~.activity.ActivityDetector`, which turns it into
two state writes per playing session, and which follows the new model across a
reconnect with everything it has heard so far intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from libkp import LibKPError
from libkp.model import DeviceModel
from libkp.state import Connection, DeviceState

from .activity import ActivityDetector
from .const import (
    CONF_ACTIVITY_THRESHOLD,
    CONF_ACTIVITY_WINDOW,
    CONF_SW_VERSION,
    DEFAULT_ACTIVITY_THRESHOLD,
    DEFAULT_ACTIVITY_WINDOW,
    DEFAULT_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .session import async_open

_LOGGER = logging.getLogger(__name__)

#: How the reconnect paces itself, in seconds. The last delay repeats for as
#: long as it takes: a device that is off is not a device that is gone.
RECONNECT_DELAYS = (2.0, 5.0, 15.0, 30.0, 60.0)
#: The attempt from which discovery is asked where the serial is, rather than
#: dialing the stored address. Two quick tries cover the blink; past that, the
#: address itself is worth doubting.
DISCOVERY_FROM_ATTEMPT = 3
#: How long the entities keep showing their last reading while a session is
#: being rebuilt. Longer than the first three attempts, so a drop that is
#: recovered promptly is invisible to the dashboard and to the logbook.
STALE_GRACE_SECONDS = 30.0
#: How long a fresh session may go without naming a rig before its snapshots
#: are published anyway. The gate is there to stop the opening burst blanking
#: the sensors, not to hold back a device whose rig has no name.
SYNC_TIMEOUT_SECONDS = 10.0

#: The entry, typed by what :attr:`ConfigEntry.runtime_data` holds.
type KemperConfigEntry = ConfigEntry[KemperCoordinator]


def activity_window(entry: ConfigEntry) -> float:
    """The configured quiet window, in seconds (the form asks for minutes)."""
    return float(entry.options.get(CONF_ACTIVITY_WINDOW, DEFAULT_ACTIVITY_WINDOW)) * 60.0


def activity_threshold(entry: ConfigEntry) -> float:
    """The configured level threshold, in percent of the meter full scale."""
    return float(entry.options.get(CONF_ACTIVITY_THRESHOLD, DEFAULT_ACTIVITY_THRESHOLD))


class KemperCoordinator(DataUpdateCoordinator[DeviceState]):
    """Publishes the model's slow-lane snapshots to the entity layer."""

    config_entry: KemperConfigEntry

    def __init__(self, hass: HomeAssistant, entry: KemperConfigEntry, model: DeviceModel) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=None,
        )
        self.model = model
        self.activity = ActivityDetector(
            hass,
            model,
            window=activity_window(entry),
            threshold=activity_threshold(entry),
        )
        self._task: asyncio.Task[None] | None = None
        #: Whether a session is open right now.
        self._connected = True
        #: Whether the open session has said what is loaded; until it has, its
        #: snapshots are held and the previous session's readings stand.
        self._synced = False
        self._sync_deadline = dt_util.utcnow()
        #: When the last reading stops being worth showing, while no session is
        #: open. ``None`` whenever one is.
        self._grace_until: datetime | None = None
        self._grace_timer: CALLBACK_TYPE | None = None
        #: Set once the entry is being torn down, so the disconnection the
        #: teardown itself causes is not mistaken for the device going away.
        self._closing = False

    # -- what the entity layer reads -------------------------------------

    @property
    def readings_live(self) -> bool:
        """Whether what the entities hold is worth showing.

        True while a session is open, and for :data:`STALE_GRACE_SECONDS` after
        one drops — the window in which a reconnect usually lands, and in which
        a reading a few seconds old is a better answer than *unavailable*.
        """
        if self._connected:
            return True
        return self._grace_until is not None and dt_util.utcnow() < self._grace_until

    @property
    def reconnecting(self) -> bool:
        """Whether the session is currently being rebuilt."""
        return not self._connected and not self._closing

    @property
    def device_id(self) -> str:
        """The device-registry identifier: the serial when discovery knew it,
        else the host, else the entry — stable across restarts either way."""
        entry = self.config_entry
        return entry.unique_id or entry.entry_id

    @property
    def device_info(self) -> DeviceInfo:
        """One device per config entry: the Profiler itself."""
        entry = self.config_entry
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=entry.data.get(CONF_NAME) or DEFAULT_NAME,
            sw_version=entry.data.get(CONF_SW_VERSION),
        )

    # -- lifecycle -------------------------------------------------------

    async def async_start(self) -> None:
        """Seed the first snapshot, attach the detector, start listening."""
        self._open_session()
        self.async_set_updated_data(self.model.state())
        self._synced = True  # setup connected; its burst is what seeded the data
        self.activity.start()
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN} {self.device_id} session"
        )

    async def _run(self) -> None:
        """Hold a session for as long as the entry is loaded."""
        while not self._closing:
            await self._pump()
            if self._closing:
                return
            self._lose_session()
            await self._reconnect()

    async def _pump(self) -> None:
        """Drain the model's store until the stream ends.

        Every snapshot is an entity update; the one thing a snapshot can say
        that this class acts on rather than passes along is that the device
        has gone.
        """
        queue = self.model.subscribe()
        try:
            while True:
                state = await queue.get()
                if state.connection is Connection.DISCONNECTED:
                    return
                self._publish(state)
        finally:
            self.model.unsubscribe(queue)

    async def _reconnect(self) -> None:
        """Open another session, however many attempts that takes."""
        with contextlib.suppress(LibKPError, OSError):
            await self.model.close()

        attempt = 0
        while not self._closing:
            attempt += 1
            await asyncio.sleep(RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS)) - 1])
            if self._closing:
                return
            try:
                model = await async_open(
                    self.hass,
                    self.config_entry,
                    locate=attempt >= DISCOVERY_FROM_ATTEMPT,
                )
            except (LibKPError, OSError) as err:
                # Once at INFO, then quietly: a device that is off would
                # otherwise write a line a minute for as long as it is off.
                log = _LOGGER.info if attempt == 1 else _LOGGER.debug
                log("Could not reach the Profiler (attempt %d): %s", attempt, err)
                continue

            self.model = model
            self.activity.rebind(model)
            self._open_session()
            _LOGGER.info("Back on the Profiler after %d attempt(s)", attempt)
            self.async_update_listeners()
            return

    # -- session bookkeeping ---------------------------------------------

    @callback
    def _open_session(self) -> None:
        """A session is up: readings are live, and its burst is awaited."""
        self._connected = True
        self._synced = False
        self._sync_deadline = dt_util.utcnow() + timedelta(seconds=SYNC_TIMEOUT_SECONDS)
        self._cancel_grace()

    @callback
    def _lose_session(self) -> None:
        """The stream ended: start the grace in which readings still stand."""
        self._connected = False
        self._grace_until = dt_util.utcnow() + timedelta(seconds=STALE_GRACE_SECONDS)
        self._cancel_grace(keep_deadline=True)
        self._grace_timer = async_call_later(self.hass, STALE_GRACE_SECONDS, self._grace_expired)
        _LOGGER.info("Lost the stream to the Profiler; rebuilding the session")

    @callback
    def _grace_expired(self, _now: object) -> None:
        """Long enough: the entities stop claiming to know anything."""
        self._grace_timer = None
        self._grace_until = None
        self.async_update_listeners()

    @callback
    def _cancel_grace(self, *, keep_deadline: bool = False) -> None:
        if self._grace_timer is not None:
            self._grace_timer()
            self._grace_timer = None
        if not keep_deadline:
            self._grace_until = None

    @callback
    def _publish(self, state: DeviceState) -> None:
        """Hand a snapshot to the entities, once it is worth showing.

        A session's first snapshots arrive before the device's opening burst
        has said what is loaded, so publishing them would blank every sensor
        for the half-second until the names land — which is exactly the
        ``unknown`` burst this class exists to avoid. The previous session's
        readings stand until the new one names a rig, or until it has had
        :data:`SYNC_TIMEOUT_SECONDS` to.
        """
        if not self._synced:
            if state.rig.name is None and dt_util.utcnow() < self._sync_deadline:
                return
            self._synced = True
        self.async_set_updated_data(state)

    async def async_shutdown(self) -> None:
        """Stop listening and hang up. The device sees one clean disconnect."""
        self._closing = True
        self._cancel_grace()
        await super().async_shutdown()
        self.activity.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.model.close()

    def apply_options(self) -> None:
        """Re-read the options the detector uses, without touching the socket."""
        entry = self.config_entry
        self.activity.update_options(
            window=activity_window(entry), threshold=activity_threshold(entry)
        )
