"""App-only FX rack engine: one instance per DeviceSession, built from an
Entry's FxRackConfig (config_model.py) plus its current PinConfig list.

Same as Drone (see drone.py), just with two encoders per segment and no
PWM/mirror involvement at all: every CC goes straight to the DAW, and the
engine only tracks which segments are on for the FX Rack tab's display. The on/shift lights are driven entirely by firmware's listen_pin
mechanism, same as Drone's.
"""

from typing import List, Optional

import config_model
from gated_segments import GatedSegmentEngine, StateCallback


class FxRackEngine:
    def __init__(
        self,
        config: config_model.FxRackConfig,
        pins: List[config_model.PinConfig],
        on_state_change: Optional[StateCallback] = None,
    ):
        self._gated = GatedSegmentEngine([seg.enable_pin for seg in config.segments], pins, on_state_change)

    def observe(self, channel: int, number: int, value: int) -> None:
        self._gated.observe(channel, number, value)

    def adopt_runtime_state(self, old: "FxRackEngine") -> None:
        self._gated.adopt_runtime_state(old._gated)

    @property
    def enabled(self) -> List[bool]:
        return self._gated.enabled

    def close(self) -> None:
        pass  # no timers/resources to release -- kept for DeviceSession's uniform engine.close() convention
