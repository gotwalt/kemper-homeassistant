"""The coordinator: one Profiler, dialed only for as long as it is being played.

**The session is duty-cycled.** A Profiler that is held open by Home Assistant
has wedged, every time, at about a day of uptime: six red LEDs, off the
network. Holding one session, or re-dialing one every ten minutes, made no
difference, so the trigger looks like *how long the device has streamed*
rather than how long any one socket lived. So the device is not streamed at
all while nobody is playing it:

- every :data:`POLL_INTERVAL_SECONDS` a session is opened and listened to for
  :data:`SAMPLE_SECONDS` -- long enough for the opening burst to name the rig
  and for a few dozen meter frames to say whether anything is sounding;
- if the :class:`~.activity.ActivityDetector` hears nothing in that sample the
  session is closed, and the next one opens a poll interval later;
- if it hears signal, the session is **held**, and it is held until the
  detector settles off -- the configured quiet window with nothing heard --
  at which point it is closed and the polling resumes.

A quiet close is routine and says nothing about the device, so the entities
keep their readings across it and nothing is logged above DEBUG.

libkp's model is already a store -- it holds the device state and hands out a
fresh snapshot whenever *slow* state changes -- so while a session is open the
coordinator's data arrives from a background task that does nothing but drain
the model's snapshot queue. That task is also where a lost stream is noticed.
**The entry is never reloaded for a lost stream.** A reload tears every entity
down and builds it again from an empty tree, which reaches the logbook as a
burst of ``unavailable`` and ``unknown`` rows for readings that never actually
changed. So the session is rebuilt underneath the entities instead: same
coordinator, same detector, same values on screen, one line in the log.

A session the device *drops* (or one that fails to open) is paced with
:data:`RECONNECT_DELAYS` and retried for as long as the entry is loaded -- a
Profiler that is switched off overnight is found again in the morning without
anyone touching Home Assistant. The first attempts dial the address as it
stands; from :data:`DISCOVERY_FROM_ATTEMPT` discovery joins in, because a
device that has been gone this long may have come back on another DHCP lease.

Two things keep the entity layer quiet across all that:

- readings stay live between polls, and for :data:`STALE_GRACE_SECONDS` after
  a drop or a failed poll, so an ordinary blip never reaches the dashboard;
- a new session's snapshots are held back until it has named a rig, so the
  half-second before the opening burst lands cannot blank the sensors.

The fast lane (meters, beat pulse, tuner deviance) never reaches this class.
It is read only by :class:`~.activity.ActivityDetector`, which turns it into
two state writes per playing session, and which follows each new model with
everything it has heard so far intact. This class only listens to the
detector's two transitions, which are what decide when to hold and let go.
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

#: How often a session is opened to ask whether anyone is playing, in seconds.
#: This bounds how late *Active* can turn on: the first note is heard at the
#: next poll, not the instant it is played.
POLL_INTERVAL_SECONDS = 30.0
#: How long each poll listens before hanging up on a quiet device, in seconds.
#: The opening burst names the rig in well under a second, and the meters run
#: at ~20 Hz, so this is ~60 frames: enough to hear anyone actually playing.
SAMPLE_SECONDS = 3.0
#: How a dropped session, or a poll that cannot connect, paces its retries, in
#: seconds. The last delay repeats for as
#: long as it takes: a device that is off is not a device that is gone.
RECONNECT_DELAYS = (2.0, 5.0, 15.0, 30.0, 60.0)
#: The attempt from which discovery is asked where the serial is, rather than
#: dialing the stored address. Two quick tries cover the blink; past that, the
#: address itself is worth doubting.
DISCOVERY_FROM_ATTEMPT = 3
#: How long the entities keep showing their last reading once the device has
#: stopped answering -- a dropped session, or a poll that could not connect.
#: Longer than the first three attempts, so a drop that is recovered promptly
#: is invisible to the dashboard and to the logbook.
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
        #: Whether the device answered the last time it was dialed. False from
        #: a drop or a failed poll until the next session opens.
        self._reachable = True
        #: Whether the open session is being closed by choice -- a quiet poll,
        #: or the detector settling off -- rather than dropped by the device.
        self._releasing = False
        #: Ends the open session's quiet sample. ``None`` once it has fired,
        #: once signal has been heard, or while no session is open.
        self._sample_timer: CALLBACK_TYPE | None = None
        self._remove_activity_listener: CALLBACK_TYPE | None = None
        #: Whether the open session has said what is loaded; until it has, its
        #: snapshots are held and the previous session's readings stand.
        self._synced = False
        self._sync_deadline = dt_util.utcnow()
        #: When the last reading stops being worth showing, while the device is
        #: not answering. ``None`` whenever it is.
        self._grace_until: datetime | None = None
        self._grace_timer: CALLBACK_TYPE | None = None
        #: Set once the entry is being torn down, so the disconnection the
        #: teardown itself causes is not mistaken for the device going away.
        self._closing = False

    # -- what the entity layer reads -------------------------------------

    @property
    def readings_live(self) -> bool:
        """Whether what the entities hold is worth showing.

        True while the device is answering -- a session open, or closed by
        choice between polls -- and for :data:`STALE_GRACE_SECONDS` after it
        stops: the window in which a reconnect usually lands, and in which a
        reading a few seconds old is a better answer than *unavailable*.
        """
        if self._reachable:
            return True
        return self._grace_until is not None and dt_util.utcnow() < self._grace_until

    @property
    def connected(self) -> bool:
        """Whether a session is open right now -- a poll's sample, or a hold."""
        return self._connected

    @property
    def reconnecting(self) -> bool:
        """Whether the device has stopped answering and is being dialed again.

        Not true between routine polls: a session closed because nobody was
        playing is not a session that needs rebuilding.
        """
        return not self._reachable and not self._closing

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
        """Seed the first snapshot, attach the detector, start listening.

        The session setup opened is treated as the first poll: it is sampled,
        and held only if someone is playing.
        """
        self._remove_activity_listener = self.activity.add_listener(self._activity_changed)
        self._open_session()
        self.async_set_updated_data(self.model.state())
        self._synced = True  # setup connected; its burst is what seeded the data
        self.activity.start()
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN} {self.device_id} session"
        )

    async def _run(self) -> None:
        """Poll the device, and hold a session while it is being played."""
        while not self._closing:
            await self._pump()
            if self._closing:
                return
            released = self._end_session()
            await self._redial(lost=not released)

    async def _pump(self) -> None:
        """Drain the model's store until the stream ends.

        Every snapshot is an entity update; the one thing a snapshot can say
        that this class acts on rather than passes along is that the stream has
        closed -- which it also says when this class closed it on purpose.
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

    async def _redial(self, *, lost: bool) -> None:
        """Open the next session, however many attempts that takes.

        After a quiet close the next attempt is the next poll; after a drop it
        is :data:`RECONNECT_DELAYS`'s first, because a device that was being
        played a moment ago is probably still being played.
        """
        with contextlib.suppress(LibKPError, OSError):
            await self.model.close()

        failures = 0
        while not self._closing:
            if lost or failures:
                delay = RECONNECT_DELAYS[min(failures, len(RECONNECT_DELAYS) - 1)]
            else:
                delay = POLL_INTERVAL_SECONDS
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                model = await async_open(
                    self.hass,
                    self.config_entry,
                    locate=failures + 1 >= DISCOVERY_FROM_ATTEMPT,
                )
            except (LibKPError, OSError) as err:
                failures += 1
                # Once at INFO, then quietly: a device that is off would
                # otherwise write a line a minute for as long as it is off.
                log = _LOGGER.info if failures == 1 else _LOGGER.debug
                log("Could not reach the Profiler (attempt %d): %s", failures, err)
                if self._reachable:
                    self._lose_contact()
                continue

            recovered = not self._reachable
            self.model = model
            self.activity.rebind(model)
            self._open_session()
            if recovered:
                _LOGGER.info("Back on the Profiler after %d attempt(s)", failures + 1)
            else:
                _LOGGER.debug("Polling the Profiler")
            self.async_update_listeners()
            return

    # -- session bookkeeping ---------------------------------------------

    @callback
    def _open_session(self) -> None:
        """A session is up: readings are live, its burst is awaited, and it
        has :data:`SAMPLE_SECONDS` to hear someone playing."""
        self._connected = True
        self._reachable = True
        self._releasing = False
        self._synced = False
        self._sync_deadline = dt_util.utcnow() + timedelta(seconds=SYNC_TIMEOUT_SECONDS)
        self._cancel_grace()
        self._cancel_sample()
        if not self.activity.active:
            self._sample_timer = async_call_later(self.hass, SAMPLE_SECONDS, self._sample_over)

    @callback
    def _end_session(self) -> bool:
        """The stream has closed. Returns whether this class closed it; if the
        device did, the grace in which readings still stand begins."""
        released = self._releasing
        self._connected = False
        self._releasing = False
        self._cancel_sample()
        # The device's own once-a-second counter restarts with each session,
        # so its last value is how long this one lasted. It is logged here,
        # once per session, rather than carried on the entities: under polling
        # it would change at every poll and put a row in the recorder each
        # time. A session that is *dropped* is the one worth reading about.
        age = self.model.state().session_counter
        lasted = "an unknown time" if age is None else f"{age}s"
        if released:
            _LOGGER.debug("Closed the session to the Profiler after %s", lasted)
        else:
            _LOGGER.info(
                "Lost the stream to the Profiler after %s of session; rebuilding it", lasted
            )
            self._lose_contact()
        return released

    @callback
    def _lose_contact(self) -> None:
        """The device stopped answering: start the grace in which readings
        still stand, and tell the entities when it runs out."""
        self._reachable = False
        self._grace_until = dt_util.utcnow() + timedelta(seconds=STALE_GRACE_SECONDS)
        self._cancel_grace(keep_deadline=True)
        self._grace_timer = async_call_later(self.hass, STALE_GRACE_SECONDS, self._grace_expired)

    @callback
    def _sample_over(self, _now: object) -> None:
        """The poll has listened long enough: hang up unless it heard signal."""
        self._sample_timer = None
        if not self.activity.active:
            self._release()

    @callback
    def _activity_changed(self) -> None:
        """The detector turned on or settled off: hold the session, or let it go."""
        if not self._connected:
            return
        if self.activity.active:
            self._cancel_sample()
            _LOGGER.info("Signal from the Profiler; holding the session while it plays")
        else:
            _LOGGER.info("The Profiler has been quiet for its window; back to polling")
            self._release()

    @callback
    def _release(self) -> None:
        """Close the open session by choice. The pump sees it end, and the run
        loop, seeing :attr:`_releasing`, waits a poll interval rather than
        treating it as a drop."""
        if self._releasing or not self._connected or self._closing:
            return
        self._releasing = True
        self.config_entry.async_create_background_task(
            self.hass, self.model.close(), name=f"{DOMAIN} {self.device_id} release"
        )

    @callback
    def _cancel_sample(self) -> None:
        if self._sample_timer is not None:
            self._sample_timer()
            self._sample_timer = None

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
        self._cancel_sample()
        if self._remove_activity_listener is not None:
            self._remove_activity_listener()
            self._remove_activity_listener = None
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
