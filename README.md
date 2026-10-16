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

1. Plug the Mega into USB and click **+ Device** at the bottom of the
   sidebar. A list of serial ports opens; on macOS the Mega looks like
   `/dev/cu.usbmodem*`.
2. If the board doesn't have the Mad Midi firmware yet (a new Mega, or one
   running another sketch), click **Flash** next to its port. This writes
   the bundled firmware and takes a few seconds.
3. Click the port name to adopt the board. The app gives it the next free
   device ID and stores that ID on the board, so it's recognized on any
   USB port later. It then appears in the sidebar as "Device `<id>`"
   (use ✎ to rename it). Each device needs an ID, and adopting assigns one
   automatically; there's nothing to set by hand. After that, a known
   device reconnects by itself whenever it's plugged in while the app is
   running.

   Later firmware updates don't need the port list: select the device and
   click **Flash firmware** in the top bar.
4. Build your pin mapping in the **Table** or **Board** view (they show the
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
5. Click **Write to device** to push the config to the selected device's
   EEPROM (the button is highlighted while the app's copy differs from
   the device's). Click **Read from device** any time to pull back whatever
   is currently stored on it.

Edits save automatically, including edits made while a device is offline.
To work on a mapping without a board, click **+ Draft** in the sidebar;
when it's ready, pick a device under **Apply to:** in the top bar and
click **Apply**.

## Import / export

**Export…** in the sidebar saves the selected device or draft to a JSON
file. With **All devices** selected it saves every device and draft into
one file. Use it for backups or to move configs to another Mac.

**Import…** reads one or more of those files (a file copied straight out
of the `devices/` folder works too):

- **Devices come back as devices**, with the same device ID. An import
  window lists each device with a port dropdown: pick the Mega's port and
  click **Import**. The imported config is written onto that board. If the
  board isn't that device yet (a different or fresh Mega), it's flashed
  first and takes over the device's ID, which overwrites what was on it.
  Choose **Don't connect now** to link it later with **Connect** in the
  sidebar.
- If a device with that ID already exists, the app asks first: replace
  its saved config, or import it as a draft instead.
- Drafts always come in as new drafts.

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

**Staying in sync with the hardware.** The Mega restarts every time the
app connects to it. Once connected (and after **Write to device** or
leaving the Test tab), the app asks the board for the current position of
every CC button, switch and pot. The board sends those as normal MIDI, so
the Sequencer/Drone/FX Rack tabs and your DAW both pick up the real
positions, and mapped parameters in your DAW jump to match the hardware.
Toggle buttons start off after the restart, and the DAW is told so.
**Write to device** no longer resets toggle buttons. Note buttons are never
replayed. This needs firmware version 4 or later: select each device and
click **Flash firmware** once after updating the app.

The **MIDI Monitor** at the bottom shows every message in and out, across
all connected devices, decoded and in raw hex — useful for checking a pin
is actually sending what you expect before you go hunting for a wiring
mistake.

## Optional features

The top bar has a checkbox for each of **Sequencer**, **Drone**, **FX Rack**
and **Joystick**. Ticking one adds its tab next to Table/Board; unticking
hides it again. Each is set per device or draft.

All four tabs work the same way: you pick pins from dropdowns that only
list free pins, and the app sets those pins' roles, channels and CC/note
numbers for you. You can still fine-tune them in Table/Board afterwards.
Because pin roles live on the device, click **Write to device** after
assigning pins. The feature settings themselves (segment layout, channel)
are saved in the app, not on the device.

## Sequencer

The **Sequencer** tab is an app-side step sequencer that drives pins on
the device.

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
- The on/off pattern and playback position aren't saved anywhere and reset
  when the device reconnects. Only the step count, subdivision, channel,
  notes and pin assignments are saved.

## Drone

The **Drone** tab sets up to 8 drone segments that share one MIDI
channel. Each segment is a group of pins around one control:

- **Encoder** or **Pot**: the value control, chosen per segment. It sends
  CC `8 + n` (segment `n` counts from 0), or CC `16 + n` while its shift
  button is held.
- **Enable**: a toggle button that sends its own on/off CC `0 + n` to
  the DAW (127 = on, 0 = off). It doesn't block anything: the control's
  CC always goes through, so map the enable CC in your DAW to whatever
  should switch on and off.
- **Shift**: a button for the control's alternate CC.
- **PWM (VU meter)**: a PWM pin that shows the control's current value.
- **On light** / **Shift light**: follow the enable and shift buttons
  directly on the device, with no MIDI round trip.

A drone never sends notes. While the device is connected, the tab shows
which segments are on (it starts at off when the device connects, until
the first enable press).

## FX Rack

The **FX Rack** tab works like Drone, but each segment (up to 8) has
**two encoders** and one **Enable** button that sends its own on/off CC
`0 + n`. The encoders always send, whether the segment is on or off.
Both encoders share one **Shift** button, and the segment has the same
**On light** / **Shift light**, but no VU meter pin. Encoder 1 sends
CC `8 + n` (shifted: `16 + n`) and Encoder 2 sends CC `24 + n` (shifted:
`32 + n`). Pots aren't supported here, only encoders.

## Joystick

The **Joystick** tab maps a single joystick: **X**, **Y** and
**Rotation** on analog pins (A0–A15) and its **Button** on a digital pin.
The axes send CC 20, 21 and 22 on channel 1, and the button sends note 60
(C4). Change these in Table/Board if you need different numbers. Each axis
has an **Invert direction** box for a stick wired backwards (write to the
device after changing it). A live dot shows the X/Y position and a
centre-zero bar shows rotation.

Unlike the other tabs, the joystick has no extra logic behind it. Its pins
are ordinary pot and button pins, and this tab is a shortcut for setting
them up together.

## Testing pins

Every device (not drafts) always has a **Test** tab for checking wiring
without any MIDI setup:

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

## Building the app (Mad Midi.app)

To get a standalone app that runs without a venv or Python setup:

```bash
packaging/build_macos.sh
```

This produces `dist/Mad Midi.app` plus a zip of it under `dist/` to share.
It uses the project's `.venv` and installs PyInstaller into it
(`requirements-build.txt`).

- **Apple Silicon (arm64) only for now.** The script refuses
  `ARCH=x86_64` / `ARCH=universal2` until Intel support is added (that
  needs x86_64 builds of `python-rtmidi` and `pyobjc`, either built on an
  Intel Mac or by setting up a universal2 environment).
- The app is ad-hoc signed, not notarized, so macOS blocks it the first
  time with only **Move to Bin** / **Cancel**. Click **Cancel**, then go to
  **System Settings → Privacy & Security** and click **Open Anyway** next
  to the Mad Midi message. Or run
  `xattr -dr com.apple.quarantine "Mad Midi.app"` and open it normally.
- Flashing still needs Rosetta 2 (see Requirements), since the bundled
  `avrdude` is x86_64.
- The built app keeps its device configs in
  `~/Library/Application Support/Mad Midi/devices/` rather than
  `app/devices/`, and writes its log to `~/Library/Logs/Mad Midi/mad-midi.log`.
  To carry over configs from running from source, copy them across:
  `mkdir -p ~/Library/Application\ Support/Mad\ Midi && cp -R app/devices ~/Library/Application\ Support/Mad\ Midi/`

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

- **Port doesn't show up**: close the port list and click **+ Device**
  again to rescan; macOS sometimes needs a moment to enumerate a new
  USB-serial device. Ports that already belong to a connected device aren't
  listed.
- **Port doesn't adopt** ("didn't respond as a Mad Midi device"): the board
  is running a different sketch or none at all. Click **Flash** next to the
  port, then click the port again.
- **Flash fails with a permissions/lock error**: make sure nothing else
  (Arduino IDE Serial Monitor, `screen`, another instance of this app) has
  the port open.
- **Flash fails outright**: unplug/replug the Mega and try again — the
  bootloader is timing-sensitive; `avrdude` occasionally needs a fresh USB
  enumeration to sync up.
- **No sound / MIDI not appearing in your DAW**: check the MIDI Monitor
  first — if messages show up there but not in your DAW, the issue is in
  the DAW's MIDI input routing/port selection, not this app.
