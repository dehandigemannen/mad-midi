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
"""

import threading
import time
from typing import Callable, List, Optional, Tuple

import config_model
import protocol

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
PWM_METER_START = 64  # start each meter at mid-brightness so both turn directions are visible right away
PWM_METER_STEP = 3  # brightness change per detent -- ~a third of a full turn (42 clicks) sweeps 0-127

SendBytes = Callable[[bytes], None]
StateCallback = Callable[[dict], None]


class _ResolvedStep:
    """A step's config plus its pins' (channel, number) resolved up front so
    the realtime-message hot path never has to scan the pin list."""

    def __init__(self, step: config_model.SequencerStepConfig, pins: List[config_model.PinConfig]):
        self.note = step.note
        self.button = _resolve(pins, step.button_pin)
        self.step_light = _resolve(pins, step.step_light_pin)
        self.pwm = _resolve(pins, step.pwm_pin)
        self.enabled_light = _resolve(pins, step.enabled_light_pin)

        # The PWM meter mirrors whichever CC the step's value control (an
        # encoder or, if step_control is "pot", a pot) is currently sending —
        # plain or, if a shift group is configured, shifted too. Pots use
        # dedicated shift_pin/shift_channel/shift_number fields instead of
        # encoder_a's in_min/out_min/out_max repurposing, since pots need
        # in_min/in_max/out_min/out_max for their own ADC->CC scaling (see
        # pin_config.h).
        self.encoder_plain: Optional[Tuple[int, int]] = None
        self.encoder_shifted: Optional[Tuple[int, int]] = None
        self.is_pot = False
        if step.encoder_pin is not None and 0 <= step.encoder_pin < len(pins):
            enc = pins[step.encoder_pin]
            self.encoder_plain = (enc.channel, enc.number)
            self.is_pot = enc.role == "pot_cc"
            if self.is_pot:
                if enc.shift_pin != protocol.NO_SHIFT_PIN:
                    self.encoder_shifted = (enc.shift_channel, enc.shift_number)
            elif enc.in_min != protocol.NO_SHIFT_PIN:
                self.encoder_shifted = (enc.out_min, enc.out_max)


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

        # The encoder only ever reports a relative click (CC 1 = one step
        # CW, 127 = one step CCW) -- there's no such thing as "its current
        # absolute value" to read back, from the device or from a DAW (a
        # generic MIDI-mapped CC in a DAW is one-way; getting real value
        # feedback would mean writing a DAW-specific control-surface
        # integration). So the pwm meter's brightness is our own running
        # position, accumulated click-by-click here rather than mirroring
        # the raw relative value directly (which would barely light up on
        # a CW click and nearly max out on a single CCW one).
        self._pwm_value = [PWM_METER_START] * len(self._steps)

    # ---- transport -----------------------------------------------------

    def handle_realtime(self, kind: str) -> None:
        """kind is one of "start", "stop", "continue", "clock"."""
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
            if interval > 0:
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
        if not self.step_enabled[index]:
            return
        step = self._steps[index]
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
        forward it to the DAW), False otherwise (including an encoder match,
        which still needs to reach the DAW — it's only observed here to
        mirror its value onto the step's PWM meter)."""
        for index, step in enumerate(self._steps):
            if step.button == (channel, number):
                # The button is a latching switch wired for level (not
                # toggle) mode: the incoming value already *is* the switch's
                # current position, so set it directly rather than inverting
                # — inverting would desync from the switch after a missed
                # edge (e.g. a session reconnect while it sits mid-position).
                self.set_step_enabled(index, value != 0)
                return True
            if step.pwm is not None and (step.encoder_plain == (channel, number) or step.encoder_shifted == (channel, number)):
                if step.is_pot:
                    # A pot reports its actual absolute position directly --
                    # just mirror it, no accumulation needed.
                    self._pwm_value[index] = value
                else:
                    # value is a relative click -- 1..63 = CW, 65..127 = CCW
                    # (two's-complement magnitude; see ENCODER_A/
                    # FLAG_ACCEL_ENABLE in firmware/src/roles.cpp), not an
                    # absolute position, so accumulate it into one here
                    # instead of forwarding it as-is. The magnitude scales
                    # the step so a fast (accelerated) turn moves the meter
                    # further, same as it moves the real mapped parameter.
                    if value <= 63:
                        delta = PWM_METER_STEP * value
                    else:
                        delta = -PWM_METER_STEP * (128 - value)
                    self._pwm_value[index] = max(0, min(127, self._pwm_value[index] + delta))
                self._send_cc(step.pwm, self._pwm_value[index])
        return False

    def set_step_enabled(self, index: int, enabled: bool) -> None:
        if index < 0 or index >= len(self._steps):
            return
        with self._lock:
            self.step_enabled[index] = enabled
        self._send_cc(self._steps[index].enabled_light, 127 if enabled else 0)
        self._emit_state()

    # ---- state ------------------------------------------------------------

    def _emit_state(self) -> None:
        self._on_state_change({
            "running": self.running,
            "current_step": self.current_step,
            "step_enabled": list(self.step_enabled),
        })

    def close(self) -> None:
        for timer in self._off_timers:
            if timer is not None:
                timer.cancel()
