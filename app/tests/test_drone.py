"""Standalone checks for drone.DroneEngine -- thin, since almost all its
behavior is really gated_segments.GatedSegmentEngine's (see
test_gated_segments.py); this only confirms DroneConfig/DroneSegmentConfig
translate into the right GatedSegmentEngine specs.

Run with: python3 app/tests/test_drone.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model
import protocol
from drone import DroneEngine


def _make_pins():
    return [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]


def test_segment_gates_its_control_pin():
    pins = _make_pins()
    pins[5].role = "button_cc"
    pins[5].toggle = True
    pins[5].channel = 9
    pins[5].number = 80
    pins[10].role = "encoder_a"
    pins[10].channel = 9
    pins[10].number = 50
    pins[10].in_min = protocol.NO_SHIFT_PIN

    config = config_model.DroneConfig(
        enabled=True,
        segments=[config_model.DroneSegmentConfig(control_pin=10, enable_pin=5)],
    )
    engine = DroneEngine(config, pins)
    assert engine.enabled == [False]

    assert engine.handle_incoming_pin_message(channel=9, number=50, value=1) is True, (
        "the drone's CC must be suppressed while disabled"
    )
    assert engine.handle_incoming_pin_message(channel=9, number=80, value=127) is True, (
        "the enable button's own message must never reach the DAW"
    )
    assert engine.enabled == [True]
    assert engine.handle_incoming_pin_message(channel=9, number=50, value=1) is False, (
        "the drone's CC must reach the DAW once enabled"
    )


def test_unassigned_segment_pins_are_harmless():
    pins = _make_pins()
    config = config_model.DroneConfig(enabled=True, segments=[config_model.DroneSegmentConfig()])
    engine = DroneEngine(config, pins)
    assert engine.enabled == [False]
    assert engine.handle_incoming_pin_message(channel=0, number=0, value=1) is False


def test_multiple_segments_from_config():
    pins = _make_pins()
    for enable_pin, number in ((5, 80), (6, 81)):
        pins[enable_pin].role = "button_cc"
        pins[enable_pin].toggle = True
        pins[enable_pin].channel = 9
        pins[enable_pin].number = number
    for control_pin, number in ((10, 50), (11, 51)):
        pins[control_pin].role = "pot_cc"
        pins[control_pin].channel = 9
        pins[control_pin].number = number
        pins[control_pin].shift_pin = protocol.NO_SHIFT_PIN

    config = config_model.DroneConfig(
        enabled=True,
        segments=[
            config_model.DroneSegmentConfig(control_pin=10, enable_pin=5),
            config_model.DroneSegmentConfig(control_pin=11, enable_pin=6),
        ],
    )
    engine = DroneEngine(config, pins)
    assert engine.enabled == [False, False]
    engine.handle_incoming_pin_message(channel=9, number=80, value=127)
    assert engine.enabled == [True, False]


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
