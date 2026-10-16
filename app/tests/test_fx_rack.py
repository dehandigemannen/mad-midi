"""Standalone checks for fx_rack.FxRackEngine -- thin, since almost all its
behavior is really gated_segments.GatedSegmentEngine's (see
test_gated_segments.py); this only confirms FxRackConfig/FxSegmentConfig
translate into the right GatedSegmentEngine specs (two value pins per
segment, gated together by one enable button).

Run with: python3 app/tests/test_fx_rack.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model
import protocol
from fx_rack import FxRackEngine


def _make_pins():
    return [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]


def test_segment_gates_both_encoders_together():
    pins = _make_pins()
    pins[5].role = "button_cc"
    pins[5].toggle = True
    pins[5].channel = 9
    pins[5].number = 80
    for pin, number in ((10, 50), (20, 51)):
        pins[pin].role = "encoder_a"
        pins[pin].channel = 9
        pins[pin].number = number
        pins[pin].in_min = protocol.NO_SHIFT_PIN

    config = config_model.FxRackConfig(
        enabled=True,
        segments=[config_model.FxSegmentConfig(encoder1_pin=10, encoder2_pin=20, enable_pin=5)],
    )
    engine = FxRackEngine(config, pins)
    assert engine.enabled == [False]

    engine.observe(channel=9, number=50, value=1)  # encoders don't touch the state
    engine.observe(channel=9, number=51, value=1)
    assert engine.enabled == [False]

    engine.observe(channel=9, number=80, value=127)
    assert engine.enabled == [True]


def test_unassigned_segment_pins_are_harmless():
    pins = _make_pins()
    config = config_model.FxRackConfig(enabled=True, segments=[config_model.FxSegmentConfig()])
    engine = FxRackEngine(config, pins)
    assert engine.enabled == [False]
    engine.observe(channel=0, number=0, value=1)
    assert engine.enabled == [False]


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
