"""App-only drone engine: one instance per DeviceSession, built from an
Entry's DroneConfig (config_model.py) plus its current PinConfig list.

A drone never sends a Note -- only its value control's ordinary CC and its
enable button's CC, both straight to the DAW. GatedSegmentEngine only tracks
which segments are on, for the Drone tab's display.
The PWM meter is driven entirely by value_mirror.ValueMirrorEngine (via the
control pin's mirror_pin field), and the on/shift lights entirely by
firmware's listen_pin mechanism -- this engine never sends or suppresses
anything.
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
        self._gated = GatedSegmentEngine([seg.enable_pin for seg in config.segments], pins, on_state_change)

    def observe(self, channel: int, number: int, value: int) -> None:
        self._gated.observe(channel, number, value)

    def adopt_runtime_state(self, old: "DroneEngine") -> None:
        self._gated.adopt_runtime_state(old._gated)

    @property
    def enabled(self) -> List[bool]:
        return self._gated.enabled

    def close(self) -> None:
        pass  # no timers/resources to release -- kept for DeviceSession's uniform engine.close() convention
