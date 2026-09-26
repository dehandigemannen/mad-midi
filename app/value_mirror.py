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

from typing import Callable, List, Optional, Tuple

import config_model
import protocol

PWM_METER_START = 64  # start each meter at mid-brightness so both turn directions are visible right away
PWM_METER_STEP = 3  # brightness change per detent -- ~a third of a full turn (42 clicks) sweeps 0-127

SendBytes = Callable[[bytes], None]


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
        self.dest = _resolve(pins, p.mirror_pin)
        self.value = PWM_METER_START


class ValueMirrorEngine:
    def __init__(self, pins: List[config_model.PinConfig], send_to_device: SendBytes):
        self._send_to_device = send_to_device
        self._targets = [
            _MirrorTarget(p, pins)
            for p in pins
            if p.role in ("encoder_a", "pot_cc")
            and p.mirror_pin != protocol.NO_SHIFT_PIN
            and _resolve(pins, p.mirror_pin) is not None
        ]

    def handle_incoming(self, channel: int, number: int, value: int) -> None:
        """Called for every MIDI message arriving from the device. Never
        consumes anything -- a mirrored encoder/pot's own CC still needs to
        reach the DAW; this only observes it to update the meter."""
        for target in self._targets:
            if (channel, number) != target.plain and (channel, number) != target.shifted:
                continue
            if target.is_pot:
                # A pot reports its actual absolute position directly --
                # just mirror it, no accumulation needed.
                target.value = value
            else:
                # value is a relative click -- 1..63 = CW, 65..127 = CCW
                # (two's-complement magnitude; see ENCODER_A/
                # FLAG_ACCEL_ENABLE in firmware/src/roles.cpp), not an
                # absolute position, so accumulate it into one here instead
                # of forwarding it as-is. The magnitude scales the step so a
                # fast (accelerated) turn moves the meter further, same as
                # it moves the real mapped parameter.
                if value <= 63:
                    delta = PWM_METER_STEP * value
                else:
                    delta = -PWM_METER_STEP * (128 - value)
                target.value = max(0, min(127, target.value + delta))
            self._send_cc(target.dest, target.value)

    def _send_cc(self, dest: Tuple[int, int], value: int) -> None:
        channel, number = dest
        status = 0xB0 | (channel & 0x0F)
        self._send_to_device(bytes([status, number & 0x7F, value & 0x7F]))
