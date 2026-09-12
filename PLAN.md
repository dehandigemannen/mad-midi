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

### EEPROM blob layout (566 bytes of 4096 available)

```
Header (6 bytes):
  [0-1] magic = 'M','M'
  [2]   config format version (currently 3)
  [3]   pin count (70)
  [4]   device id (0 = unassigned; set once via SET_DEVICE_ID, persists across reconnects/ports)
  [5]   checksum (sum of all record bytes, mod 256 — does NOT cover the device id byte,
                  so SET_DEVICE_ID can update it independently without a full config rewrite)

Records: 70 x 8 bytes, indexed by Arduino Mega pin number directly
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
    [7] flags   (bit0 invert, bit1 toggle-mode, bit2 match-CC-not-Note
                 [LIGHT_DIGITAL/LIGHT_PWM only, see below]; rest reserved)
```

### PinRole enum (v1)

| Value | Role | Direction | Behavior |
|---|---|---|---|
| 0 | UNUSED | — | pin ignored |
| 1 | BUTTON_NOTE | digital-in -> MIDI out | press = Note On, release = Note Off |
| 2 | BUTTON_CC | digital-in -> MIDI out | momentary or toggle CC (flags bit1) |
| 3 | LIGHT_DIGITAL | MIDI in -> digital out | on/off from an incoming Note or CC (which one is chosen by flags bit2, `FLAG_MATCH_CC`) |
| 4 | LIGHT_PWM | MIDI in -> PWM out | `analogWrite(map(value, inMin,inMax, outMin,outMax))` — also used for VU meters (single-pin analog needle); Note vs CC selected the same way as LIGHT_DIGITAL |
| 5 | ENCODER_A | digital-in pair -> MIDI out | quadrature phase A; paired with the next pin (ENCODER_B). Fixed 1:1, relative CC: value `1` = +1 step (CW), value `127` = -1 step (CCW). Fields are repurposed for shift support (see below) |
| 6 | ENCODER_B | — | marks the pin as consumed by the preceding ENCODER_A |
| 7 | POT_CC | analog-in -> MIDI out | raw ADC (0-1023, stored as inMin/inMax >>2) scaled to CC 0-127 |
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
`(outMin, outMax)` instead of `(channel, number)` — same 1/127 encoding
either way.

## Multi-device support

- Each connected Mega gets its own `SerialLink` + `MidiBridge` session in the
  app, keyed by serial port.
- Each device gets its own **persistent device id**, stored in EEPROM byte 4
  of the header (0 = unassigned). The app reads it via `PING` right after
  connecting, so identity survives being plugged into a different USB port —
  presets/port-naming don't depend on which physical port a Mega is on.
- Each device gets its **own virtual MIDI port pair**, named from its device
  id once assigned (e.g. `Mad Midi 3 In` / `Mad Midi 3 Out`), or from its
  serial port path if the id hasn't been assigned yet.
- The MIDI monitor is global across all connected devices (each row tagged
  with the originating device), since debugging a multi-device rig benefits
  from seeing all traffic at once.

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
    main.py                    entrypoint, pywebview window + JS bridge API
    protocol.py                same wire-protocol constants as firmware (source of truth kept in sync manually)
    serial_link.py             pyserial connection, background reader, mode transitions
    config_model.py            PinConfig/Preset dataclasses, JSON <-> binary blob (de)serialization
    midi_bridge.py             python-rtmidi virtual port, decode/encode MIDI messages
    flasher.py                 subprocess wrapper around bundled avrdude
    ui/
      index.html
      app.js
      style.css
    vendor/avrdude/darwin/     bundled avrdude binary + avrdude.conf
    presets/                   saved *.json preset files
  requirements.txt
```

## v1 scope

- Pin roles: buttons, lights (digital/PWM incl. VU meters), 2-pin encoders
  (no push-button, fixed 1:1 relative CC), pots/faders -> CC, shift buttons
  (pure modifier, gates specific encoders' shifted channel/CC — see below).
- Multiple Megas can be connected simultaneously, each with its own
  persistent device id, virtual MIDI port pair, and preset read/write; the
  MIDI monitor shows all devices' traffic tagged by device.
- Dual UI: table view and physical board view over one shared config model,
  with live per-pin state overlay while connected in run mode.
- Unified MIDI monitor: decoded + raw bytes, tagged MIDI vs CTRL.
- Multiple named, saveable/loadable JSON presets.
- Firmware flashing from the app via bundled avrdude (arm64 + Intel Mac,
  currently one x86_64 binary that runs on both via Rosetta 2).
- Config-format version byte checked before any EEPROM write is applied.
- macOS only. Run from source (`python app/main.py` in a venv) — no .app
  packaging yet.

Out of scope for v1 (explicitly deferred): encoder push-buttons, encoder
acceleration, real DIN MIDI hardware output, Windows/Linux support,
distributable `.app` packaging, encoder velocity/curve options beyond linear.

## Build / run

```
# firmware: build + flash (from app, or manually via PlatformIO while developing)
cd firmware && ~/.platformio/penv/bin/pio run

# app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app/main.py
```
