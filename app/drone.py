"""App-only drone engine: one instance per DeviceSession, built from an
Entry's DroneConfig (config_model.py) plus its current PinConfig list.

A drone never sends a Note -- only its value control's ordinary CC, gated by
GatedSegmentEngine so it only reaches the DAW while the segment is enabled.
The PWM meter is driven entirely by value_mirror.ValueMirrorEngine (via the
control pin's mirror_pin field), and the on/shift lights entirely by
firmware's listen_pin mechanism -- this engine never sends anything itself,
it only decides whether an incoming message should be suppressed.
"""

from typing import List, Optional

import config_model
from gated_segments import GatedSegmentEngine, StateCallback


class DroneEngine:
    def __init__(
        self,
        config: config_model.DroneConfig,
        pins: List[config_model.PinConfig],
        on_state_change: Optional[StateCallback] = None,
    ):
        specs = [(seg.enable_pin, [seg.control_pin]) for seg in config.segments]
        self._gated = GatedSegmentEngine(specs, pins, on_state_change)

    def handle_incoming_pin_message(self, channel: int, number: int, value: int) -> bool:
        return self._gated.handle_incoming_pin_message(channel, number, value)

    @property
    def enabled(self) -> List[bool]:
        return self._gated.enabled

    def close(self) -> None:
        pass  # no timers/resources to release -- kept for DeviceSession's uniform engine.close() convention
