"""App-side step multiplier for relative encoders. Any encoder_a pin with
PinConfig.step_size > 1 has every relative CC it sends multiplied by that
factor before it reaches the DAW, so a parameter with a wide range (say a
0-1000 knob in Live) needs fewer turns. The firmware has no idea this is
happening: it keeps sending its own 1-step clicks (or bigger ones, with
FLAG_ACCEL_ENABLE), and this rewrites them on their way through.

The scaled value is what everyone downstream sees -- the DAW and the PWM
meter (value_mirror.py) alike -- so the meter moves exactly as far as the
mapped parameter does.
"""

from typing import Dict, List, Tuple

import config_model
import protocol

MAX_STEP = 63  # largest relative step either encoding can carry in one CC


class EncoderStepScaler:
    def __init__(self, pins: List[config_model.PinConfig], binary_offset: bool = True):
        # False for firmware older than protocol.BINARY_OFFSET_MIN_FW_VERSION,
        # whose encoders still send two's complement (1 = CW, 127 = CCW).
        self._binary_offset = binary_offset
        self._factors: Dict[Tuple[int, int], int] = {}
        for p in pins:
            if p.role != "encoder_a" or p.step_size <= 1:
                continue
            self._factors[(p.channel, p.number)] = p.step_size
            # encoder_a repurposes in_min/out_min/out_max as shift pin/channel/CC
            # (see pin_config.h); a shifted turn needs the same scaling.
            if p.in_min != protocol.NO_SHIFT_PIN:
                self._factors[(p.out_min, p.out_max)] = p.step_size

    @property
    def active(self) -> bool:
        return bool(self._factors)

    def scale(self, channel: int, number: int, value: int) -> int:
        """Returns the CC value to forward: `value` multiplied by the
        encoder's step_size, or unchanged if (channel, number) isn't a
        scaled encoder."""
        factor = self._factors.get((channel, number))
        if not factor:
            return value
        if self._binary_offset:
            steps = value - 64
        else:
            steps = value if value <= 63 else value - 128
        if steps == 0:
            return value
        scaled = max(-MAX_STEP, min(MAX_STEP, steps * factor))
        if self._binary_offset:
            return 64 + scaled
        return scaled & 0x7F
