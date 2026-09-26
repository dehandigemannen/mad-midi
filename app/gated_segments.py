"""Shared engine behind Drone and FX Rack: N segments, each with one enable
button gating one or more value-control pins' CC from reaching the DAW.

Neither feature needs its own note/trigger logic (unlike the Sequencer) --
a segment's enable button doesn't send anything itself (see
config_model.DroneSegmentConfig/FxSegmentConfig: it's an ordinary
button_cc+toggle pin), it just decides whether its value control(s)' own
ordinary CC output is allowed through. The on/shift lights need no engine
support at all -- they're wired via PinConfig.listen_pin directly at the
enable/shift pins (firmware mirrors a toggle button's logical on/off state,
not just its raw press/release edges -- see roles.cpp's applyListenLights()).
"""

from typing import Callable, List, Optional, Sequence, Tuple

import config_model
import protocol

StateCallback = Callable[[List[bool]], None]


def _resolve(pins: List[config_model.PinConfig], pin_index: Optional[int]) -> Optional[Tuple[int, int]]:
    if pin_index is None or pin_index < 0 or pin_index >= len(pins):
        return None
    p = pins[pin_index]
    return (p.channel, p.number)


def _resolve_value_dest(pins: List[config_model.PinConfig], pin_index: Optional[int]):
    """Resolves a value-control pin (encoder_a or pot_cc) to its plain +
    shifted (channel, number) destinations, same shift-groups logic as
    value_mirror._MirrorTarget / the old sequencer._ResolvedStep."""
    if pin_index is None or pin_index < 0 or pin_index >= len(pins):
        return None
    p = pins[pin_index]
    plain = (p.channel, p.number)
    shifted = None
    if p.role == "pot_cc":
        if p.shift_pin != protocol.NO_SHIFT_PIN:
            shifted = (p.shift_channel, p.shift_number)
    elif p.in_min != protocol.NO_SHIFT_PIN:
        shifted = (p.out_min, p.out_max)
    return plain, shifted


class _ResolvedSegment:
    def __init__(self, enable_pin: Optional[int], value_pins: Sequence[Optional[int]],
                 pins: List[config_model.PinConfig]):
        self.enable_dest = _resolve(pins, enable_pin)
        self.value_dests = [
            resolved for resolved in (_resolve_value_dest(pins, vp) for vp in value_pins)
            if resolved is not None
        ]


class GatedSegmentEngine:
    def __init__(
        self,
        segment_specs: List[Tuple[Optional[int], Sequence[Optional[int]]]],
        pins: List[config_model.PinConfig],
        on_state_change: Optional[StateCallback] = None,
    ):
        """segment_specs: one (enable_pin_index, [value_pin_indices]) tuple
        per segment -- Drone passes a single value pin per segment, FX Rack
        passes two (its pair of encoders)."""
        self._segments = [_ResolvedSegment(enable, values, pins) for enable, values in segment_specs]
        self.enabled = [False] * len(self._segments)  # fresh rebuild = all off
        self._on_state_change = on_state_change

    def handle_incoming_pin_message(self, channel: int, number: int, value: int) -> bool:
        """Called for every MIDI message arriving from the device. Returns
        True if the message must be suppressed from the DAW (either it's a
        segment's own enable button, which never reaches the DAW, or it's a
        value pin whose segment is currently disabled), False otherwise."""
        for index, segment in enumerate(self._segments):
            if segment.enable_dest == (channel, number):
                self.enabled[index] = value != 0
                self._emit_state()
                return True
            for plain, shifted in segment.value_dests:
                if (channel, number) == plain or (shifted is not None and (channel, number) == shifted):
                    return not self.enabled[index]
        return False

    def _emit_state(self) -> None:
        if self._on_state_change:
            self._on_state_change(list(self.enabled))
