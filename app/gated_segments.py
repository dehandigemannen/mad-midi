"""Shared engine behind Drone and FX Rack: N segments, each with one enable
button, tracking which segments are switched on so the Drone/FX Rack tabs
can show it.

Nothing is gated: the enable button's own CC and the value controls' CCs all
reach the DAW like any other pin's output, so the DAW decides what "enabled"
means (e.g. map the enable CC to a device's on/off). The on/shift lights need
no engine support at all -- they're wired via PinConfig.listen_pin directly
at the enable/shift pins (firmware mirrors a toggle button's logical on/off
state, not just its raw press/release edges -- see roles.cpp's
applyListenLights()).
"""

from typing import Callable, List, Optional, Tuple

import config_model

StateCallback = Callable[[List[bool]], None]


def _resolve(pins: List[config_model.PinConfig], pin_index: Optional[int]) -> Optional[Tuple[int, int]]:
    if pin_index is None or pin_index < 0 or pin_index >= len(pins):
        return None
    p = pins[pin_index]
    return (p.channel, p.number)


class GatedSegmentEngine:
    def __init__(
        self,
        enable_pins: List[Optional[int]],
        pins: List[config_model.PinConfig],
        on_state_change: Optional[StateCallback] = None,
    ):
        """enable_pins: each segment's enable pin index (None = unassigned)."""
        self._enable_dests = [_resolve(pins, pin) for pin in enable_pins]
        self.enabled = [False] * len(self._enable_dests)  # fresh rebuild = all off
        self._on_state_change = on_state_change

    def adopt_runtime_state(self, old: "GatedSegmentEngine") -> None:
        """Carries the on/off state over from the engine this one replaces,
        segment by segment (extra or missing segments just start off)."""
        for index in range(min(len(self.enabled), len(old.enabled))):
            self.enabled[index] = old.enabled[index]

    def observe(self, channel: int, number: int, value: int) -> None:
        """Called for every MIDI message arriving from the device; updates a
        segment's on/off state when it's that segment's enable CC. Never
        suppresses anything -- every message still goes on to the DAW."""
        for index, dest in enumerate(self._enable_dests):
            if dest == (channel, number):
                self.enabled[index] = value != 0
                self._emit_state()

    def _emit_state(self) -> None:
        if self._on_state_change:
            self._on_state_change(list(self.enabled))
