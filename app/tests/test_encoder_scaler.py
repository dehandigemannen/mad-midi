"""Standalone checks for encoder_scaler.EncoderStepScaler -- no hardware,
pywebview, or serial connection needed. Plain functions with asserts, same
as the other test files here.

Run with: python3 app/tests/test_encoder_scaler.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model
import protocol
from encoder_scaler import EncoderStepScaler


def _make_pins(step_size, shift_pin=None, role="encoder_a"):
    pins = [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]
    c = pins[10]
    c.role = role
    c.channel = 9
    c.number = 50
    c.step_size = step_size
    c.in_min = protocol.NO_SHIFT_PIN
    if shift_pin is not None:
        c.in_min = shift_pin
        c.out_min = 9
        c.out_max = 60  # shifted CC number
    return pins


def test_binary_offset_steps_are_multiplied():
    scaler = EncoderStepScaler(_make_pins(4))
    assert scaler.scale(9, 50, 65) == 68, "one CW click x4"
    assert scaler.scale(9, 50, 63) == 60, "one CCW click x4"
    assert scaler.scale(9, 50, 66) == 72, "an accelerated 2-step click x4"


def test_scaled_step_is_capped_at_63():
    scaler = EncoderStepScaler(_make_pins(16))
    assert scaler.scale(9, 50, 74) == 127, "+10 x16 must cap at +63, not wrap"
    assert scaler.scale(9, 50, 54) == 1, "-10 x16 must cap at -63, not wrap"


def test_twos_complement_for_old_firmware():
    scaler = EncoderStepScaler(_make_pins(4), binary_offset=False)
    assert scaler.scale(9, 50, 1) == 4, "one CW click x4"
    assert scaler.scale(9, 50, 127) == 124, "one CCW click x4"


def test_shifted_cc_is_scaled_too():
    scaler = EncoderStepScaler(_make_pins(4, shift_pin=20))
    assert scaler.scale(9, 60, 65) == 68


def test_step_size_one_changes_nothing():
    scaler = EncoderStepScaler(_make_pins(1))
    assert not scaler.active
    assert scaler.scale(9, 50, 65) == 65


def test_other_ccs_and_non_encoders_are_untouched():
    scaler = EncoderStepScaler(_make_pins(4))
    assert scaler.scale(9, 51, 65) == 65, "different CC number"
    assert scaler.scale(2, 50, 65) == 65, "different channel"
    pot_scaler = EncoderStepScaler(_make_pins(4, role="pot_cc"))
    assert pot_scaler.scale(9, 50, 65) == 65, "a pot's absolute value must never be rescaled"


def test_step_size_defaults_to_one_for_saved_configs_without_it():
    d = config_model.PinConfig(pin=3).to_dict()
    del d["step_size"]
    assert config_model.PinConfig.from_dict(d).step_size == 1


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"\n{failures}/{len(tests)} failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} checks passed")
