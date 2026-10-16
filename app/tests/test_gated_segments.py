"""Standalone checks for gated_segments.GatedSegmentEngine -- the shared
enable-state tracker behind Drone and FX Rack. No test framework is
installed in this project yet, so these are plain functions with asserts,
each runnable directly and also auto-discoverable if pytest is added later.

Run with: python3 app/tests/test_gated_segments.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model
import protocol
from gated_segments import GatedSegmentEngine


def _make_pins():
    return [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]


def _set_enable(pins, pin, channel=9, number=80):
    pins[pin].role = "button_cc"
    pins[pin].toggle = True
    pins[pin].channel = channel
    pins[pin].number = number


def test_enable_message_is_tracked():
    pins = _make_pins()
    _set_enable(pins, 5)
    engine = GatedSegmentEngine([5], pins)
    assert engine.enabled == [False]

    engine.observe(channel=9, number=80, value=127)
    assert engine.enabled == [True]

    engine.observe(channel=9, number=80, value=0)
    assert engine.enabled == [False]


def test_unrelated_message_changes_nothing():
    pins = _make_pins()
    _set_enable(pins, 5)
    states = []
    engine = GatedSegmentEngine([5], pins, on_state_change=states.append)
    engine.observe(channel=2, number=99, value=127)
    assert engine.enabled == [False]
    assert states == []


def test_multiple_segments_are_independent():
    pins = _make_pins()
    _set_enable(pins, 5, number=80)
    _set_enable(pins, 6, number=81)
    engine = GatedSegmentEngine([5, 6], pins)

    engine.observe(channel=9, number=80, value=127)  # enable segment 0 only
    assert engine.enabled == [True, False]


def test_unassigned_enable_pin_is_harmless():
    pins = _make_pins()
    engine = GatedSegmentEngine([None], pins)
    engine.observe(channel=0, number=0, value=127)
    assert engine.enabled == [False]


def test_state_change_callback_fires_on_enable_toggle():
    pins = _make_pins()
    _set_enable(pins, 5)
    states = []
    engine = GatedSegmentEngine([5], pins, on_state_change=states.append)
    engine.observe(channel=9, number=80, value=127)
    assert states[-1] == [True]


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
