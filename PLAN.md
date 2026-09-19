# mad-midi — Implementation Plan

A tool to map every pin of an Arduino Mega 2560 to MIDI in/out, driven by a
Python desktop app with a local web UI.

## Architecture

```
+----------------+   USB-serial, 115200 baud    +----------------------+   CoreMIDI virtual port   +---------+
|  Arduino Mega  | <---------------------------> |  Python app          | <------------------------> |  DAW /  |
|  (firmware)    |   MIDI bytes (run mode) or    |  (pywebview + pyserial|                            |  synth  |
|  EEPROM config |   framed commands (config mode)|  + python-rtmidi)   |                            +---------+
+----------------+                                +----------------------+
                                                          |
                                                     local web UI
                                                (table view, board view,
                                                 MIDI monitor, presets)
```

Everything rides on one USB-serial link (bridge-only MIDI — no DIN hardware
in v1). The firmware has two modes:

- **RUN mode** (default on boot): bridges live MIDI both directions using
  whatever pin mapping is currently stored in EEPROM. Pin 13 LED: solid on.
- **CONFIG mode**: entered via a reserved SysEx trigger sent over the same
  link; only config-protocol traffic is expected, no MIDI bridging happens.
  Pin 13 LED: slow blink. Auto-returns to RUN mode after 15s of inactivity.

## Wire protocol

### Entering/leaving config mode (valid MIDI SysEx, works from RUN mode)

| Direction | Bytes | Meaning |
|---|---|---|
| App -> Firmware | `F0 7D 01 F7` | Enter config mode |
| Firmware -> App | `F0 7D 02 F7` | Ack: now in config mode |

`0x7D` is the MIDI-spec-reserved "non-commercial/private use" manufacturer ID,
so this can never collide with a real device's SysEx.

### Config-mode command framing (not MIDI-shaped — no bridging active)

```
[0xAA][CMD][LEN_LO][LEN_HI][PAYLOAD x LEN][CHECKSUM]
```

`CHECKSUM` = one-byte value such that `(CMD + LEN_LO + LEN_HI + sum(PAYLOAD) + CHECKSUM) mod 256 == 0`.

| CMD | Name | Payload (request) | Reply CMD | Reply payload |
|---|---|---|---|---|
| 0x01 | PING | — | 0x81 | `[fw_version, config_format_version, device_id]` |
| 0x02 | READ_CONFIG | — | 0x82 | full EEPROM blob |
| 0x03 | WRITE_CONFIG | full EEPROM blob | 0x83 / 0xC3 | — / `[error_code]` |
| 0x04 | RESUME_RUN | — | 0x84 | — |
| 0x05 | SET_DEVICE_ID | `[new_device_id]` | 0x85 / 0xC5 | — / `[error_code]` |

Reply convention: success = request CMD `| 0x80`, error = request CMD `| 0xC0`.

### EEPROM blob layout (776 bytes of 4096 available)

```
Header (6 bytes):
  [0-1] magic = 'M','M'
  [2]   config format version (currently 5)
  [3]   pin count (70)
  [4]   device id (0 = unassigned; set once via SET_DEVICE_ID, persists across reconnects/ports)
  [5]   checksum (sum of all record bytes, mod 256 — does NOT cover the device id byte,
                  so SET_DEVICE_ID can update it independently without a full config rewrite)

Records: 70 x 11 bytes, indexed by Arduino Mega pin number directly
  (0-53 = digital pins, 54-69 = A0-A15)
  Reserved indices, not user-configurable: 0, 1 (USB Serial0), 13 (status LED)

  PinConfig record:
    [0] role    (see PinRole below)
    [1] channel (0-15)
    [2] number  (note or CC number, 0-127)
    [3] inMin
    [4] inMax
    [5] outMin
    [6] outMax
    [7] flags        (bit0 invert, bit1 toggle-mode, bit2 match-CC-not-Note
                       [LIGHT_DIGITAL/LIGHT_PWM only, see below], bit3
                       FLAG_ACCEL_ENABLE [ENCODER_A only]; rest reserved)
    [8] shiftPin     (POT_CC only, see Pot shift fields below)
    [9] shiftChannel (POT_CC only)
    [10] shiftNumber (POT_CC only)
```

### PinRole enum (v1)

| Value | Role | Direction | Behavior |
|---|---|---|---|
| 0 | UNUSED | — | pin ignored |
| 1 | BUTTON_NOTE | digital-in -> MIDI out | press = Note On, release = Note Off |
| 2 | BUTTON_CC | digital-in -> MIDI out | momentary or toggle CC (flags bit1) |
| 3 | LIGHT_DIGITAL | MIDI in -> digital out | on/off from an incoming Note or CC (which one is chosen by flags bit2, `FLAG_MATCH_CC`) |
| 4 | LIGHT_PWM | MIDI in -> PWM out | `analogWrite(map(value, inMin,inMax, outMin,outMax))` — also used for VU meters (single-pin analog needle); Note vs CC selected the same way as LIGHT_DIGITAL |
| 5 | ENCODER_A | digital-in pair -> MIDI out | quadrature phase A; paired with the next pin (ENCODER_B). Relative CC: value `1` = +1 step (CW), value `127` = -1 step (CCW) per detent. With `FLAG_ACCEL_ENABLE` (flags bit3) set, the step magnitude scales with turn speed instead of staying fixed at 1 (see Encoder acceleration below). Fields are repurposed for shift support (see below) |
| 6 | ENCODER_B | — | marks the pin as consumed by the preceding ENCODER_A |
| 7 | POT_CC | analog-in -> MIDI out | raw ADC (0-1023, stored as inMin/inMax >>2) scaled to CC 0-127. Optional shift support via dedicated `shiftPin`/`shiftChannel`/`shiftNumber` fields (see below) — unlike ENCODER_A, inMin/inMax/outMin/outMax are already needed for ADC scaling, so shift needs its own fields |
| 8 | BUTTON_SHIFT | digital-in, modifier only | pure modifier: sends no MIDI of its own; while held, any ENCODER_A pin whose `inMin` names this pin's index switches to its shifted channel/CC |

#### Encoder shift fields (ENCODER_A only)

Encoders don't otherwise need a value range, so `inMin`/`outMin`/`outMax` are
repurposed for the "shift groups" model (an encoder can be shifted by one
specific button, not a device-wide modifier):

| Field | Meaning for ENCODER_A |
|---|---|
| `channel`, `number` | normal (unshifted) MIDI channel + CC number |
| `inMin` | pin index of the BUTTON_SHIFT button that shifts this encoder; `255` = no shift button assigned |
| `inMax` | unused |
| `outMin` | shifted-state MIDI channel |
| `outMax` | shifted-state MIDI CC number |

While the referenced shift button is held, a turn sends relative CC on
`(outMin, outMax)` instead of `(channel, number)` — same encoding either way
(including acceleration, if enabled).

#### Pot shift fields (POT_CC only)

Same shift-groups model as ENCODER_A above, but pots can't repurpose
inMin/inMax/outMin/outMax the way encoders do — those are already the ADC-
to-CC scaling range — so pots get three dedicated fields instead:

| Field | Meaning for POT_CC |
|---|---|
| `shiftPin` | pin index of the BUTTON_SHIFT button that shifts this pot; `255` (`NO_SHIFT_PIN`) = no shift button assigned |
| `shiftChannel` | shifted-state MIDI channel |
| `shiftNumber` | shifted-state MIDI CC number |

While the referenced shift button is held, the pot's (absolute) value is
sent on `(shiftChannel, shiftNumber)` instead of `(channel, number)`. Since
a pot only sends on value change, a shift *transition* with no pot movement
still forces one re-send on the newly active destination — otherwise it
would look stale until the pot moves (see `potLastShifted` in roles.cpp).

#### Encoder acceleration (ENCODER_A only)

Opt-in per encoder via `FLAG_ACCEL_ENABLE` (flags bit3), default on for newly
created pins in the app; existing devices keep the old fixed-1 behavior until
their config is re-saved and re-flashed, since the bit is unset on already-
written EEPROM blobs. When enabled, the relative CC value sent per detent
scales with how fast the encoder is turned, instead of always being `1`/`127`
— useful for parameters mapped over a wide range (e.g. a filter cutoff from
30Hz to 20kHz), which otherwise take up to 127 physical detents to sweep
since Ableton spreads the mapped range evenly over the 7-bit relative CC
range.

The acceleration curve is ported as-is from the ClickEncoder library
(0xPIT/encoder), whose constants happen to already be tuned for a ~1ms
service tick — the same cadence `roles.cpp`'s quadrature ISR runs at:
- a per-encoder counter decays by 2 every ISR tick (~1ms)
- bumped by 25 on each confirmed step
- capped at 3072
- turned into a step multiplier via `counter >> 8` (0-12) when a pending
  step is drained, so the sent value is `1 + multiplier`, capped at 63 (the
  ceiling of the two's-complement relative CC encoding)

These are fixed firmware constants, not app-configurable — tune by editing
`ENC_ACCEL_TOP` / `ENC_ACCEL_INC` / `ENC_ACCEL_DEC` in `roles.cpp` and
reflashing.

## Sequencer

An app-only step sequencer per device entry — not part of the wire
protocol or EEPROM blob at all. It reuses the same pin-config plumbing
(each step's controls are just pins already assigned a role in the entry's
pin list) rather than introducing a parallel config surface.

- **Clock source**: slaved entirely to the DAW's MIDI Clock/Start/Stop/
  Continue, received on the device's existing virtual MIDI-in port. There
  is no internal free-running clock, so a sequence never advances unless
  the DAW's transport is running. `MidiBridge` intercepts these four
  realtime message types before they'd otherwise be forwarded to the
  Arduino (which has no use for them) and routes them to the entry's
  `SequencerEngine` instead (`app/midi_bridge.py`, `app/sequencer.py`).
- **Timing**: step advancement is pure tick counting — 24 clock ticks per
  quarter note (the MIDI standard) — using a configurable ticks-per-step
  for 8th/16th/32nd/16th-triplet subdivisions. Wall-clock time is only
  used to smooth an average tick interval, which sizes each triggered
  step's gate length (fixed 50% of the step duration).
- **Per-step config** (`config_model.SequencerStepConfig`): a note number
  plus up to five pin indices borrowed from the entry's existing pin
  list — a button (enable/disable the step; also its own mute, no
  separate control), a value control (live CC while the step plays), a
  shift button (the value control's alternate CC, same shift-group
  mechanism as regular encoders/pots), a PWM pin (mirrors the value
  control's current value as a brightness/VU meter — see **Value mirror**
  below, which now drives this instead of the sequencer engine itself),
  and a step light (flashed for the gate duration while the step plays).
- **Step value control type** (`SequencerStepConfig.control_type`,
  `"encoder"` | `"pot"`, default `"encoder"`): each step independently
  chooses what role its own value-control pin gets assigned — a "Value
  control" dropdown right in that step's row, not a device-wide setting —
  so one step can use an encoder while another uses a pot on the same
  entry. `assignSequencerPin` in app.js wires it to either ENCODER_A (2
  digital pins, relative CC) or POT_CC (1 analog pin, absolute CC)
  accordingly, including the shift-group linkage either role needs.
  Switching it doesn't retroactively move an already-assigned pin — it has
  to be reassigned afterward. The engine itself never branches on this
  setting: `value_mirror.ValueMirrorEngine` (see **Value mirror** below)
  derives encoder-vs-pot behavior straight from the pin's actual role at
  resolve time, so a step's PWM meter already mirrors a pot's absolute CC
  directly and accumulates an encoder's relative clicks with no
  step-level configuration needed for that part.
- **Runtime vs. persisted state**: step count, subdivision, channel,
  per-step notes, and pin assignments are part of the entry and
  auto-saved to its JSON file. The on/off pattern and playback position
  are runtime-only, live on the `SequencerEngine` instance, and are lost
  on disconnect/app restart — restarting always comes up with every step
  disabled.
- **Message flow**: a step's button press arrives from the device like any
  other pin message; `DeviceSession._on_device_midi` gives the sequencer
  first look (after the value-mirror engine, which never consumes
  anything), and only messages it doesn't recognize as a sequencer input
  are forwarded on to the DAW. A triggered step's Note On/Off goes
  straight to the DAW via the bridge; step-light updates go the other
  direction, straight to the device, using the same synthetic CC messages
  the firmware already expects for `LIGHT_DIGITAL`/`LIGHT_PWM` pins.
- **UI**: a dedicated **Sequencer** tab per entry, separate from the
  Table/Board pin views, showing global settings (step count, subdivision,
  channel) and one row per step for its note and pin assignments; live
  step/enabled state streams into the UI while connected and running, and
  a step can also be toggled from the UI itself as a software fallback
  when no physical button is wired up.

## Value mirror (VU meter, any encoder/pot)

An app-side "mirror this control's live value onto a PWM pin's brightness"
mechanism, generalized out of what used to be sequencer-only logic
(`app/value_mirror.py`) so it works for **any** `encoder_a`/`pot_cc` pin —
part of a plain Table/Board mapping, a Sequencer step, a Drone segment, or
nothing composite at all.

- **`PinConfig.mirror_pin`** (`config_model.py`): app-only field on an
  `encoder_a`/`pot_cc` pin naming the `light_pwm` pin whose brightness
  should track it. Deliberately excluded from `to_tuple()`/`from_tuple()`
  (never part of the EEPROM blob) — the firmware has no idea this is
  happening, exactly like the original sequencer trick: it's just an
  ordinary CC arriving that happens to match a `light_pwm`+`match_cc` pin.
- **Accumulation**: an encoder only ever reports a relative click (1=CW,
  127=CCW, scaled by `FLAG_ACCEL_ENABLE`'s magnitude), never an absolute
  position, so `ValueMirrorEngine` keeps its own running value per mirrored
  control (seeded mid-brightness, clamped 0-127). A pot reports its actual
  absolute position, so it's mirrored directly with no accumulation.
- **Always active**: built unconditionally per `DeviceSession` (not gated
  by any feature's `enabled` flag), so a step/segment's meter — or a bare
  Table/Board encoder's — keeps updating even while its owning feature is
  disabled.
- **UI**: a "Mirror value to PWM pin (VU meter)" picker in the Table/Board
  editor panel for any `encoder_a`/`pot_cc` pin; Sequencer/Drone's own
  "PWM" pin-picker is sugar over the same field (assigning it sets the
  paired control pin's `mirror_pin` automatically).

## Drone

1-8 app-only "drone" segments per entry (`config_model.DroneConfig`/
`DroneSegmentConfig`), reusing the same pin-borrowing pattern as the
Sequencer. A drone **never sends a Note** — it's a value control whose CC
output is gated on/off by an enable button.

- **Segment shape**: one value control (encoder or pot, `control_type`
  chosen independently per segment — same "Value control" dropdown pattern
  as the Sequencer's per-step choice, not a device-wide setting), one
  enable button (`button_cc`+toggle), one shift button, one PWM meter (via
  `mirror_pin`), and two indicator lights (on-state, shift-held).
- **Gating** (`app/gated_segments.py`'s `GatedSegmentEngine`, shared with FX
  Rack): the enable button's own CC never reaches the DAW; while its
  segment is disabled, the control pin's CC is suppressed too; while
  enabled, it passes straight through untouched. No note/trigger logic at
  all — thinner than the Sequencer engine.
- **Lights need zero engine code**: both the on-light and shift-light are
  wired via `PinConfig.listen_pin` straight at the enable/shift pin —
  firmware's `applyListenLights()` already mirrors a toggle button's
  *logical* on/off state (not just raw press/release), so this works with
  no MIDI round-trip and no app polling.
- **Enable is hardware-only**: unlike the Sequencer's step-enabled state
  (which the app can also toggle via a synthetic CC + software checkbox),
  a drone segment's on/off state lives in the firmware's real button
  `toggleState`, which the app has no protocol command to set remotely —
  so the Drone tab shows live enabled state as a read-only indicator
  (`drone_state` push event), not a clickable checkbox.
- **No firmware/wire-protocol changes**: every mechanism above reuses
  existing roles/flags/fields verbatim.

## FX Rack

1-8 app-only FX segments per entry (`config_model.FxRackConfig`/
`FxSegmentConfig`). Each segment is always **two encoders** (never pots) +
one enable button gating both at once + one shared shift button + the same
on/shift lights as Drone — no PWM meter.

- Reuses `GatedSegmentEngine` directly (`app/fx_rack.py`) with two value
  pins per segment instead of Drone's one — the only difference in shape.
- Both encoders share the same shift button, each with its own shifted CC
  destination so they don't collide.

## Joystick

A single (non-repeatable) X/Y/rotation + button widget per entry
(`config_model.JoystickConfig`). Every pin is an ordinary existing role —
three `pot_cc` pins for the axes (rotation is just a third pot, wired to a
twist mechanism) and one `button_note`/`button_cc` pin for the click. No
new `PinRole`, no engine at all: all four pins flow through the normal
pin-role dispatch like any other input. The Joystick tab includes a small
XY-dot + Z-bar visualization fed by the existing `pin_activity` event
stream (the same one Table/Board's per-pin activity dots use).

## Feature toggles

Sequencer/Drone/FX Rack/Joystick each have their own `enabled` flag
(`SequencerConfig`/`DroneConfig`/`FxRackConfig`/`JoystickConfig`), surfaced
as a row of checkboxes in the entry header (`app.js`'s
`buildFeatureToggles()`) rather than buried inside each tab. The tab bar
itself is built from a small `TAB_DEFS` registry: Table/Board always show;
the other four only appear while their feature is enabled, and switching a
feature off while its tab is active falls back to Table.

## Multi-device support

- Each connected Mega gets its own `SerialLink` + `MidiBridge` session in the
  app, keyed by serial port.
- Each device gets its own **persistent device id**, stored in EEPROM byte 4
  of the header (0 = unassigned). The app reads it via `PING` right after
  connecting; if it's still `0` (never adopted before), the app silently
  assigns the next free id. Identity survives being plugged into a different
  USB port — config storage doesn't depend on which physical port a Mega is on.
- Each device gets its **own virtual MIDI port pair**, named from its device
  id once assigned (e.g. `Mad Midi 3 In` / `Mad Midi 3 Out`), or from its
  serial port path if the id hasn't been assigned yet.
- The sidebar lists every **adopted device** and every **draft** (a config not
  yet tied to hardware) as one flat list of entries, persisted across app
  restarts and while unplugged. Each entry has its own auto-saved JSON file
  under `app/devices/`, named by its id (`<device_id>.json` for a device,
  `draft-<hex>.json` for a draft) — there is no separate named-preset
  library. Editing an entry's pins auto-saves (debounced) regardless of
  whether its device is currently connected.
- A device entry's "Write to device" button recolors when its saved buffer
  diverges from a baseline snapshot of what's actually on the EEPROM — that
  baseline is captured by auto-reading the config right when the device is
  adopted or reconnected, and refreshed after every successful read/write.
  It's disabled outright while the device is offline.
- A draft has no connection of its own; instead it offers an "Apply to
  device" action that copies its pins onto a chosen device entry's buffer
  (which then goes out of sync until written), so the same draft can seed
  more than one board.
- The MIDI monitor follows the sidebar selection by default (only that
  device's traffic), with an "All devices" pseudo-entry at the top of the
  sidebar to see every connected device's traffic tagged by origin at once.

## Repo layout

```
mad-midi/
  PLAN.md
  firmware/                    PlatformIO project (board: megaatmega2560)
    platformio.ini
    src/
      main.cpp
      protocol.h / .cpp        SysEx trigger detection + config-mode framing
      pin_config.h / .cpp      PinConfig struct, EEPROM read/write/validate
      mode.h / .cpp            RUN/CONFIG state machine, LED, timeout
      midi_io.h / .cpp         MIDI byte encode/decode over Serial
      roles.h / .cpp           per-pin role handlers (buttons, lights, encoders, pots)
  app/                         Python app (run from source)
    main.py                    entrypoint only: creates the Api and the pywebview window
    api.py                     Api class exposed to the UI as window.pywebview.api (entries,
                               device adoption/reconnect, config read/write, flashing, auto-reconnect poller)
    device_session.py          DeviceSession (one per connected device: serial link + MIDI bridge +
                               sequencer/drone/fx_rack/value-mirror engines) plus the small helpers
                               only a session needs (virtual port naming, monitor decoding, pin matching)
    protocol.py                same wire-protocol constants as firmware (source of truth kept in sync manually)
    serial_link.py             pyserial connection, background reader, mode transitions
    config_model.py            PinConfig/Entry + per-feature config dataclasses, JSON <-> binary blob (de)serialization
    midi_bridge.py             python-rtmidi virtual port, decode/encode MIDI messages
    sequencer.py               app-only step sequencer engine, slaved to MIDI clock (see Sequencer above)
    value_mirror.py            generalized encoder/pot -> PWM VU-meter engine (see Value mirror above)
    gated_segments.py          shared enable-button-gates-value-pins engine behind Drone and FX Rack
    drone.py                   app-only drone engine (thin wrapper over gated_segments, see Drone above)
    fx_rack.py                 app-only FX rack engine (thin wrapper over gated_segments, see FX Rack above)
    flasher.py                 subprocess wrapper around bundled avrdude
    ui/
      index.html
      app.js
      style.css
    vendor/avrdude/darwin/     bundled avrdude binary + avrdude.conf
    devices/                   one auto-saved *.json per sidebar entry (device or draft)
    tests/                     plain assert-based checks, no test framework installed
                               (run with `python3 app/tests/test_<name>.py`)
  requirements.txt
```

## v1 scope

- Pin roles: buttons, lights (digital/PWM incl. VU meters), 2-pin encoders
  (no push-button, fixed 1:1 relative CC), pots/faders -> CC, shift buttons
  (pure modifier, gates specific encoders' shifted channel/CC — see below).
- Multiple Megas can be connected simultaneously, each with its own
  persistent device id, virtual MIDI port pair, and auto-saved config; the
  MIDI monitor can show one device's traffic or all devices' traffic tagged
  by device.
- Dual UI: table view and physical board view over one shared config model,
  with live per-pin state overlay while connected in run mode.
- App-only step sequencer per device entry, slaved to external MIDI clock,
  driving pins already assigned in that entry's pin map (see Sequencer
  above).
- Generalized value-mirror (VU meter) for any encoder/pot pin, plus Drone
  (1-8 gated value-control segments), FX Rack (1-8 dual-encoder segments),
  and Joystick (X/Y/rotation + button) — each independently toggleable per
  entry (see Value mirror / Drone / FX Rack / Joystick / Feature toggles
  above). None of these need firmware or wire-protocol changes.
- Unified MIDI monitor: decoded + raw bytes, tagged MIDI vs CTRL.
- A sidebar of devices (adopted, persistent, offline-editable) and drafts
  (configs not yet tied to hardware, applicable to any adopted device).
- Firmware flashing from the app via bundled avrdude (arm64 + Intel Mac,
  currently one x86_64 binary that runs on both via Rosetta 2).
- Config-format version byte checked before any EEPROM write is applied.
- macOS only. Run from source (`python app/main.py` in a venv) — no .app
  packaging yet.

Out of scope for v1 (explicitly deferred): encoder push-buttons, real DIN
MIDI hardware output, Windows/Linux support, distributable `.app` packaging,
app-configurable encoder acceleration curves (the shipped curve is a fixed
firmware constant — see Encoder acceleration above).

## Build / run

```
# firmware: build + flash (from app, or manually via PlatformIO while developing)
cd firmware && ~/.platformio/penv/bin/pio run

# app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app/main.py
```
