# Mad Midi

Map every pin of an Arduino Mega 2560 to MIDI in/out from a local desktop
app — buttons, lights/VU meters, encoders (with shift support), and pots —
then flash and configure the board without touching the Arduino IDE.

See [PLAN.md](PLAN.md) for the full architecture and wire protocol.

## Requirements

- macOS (Apple Silicon or Intel) — this is currently macOS-only.
- Python 3.9+ (the system `python3` on macOS is fine).
- An Arduino Mega 2560, connected via USB.
- Xcode Command Line Tools (`xcode-select --install`) if you don't already
  have them — needed for Rosetta 2 to be available, since the bundled
  `avrdude` binary is x86_64 and runs under Rosetta on Apple Silicon.

You do **not** need the Arduino IDE or PlatformIO installed — `avrdude` and
a precompiled firmware `.hex` are bundled under `app/vendor/`.

## Setup

```bash
git clone <this repo>
cd mad-midi

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running the app

```bash
source .venv/bin/activate
python app/main.py
```

This opens the app window. Every time you come back to work on this project,
just re-activate the venv and run that same command.

## First-time device setup

1. Plug the Mega into USB. In the app's top bar, click the port dropdown
   (⟳ refreshes the list) and pick the Mega's port — on macOS it looks like
   `/dev/cu.usbmodem*`.
2. Click **Connect**. A chip for the device appears in the device bar below
   the top bar, showing "Unassigned" (no device ID yet) and its current
   mode.
3. Click **Flash firmware** with that device selected. This writes the
   bundled firmware onto the Mega — takes a few seconds. You only need to
   redo this if the firmware itself changes (not for every pin-config
   change).
4. If you plan to run **more than one Mega** at once, give each a
   persistent ID: type a number into the small field on its device chip and
   click **Assign ID**. This is stored on the device itself, so it's
   remembered even if you plug it into a different USB port next time. A
   single device can skip this — ID `0`/unassigned works fine.
5. Build your pin mapping in the **Table** or **Board** view (they show the
   same data two ways — pick whichever matches how you think about your
   wiring). Click a pin to open its editor on the right and choose a role:
   - **Button → Note / CC** — wire the button between the pin and GND
     (pins use `INPUT_PULLUP`, so no external resistor needed).
   - **Light (on/off) / Light-VU (PWM)** — wire an LED (with resistor) or a
     VU meter needle to the pin; PWM roles only work on PWM-capable pins
     (2–13, 44–46), the UI won't offer PWM on other pins.
   - **Encoder (2-pin)** — takes this pin *and the next pin* as A/B
     quadrature; wire both between the encoder's outputs and GND.
   - **Shift button** — a modifier button (also wired to GND like a normal
     button) that sends no MIDI itself. Assign it to an encoder from that
     encoder's editor panel to give the encoder an alternate channel/CC
     while the shift button is held.
   - **Pot/Fader → CC** — analog pins only (A0–A15).
6. Click **Write to device** to push the config to the selected device's
   EEPROM. Click **Read from device** any time to pull back whatever is
   currently stored on it.
7. Save your work as a named preset (top bar: type a name, **Save**) so you
   can reload it later or push the same mapping to another device.

## Using it

Once configured, the Mega runs standalone in **RUN mode** (default on
boot — its status LED, pin 13, is solid on). While the app is running, each
connected device shows up in your DAW as its own virtual MIDI port pair
(`Mad Midi <id> In` / `Mad Midi <id> Out`), so route them like any other
MIDI device.

Reconfiguring later: connecting from the app automatically checks in with
each device; hitting **Read from device** / **Write to device** briefly
switches it into **CONFIG mode** (LED blinks) and back — you don't need to
do anything else for that. If a device sits in config mode with the app not
talking to it, it auto-returns to RUN mode after 15 seconds.

The **MIDI Monitor** at the bottom shows every message in and out, across
all connected devices, decoded and in raw hex — useful for checking a pin
is actually sending what you expect before you go hunting for a wiring
mistake.

## Sequencer

Each device entry also has a **Sequencer** tab: an app-side step sequencer
that drives pins on that device, separate from the pin-role mapping in the
Table/Board views.

- It's slaved entirely to an external MIDI Clock — there's no internal
  clock, so it stays idle until your DAW sends Start/Stop/Continue and
  clock ticks. Set the step count, note subdivision (8th/16th/32nd/16th
  triplets), and MIDI channel for the whole sequence.
- Each step gets its note plus up to five pin assignments, picked from your
  device's already-wired pins: a **button** to enable/disable that step
  (also doubles as its own mute — no separate control), an **encoder** to
  adjust it live, a **shift** button for the encoder's alternate CC, a
  **PWM** pin that mirrors the encoder's current value as a brightness/VU
  meter, and a **step light** that flashes while the step plays.
- While a device is connected and the DAW's transport is running, the
  step/enabled state streams back live into the UI so you can see the
  sequence advance.
- The on/off pattern and playback position are runtime-only (not saved to
  a preset or the device's EEPROM) — only the step count, subdivision,
  channel, notes, and pin assignments are persisted.

## Testing pins

Each device entry has a **Test** tab for checking wiring without any MIDI
setup:

- **H** / **L** drive a pin high or low (a LED or VU meter check).
- **In** turns a pin into a pull-up input, for checking a button wired to a
  pin that doesn't have a button role yet.
- Click a lit button again to release the pin. **All low** and **Release
  all** act on every pin at once.
- Every pin shows its live level. Pins with a button role say "pressed"
  while held. Analog pins (A0–A15) also show their 0–1023 reading and a
  bar, so you can sweep a pot or fader and watch it move.

While the tab is open the device is held in config mode, so MIDI is paused
and pin roles are ignored. Leaving the tab (or reading, writing or flashing)
returns every tested pin to its configured role. If the app quits mid-test,
the device times out after 15 seconds and restores the pins on its own.
Needs firmware version 3 or later, so flash once after updating.

## Rebuilding the firmware (only needed if you edit `firmware/`)

Requires [PlatformIO](https://platformio.org/) (its CLI, not just the
Arduino IDE).

```bash
cd firmware
~/.platformio/penv/bin/pio run   # or just `pio run` if pio is on your PATH
cp .pio/build/megaatmega2560/firmware.hex ../app/vendor/firmware.hex
```

Then **Flash firmware** from the app as usual to push the rebuilt version.

## Troubleshooting

- **Port doesn't show up**: click ⟳ next to the port dropdown after
  plugging in; macOS sometimes needs a moment to enumerate a new USB-serial
  device.
- **Flash fails with a permissions/lock error**: make sure nothing else
  (Arduino IDE Serial Monitor, `screen`, another instance of this app) has
  the port open.
- **Flash fails outright**: unplug/replug the Mega and try again — the
  bootloader is timing-sensitive; `avrdude` occasionally needs a fresh USB
  enumeration to sync up.
- **No sound / MIDI not appearing in your DAW**: check the MIDI Monitor
  first — if messages show up there but not in your DAW, the issue is in
  the DAW's MIDI input routing/port selection, not this app.
