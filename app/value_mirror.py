"""Generalized "encoder/pot value mirrored onto a PWM pin as a VU meter"
engine. Originally lived only inside sequencer.SequencerEngine, tied to a
step's own pwm_pin; extracted here so ANY encoder_a/pot_cc pin can drive a
light_pwm pin's brightness to track its own live value -- whether that pin
belongs to a plain Table/Board mapping, a Sequencer step, a Drone segment, or
nothing composite at all. See PinConfig.mirror_pin (config_model.py).

The firmware has no idea this is happening: from its side, this is just an
ordinary CC arriving that happens to match a light_pwm+match_cc pin's
(channel, number) -- exactly what a DAW's own MIDI feedback would look like.
"""

import threading
from typing import Callable, Iterable, List, Optional, Tuple

import config_model
import protocol

PWM_METER_START = 64  # start each meter at mid-brightness so both turn directions are visible right away
PWM_METER_STEP = 3  # brightness change per detent -- ~a third of a full turn (42 clicks) sweeps 0-127

# Spring meters (Drone encoders): the meter rests at PWM_METER_START and
# shows how hard the encoder is being turned rather than an accumulated
# position -- each click kicks it up (CW) or down (CCW) by SPRING_PUSH per
# step, and between clicks it eases back to the middle like an elastic band,
# losing (1 - SPRING_DECAY) of its distance from the middle every
# SPRING_TICK_S (~0.5s to settle). Turning faster outruns the pull back, so
# the deflection tracks turn speed.
SPRING_PUSH = 8
SPRING_TICK_S = 0.025
SPRING_DECAY = 0.85

SendBytes = Callable[[bytes], None]


def spring_step(value: int) -> int:
    """One SPRING_TICK_S of a spring meter easing back toward the middle:
    keeps SPRING_DECAY of its distance, but always moves at least 1 so it
    actually lands on PWM_METER_START instead of creeping forever."""
    offset = value - PWM_METER_START
    eased = int(offset * SPRING_DECAY)
    if eased == offset:
        eased -= 1 if offset > 0 else -1
    return PWM_METER_START + eased


def _resolve(pins: List[config_model.PinConfig], pin_index: Optional[int]) -> Optional[Tuple[int, int]]:
    if pin_index is None or pin_index < 0 or pin_index >= len(pins):
        return None
    p = pins[pin_index]
    return (p.channel, p.number)


class _MirrorTarget:
    """One encoder_a/pot_cc pin with mirror_pin set, resolved up front so the
    realtime-message hot path never has to scan the pin list."""

    def __init__(self, p: config_model.PinConfig, pins: List[config_model.PinConfig]):
        self.plain: Tuple[int, int] = (p.channel, p.number)
        self.shifted: Optional[Tuple[int, int]] = None
        self.is_pot = p.role == "pot_cc"
        # Same shift-groups resolution as sequencer._ResolvedStep used to do:
        # pots use dedicated shift_pin/shift_channel/shift_number fields,
        # encoders repurpose their otherwise-unused in_min/out_min/out_max
        # (see pin_config.h / config_model.PinConfig).
        if self.is_pot:
            if p.shift_pin != protocol.NO_SHIFT_PIN:
                self.shifted = (p.shift_channel, p.shift_number)
        elif p.in_min != protocol.NO_SHIFT_PIN:
            self.shifted = (p.out_min, p.out_max)
        self.pin = p.pin
        self.spring = False
        self.meter_pin = p.mirror_pin
        self.dest = _resolve(pins, p.mirror_pin)
        self.value = PWM_METER_START


class ValueMirrorEngine:
    def __init__(self, pins: List[config_model.PinConfig], send_to_device: SendBytes,
                 binary_offset: bool = True, spring_pins: Iterable[int] = ()):
        self._send_to_device = send_to_device
        self._lock = threading.Lock()
        self._spring_thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        # False for firmware older than protocol.BINARY_OFFSET_MIN_FW_VERSION,
        # whose encoders still send two's complement -- decoding those as
        # binary offset would read a single CW click as 63 steps down.
        self._binary_offset = binary_offset
        self._targets = [
            _MirrorTarget(p, pins)
            for p in pins
            if p.role in ("encoder_a", "pot_cc")
            and p.mirror_pin != protocol.NO_SHIFT_PIN
            and _resolve(pins, p.mirror_pin) is not None
        ]
        # Only encoders can spring: a pot reports a real position, which
        # the meter should keep showing.
        spring_pins = set(spring_pins)
        for target in self._targets:
            target.spring = target.pin in spring_pins and not target.is_pot
        # (status type, channel, number) of every meter pin, as the firmware
        # would match it: CC if the light is set to match CC, else Note.
        self._meter_matches = set()
        for target in self._targets:
            meter = pins[target.meter_pin]
            channel, number = target.dest
            if meter.match_cc:
                self._meter_matches.add((0xB0, channel, number))
            else:
                self._meter_matches.add((0x90, channel, number))
                self._meter_matches.add((0x80, channel, number))

    def is_meter_message(self, raw: bytes) -> bool:
        """True if `raw` (a message from the DAW) would drive one of the
        meters this engine owns. Meters only ever follow their own control,
        never the DAW -- otherwise a DAW sending a mapped parameter's value
        back out (Ableton's Remote feedback) lands on whichever meter shares
        that channel/number, even one on a different Mega."""
        if len(raw) < 3:
            return False
        return (raw[0] & 0xF0, raw[0] & 0x0F, raw[1]) in self._meter_matches

    def handle_incoming(self, channel: int, number: int, value: int) -> None:
        """Called for every MIDI message arriving from the device. Never
        consumes anything -- a mirrored encoder/pot's own CC still needs to
        reach the DAW; this only observes it to update the meter."""
        start_spring = False
        with self._lock:
            for target in self._targets:
                if (channel, number) != target.plain and (channel, number) != target.shifted:
                    continue
                self._update(target, value)
                self._send_cc(target.dest, target.value)
                start_spring = start_spring or target.spring
        if start_spring:
            self._ensure_spring_thread()

    def _update(self, target: _MirrorTarget, value: int) -> None:
        if target.is_pot:
            # A pot reports its actual absolute position directly --
            # just mirror it, no accumulation needed.
            target.value = value
        else:
            # value is a relative click -- 65..127 = CW, 1..63 = CCW
            # (binary offset around 64; older firmware: 1..63 = CW,
            # 65..127 = CCW, two's complement; see ENCODER_A/
            # FLAG_ACCEL_ENABLE in firmware/src/roles.cpp), not an
            # absolute position, so accumulate it into one here instead
            # of forwarding it as-is. The magnitude scales the step so a
            # fast (accelerated) turn moves the meter further, same as
            # it moves the real mapped parameter.
            if self._binary_offset:
                steps = value - 64
            else:
                steps = value if value <= 63 else value - 128
            delta = (SPRING_PUSH if target.spring else PWM_METER_STEP) * steps
            target.value = max(0, min(127, target.value + delta))

    def _ensure_spring_thread(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            if self._spring_thread and self._spring_thread.is_alive():
                return
            self._spring_thread = threading.Thread(target=self._spring_loop, daemon=True)
            self._spring_thread.start()

    def _spring_loop(self) -> None:
        """Eases every spring meter back toward PWM_METER_START, one
        SPRING_TICK_S at a time, and exits once they've all settled -- the
        next click starts it again."""
        while not self._closed.wait(SPRING_TICK_S):
            with self._lock:
                moving = False
                for target in self._targets:
                    if not target.spring or target.value == PWM_METER_START:
                        continue
                    target.value = spring_step(target.value)
                    self._send_cc(target.dest, target.value)
                    moving = True
                if not moving:
                    self._spring_thread = None
                    return

    def close(self) -> None:
        self._closed.set()

    def _send_cc(self, dest: Tuple[int, int], value: int) -> None:
        channel, number = dest
        status = 0xB0 | (channel & 0x0F)
        self._send_to_device(bytes([status, number & 0x7F, value & 0x7F]))
