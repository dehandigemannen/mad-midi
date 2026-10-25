"""App-only step sequencer engine: one instance per DeviceSession, built from
an Entry's SequencerConfig (config_model.py) plus its current PinConfig list.

Slaved entirely to external MIDI Clock/Start/Stop/Continue — there is no
internal free-running clock. Step advancement is pure MIDI-clock-tick
counting (24 ticks per quarter note); wall-clock time is only used to
estimate the current tick interval, which in turn sizes each step's gate
length. The on/off pattern and playback position are in-memory only and are
not persisted — see config_model.SequencerConfig's docstring.

Drives its own step lights and notes by reusing the app's existing MIDI
paths verbatim: a light pin is flashed with the same kind of synthetic
CC/Note the firmware already treats as "light_digital"/"light_pwm" input
(normally sent by a DAW), and a triggered step's note is forwarded to the
DAW exactly like any other device-originated MIDI message. Each step's
enabled_light pin is driven the same way, but tracks the step's on/off
state rather than flashing on trigger.

A step's PWM value-meter (mirroring its value control's live CC output) is
no longer this engine's concern -- see value_mirror.ValueMirrorEngine, which
generalizes that trick to any encoder_a/pot_cc pin via PinConfig.mirror_pin,
not just a sequencer step's.
"""

import threading
import time
from typing import Callable, List, Optional, Tuple

import config_model

TICKS_PER_QUARTER = 24  # MIDI clock standard: 24 clock messages per quarter note
SUBDIVISION_TICKS = {
    "8th": TICKS_PER_QUARTER // 2,          # 12
    "16th": TICKS_PER_QUARTER // 4,         # 6
    "32nd": TICKS_PER_QUARTER // 8,         # 3
    "16th_triplet": TICKS_PER_QUARTER // 6,  # 4
}
DEFAULT_TICKS_PER_STEP = SUBDIVISION_TICKS["16th"]
GATE_FRACTION = 0.5  # Note On lasts this fraction of one step's duration
DEFAULT_VELOCITY = 100
FALLBACK_TICK_INTERVAL_S = 60.0 / 120 / TICKS_PER_QUARTER  # seed at ~120bpm until clock arrives
# Gaps longer than this (~20bpm) aren't a tempo, they're the DAW having
# paused its clock while stopped. Averaging one in would stretch the next
# steps' gates to seconds, leaving their lights and notes hanging.
MAX_TICK_INTERVAL_S = 60.0 / 20 / TICKS_PER_QUARTER

SendBytes = Callable[[bytes], None]
StateCallback = Callable[[dict], None]


class _ResolvedStep:
    """A step's config plus its pins' (channel, number) resolved up front so
    the realtime-message hot path never has to scan the pin list."""

    def __init__(self, step: config_model.SequencerStepConfig, pins: List[config_model.PinConfig]):
        self.note = step.note
        self.button = _resolve(pins, step.button_pin)
        self.step_light = _resolve(pins, step.step_light_pin)
        self.enabled_light = _resolve(pins, step.enabled_light_pin)


def _resolve(pins: List[config_model.PinConfig], pin_index: Optional[int]) -> Optional[Tuple[int, int]]:
    if pin_index is None or pin_index < 0 or pin_index >= len(pins):
        return None
    p = pins[pin_index]
    return (p.channel, p.number)


class SequencerEngine:
    def __init__(
        self,
        config: config_model.SequencerConfig,
        pins: List[config_model.PinConfig],
        send_to_daw: SendBytes,
        send_to_device: SendBytes,
        on_state_change: StateCallback,
    ):
        self.channel = config.channel & 0x0F
        self.ticks_per_step = SUBDIVISION_TICKS.get(config.subdivision, DEFAULT_TICKS_PER_STEP)
        self._steps = [_ResolvedStep(s, pins) for s in config.steps]
        self._send_to_daw = send_to_daw
        self._send_to_device = send_to_device
        self._on_state_change = on_state_change

        self.running = False
        self.current_step = 0
        self.step_enabled = [False] * len(self._steps)

        self._tick_count = 0
        self._last_tick_time: Optional[float] = None
        self._avg_tick_interval = FALLBACK_TICK_INTERVAL_S
        self._off_timers: List[Optional[threading.Timer]] = [None] * len(self._steps)
        self._lock = threading.Lock()

    def adopt_runtime_state(self, old: "SequencerEngine") -> None:
        """Carries transport position, tempo estimate and the on/off pattern
        over from the engine this one replaces. DeviceSession rebuilds the
        engine on every config edit; without this, editing anything while
        the DAW plays would drop back to stopped/all-off until the next
        Start."""
        with old._lock:
            running = old.running
            current_step = old.current_step
            tick_count = old._tick_count
            last_tick_time = old._last_tick_time
            avg_tick_interval = old._avg_tick_interval
            old_enabled = list(old.step_enabled)
        with self._lock:
            self.running = running
            self.current_step = current_step % len(self._steps) if self._steps else 0
            self._tick_count = min(tick_count, self.ticks_per_step - 1)
            self._last_tick_time = last_tick_time
            self._avg_tick_interval = avg_tick_interval
            for i in range(min(len(old_enabled), len(self.step_enabled))):
                self.step_enabled[i] = old_enabled[i]
        # re-light in case a step's enabled_light pin changed in the edit
        for step, enabled in zip(self._steps, self.step_enabled):
            if enabled:
                self._send_cc(step.enabled_light, 127)
        self._emit_state()

    # ---- transport -----------------------------------------------------

    def handle_realtime(self, kind: str) -> None:
        """kind is one of "start", "stop", "continue", "clock"."""
        if kind == "stop":
            self._all_step_lights_off()
        with self._lock:
            if kind == "start":
                self.current_step = 0
                self._tick_count = 0
                self.running = True
            elif kind == "continue":
                self.running = True
            elif kind == "stop":
                self.running = False
            elif kind == "clock":
                self._on_clock_tick()
            else:
                return
        self._emit_state()

    def _on_clock_tick(self) -> None:
        """Caller already holds self._lock."""
        now = time.monotonic()
        if self._last_tick_time is not None:
            interval = now - self._last_tick_time
            if 0 < interval <= MAX_TICK_INTERVAL_S:
                self._avg_tick_interval = 0.8 * self._avg_tick_interval + 0.2 * interval
        self._last_tick_time = now

        if not self.running or not self._steps:
            return
        self._tick_count += 1
        if self._tick_count < self.ticks_per_step:
            return
        self._tick_count = 0
        triggered = self.current_step
        self.current_step = (self.current_step + 1) % len(self._steps)
        self._trigger_step(triggered)

    # ---- step playback ---------------------------------------------------

    def _trigger_step(self, index: int) -> None:
        step = self._steps[index]
        # Re-assert the enabled light on every pass. A CC can get lost on the
        # serial line to the Mega; without this a light that missed its
        # update stays wrong until the switch is flipped again.
        self._send_cc(step.enabled_light, 127 if self.step_enabled[index] else 0)
        if not self.step_enabled[index]:
            # Nothing to play, but still send an off: a light left on by an
            # earlier trigger (or a missed off) gets cleared every pass.
            self._send_cc(step.step_light, 0)
            return
        note_on = bytes([0x90 | self.channel, step.note & 0x7F, DEFAULT_VELOCITY])
        note_off = bytes([0x80 | self.channel, step.note & 0x7F, 0])
        self._send_to_daw(note_on)
        self._send_cc(step.step_light, 127)

        existing = self._off_timers[index]
        if existing is not None:
            existing.cancel()
        gate_seconds = self._avg_tick_interval * self.ticks_per_step * GATE_FRACTION
        timer = threading.Timer(gate_seconds, self._end_step, args=(index, note_off, step))
        timer.daemon = True
        self._off_timers[index] = timer
        timer.start()

    def _end_step(self, index: int, note_off: bytes, step: _ResolvedStep) -> None:
        self._send_to_daw(note_off)
        self._send_cc(step.step_light, 0)

    def _all_step_lights_off(self) -> None:
        # Pending gate timers still send their own note-offs; this only makes
        # sure no step light outlives the transport.
        for step in self._steps:
            self._send_cc(step.step_light, 0)

    def _send_cc(self, target: Optional[Tuple[int, int]], value: int) -> None:
        if target is None:
            return
        channel, number = target
        status = 0xB0 | (channel & 0x0F)
        self._send_to_device(bytes([status, number & 0x7F, value & 0x7F]))

    # ---- incoming pin messages (from the device) -----------------------

    def handle_incoming_pin_message(self, channel: int, number: int, value: int) -> bool:
        """Called for every MIDI message arriving from the device. Returns
        True if it was a sequencer step's button (consumed — caller must not
        forward it to the DAW), False otherwise."""
        for index, step in enumerate(self._steps):
            if step.button == (channel, number):
                # The button is a latching switch wired for level (not
                # toggle) mode: the incoming value already *is* the switch's
                # current position, so set it directly rather than inverting
                # — inverting would desync from the switch after a missed
                # edge (e.g. a session reconnect while it sits mid-position).
                self.set_step_enabled(index, value != 0)
                return True
        return False

    def set_step_enabled(self, index: int, enabled: bool) -> None:
        if index < 0 or index >= len(self._steps):
            return
        with self._lock:
            self.step_enabled[index] = enabled
        if not enabled:
            # Don't leave a step light lit until its gate timer ends
            self._send_cc(self._steps[index].step_light, 0)
        self._send_cc(self._steps[index].enabled_light, 127 if enabled else 0)
        self._emit_state()

    # ---- state ------------------------------------------------------------

    def _emit_state(self) -> None:
        self._on_state_change({
            "running": self.running,
            "current_step": self.current_step,
            "step_enabled": list(self.step_enabled),
            # None until the first MIDI Clock tick arrives -- lets the UI tell
            # "DAW isn't sending clock" apart from "clock, but stopped"
            "bpm": (
                round(60.0 / (self._avg_tick_interval * TICKS_PER_QUARTER))
                if self._last_tick_time is not None and self._avg_tick_interval > 0
                else None
            ),
        })

    def close(self) -> None:
        # Send any still-pending note-offs now rather than dropping them --
        # otherwise a rebuild mid-gate leaves the note hanging in the DAW.
        for timer in self._off_timers:
            if timer is not None and not timer.finished.is_set():
                timer.cancel()
                self._end_step(*timer.args)
