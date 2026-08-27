<img src="custom_components/kemper/brand/logo.png" alt="" align="right" width="96">

# Kemper Profiler — Home Assistant integration

[![HACS](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/gotwalt/kemper-homeassistant/actions/workflows/validate.yml/badge.svg)](https://github.com/gotwalt/kemper-homeassistant/actions/workflows/validate.yml)

A custom integration that puts a Kemper Profiler on the local network into
Home Assistant: what rig is loaded, and whether anyone is playing through it.

It holds one MIDI3 session for as long as Home Assistant runs, takes
everything it shows from what the device pushes unrequested, and never polls
the device or reconnects in a loop. The protocol underneath is
[libkp](https://github.com/gotwalt/libkp), which Home Assistant installs from
PyPI when it first loads the integration.

A Profiler is identified by the **serial number** it advertises, not by its
address. Every setup broadcasts once to ask where that serial is now, so a
device whose DHCP lease moves it to another address is followed automatically:
the entry, the device and all five entities stay exactly as they were, history
included. Discovery finding nothing — the port held by Rig Manager, a quiet
network — is not an error; the last known address is used as it stands.

```
Kemper Profiler
├─ sensor.<device>_rig            Rig name        "Crunchy Vox"
├─ sensor.<device>_amp            Amp name        "Vintage Twin"
├─ sensor.<device>_cabinet        Cabinet name    "2x12 Alnico"
├─ binary_sensor.<device>_active  Playing?        on / off
└─ sensor.<device>_last_activity  Last activity   timestamp
```

## The entities

| Entity | What it is |
|---|---|
| `sensor` Rig / Amp / Cabinet | The names the device pushes on a rig change. They follow the front panel, a MIDI controller, Rig Manager — anything that loads a rig. |
| `binary_sensor` Active | On while signal is passing through the rig. See below. |
| `sensor` Last activity | When signal was last heard. While *Active* is on it is when the current session began; when *Active* goes off it is the moment of the last note. |

Everything else the device says — the effect slots, the tempo, the volumes,
the tuner, the bank preview, both channels' states — is in the integration's
**diagnostics** download rather than in entities. Adding an entity for any of
it is one row in the table in `sensor.py`.

### Activity detection

The Profiler pushes a meter frame about twenty times a second. Writing an
entity per frame would put 72,000 states an hour into the recorder to say
"someone is playing", so the meter lane is read by one plain callback that
compares a single 14-bit integer per frame and writes Home Assistant state
only when the answer changes: **two state writes per playing session**,
however long the session runs.

The level it reads is the **rig output** meter — after the rig's own volume,
before the master/monitor/headphone volumes — so a rig turned down reads
quiet, but practising with the monitors off still reads as playing.

Two options (Settings → Devices & services → Kemper Profiler → Configure):

- **Quiet window** — how long the output must stay below the threshold before
  *Active* turns off. Default 5 minutes.
- **Level threshold** — how loud counts as playing, as a percentage of full
  scale. Default 2%.

Saving them retunes the running detector; it does **not** reconnect to the
device.

### Losing the connection

When the stream ends — the amp switched off, the network dropped — the
integration does not redial the address it was using, because that address is
the part that can change. It reloads the config entry instead, which starts
again at discovery: find the serial, follow it to wherever it is now, connect
once. A Profiler that is simply off fails that setup and Home Assistant retries
on its own widening schedule; a session that ends within a minute of opening
waits half a minute before reloading, so nothing can spin.

## Install

### HACS

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=gotwalt&repository=kemper-homeassistant&category=integration)

The badge opens HACS on this repository. Until it lands in the HACS default
store, add it by hand first: **HACS → ⋮ → Custom repositories**, the URL
`https://github.com/gotwalt/kemper-homeassistant`, category **Integration**.
Then **Download**, and restart Home Assistant.

### By hand

Build the bundle and copy it into your Home Assistant configuration directory,
next to `configuration.yaml`:

```sh
uv run python build.py                      # dist/custom_components/kemper + a zip
uv run python build.py --install ~/homeassistant
```

For a Home Assistant OS or supervised install, take
`dist/kemper-<version>.zip` and unpack it into the configuration directory
with the **Samba share**, **Terminal & SSH**, or **File editor** add-on — its
paths are already `custom_components/kemper/…`.

### Then

1. Restart Home Assistant.
2. **Settings → Devices & services → Add integration → "Kemper Profiler"**.
3. The flow broadcasts for Profilers on the LAN and lists what answers. If
   nothing answers — Rig Manager holds the discovery port exclusively, and so
   does a running `meters` example — choose *Enter a host manually* and give
   the Profiler's IP address.

Home Assistant 2024.11 or newer. The integration itself is pure Python and its
only requirement is `libkp`, which Home Assistant installs from PyPI on first
load; everything after that is on the local network, with nothing to sign in
to and no cloud in the path.

## Development

```sh
uv sync                     # Python 3.14, Home Assistant 2026.8, the pinned libkp
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python build.py
```

The tests are end-to-end over a real loopback socket: they drive
`libkp.testing.FakeDevice`, push the same bytes a Profiler pushes, and assert
on entity states. Nothing below the config entry is mocked except the discovery
broadcast.

`manifest.json` pins `libkp` exactly, and `pyproject.toml` installs that same
version, so the suite runs against what Home Assistant will install —
`tests/test_manifest.py` fails if the two ever drift. To move to a new libkp,
change both, then run the suite.

To work against an unreleased libkp — a checkout beside this one:

```sh
uv run --with-editable ../libkp/python pytest -q
```

## Related

- [libkp](https://github.com/gotwalt/libkp) — the protocol: the spec, and
  implementations in Python, Rust and Swift.
