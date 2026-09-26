"""App-only FX rack engine: one instance per DeviceSession, built from an
Entry's FxRackConfig (config_model.py) plus its current PinConfig list.

Each segment's enable button gates BOTH of its encoders' CC output from
reaching the DAW at once -- otherwise identical to Drone's gating, just with
two value pins per segment instead of one, and no PWM/mirror involvement at
all. The on/shift lights are driven entirely by firmware's listen_pin
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
        specs = [(seg.enable_pin, [seg.encoder1_pin, seg.encoder2_pin]) for seg in config.segments]
        self._gated = GatedSegmentEngine(specs, pins, on_state_change)

    def handle_incoming_pin_message(self, channel: int, number: int, value: int) -> bool:
        return self._gated.handle_incoming_pin_message(channel, number, value)

    @property
    def enabled(self) -> List[bool]:
        return self._gated.enabled

    def close(self) -> None:
        pass  # no timers/resources to release -- kept for DeviceSession's uniform engine.close() convention
