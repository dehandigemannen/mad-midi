"""Standalone checks for gated_segments.GatedSegmentEngine -- the shared
enable-button-gates-value-pins engine behind Drone and FX Rack. No test
framework is installed in this project yet, so these are plain functions
with asserts, each runnable directly and also auto-discoverable if pytest is
added later.

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


def _set_encoder(pins, pin, channel=9, number=50, shift_pin=None, shift_number=None):
    pins[pin].role = "encoder_a"
    pins[pin].channel = channel
    pins[pin].number = number
    pins[pin].in_min = protocol.NO_SHIFT_PIN
    if shift_pin is not None:
        pins[pin].in_min = shift_pin
        pins[pin].out_min = channel
        pins[pin].out_max = shift_number


def test_enable_message_is_always_consumed_and_tracked():
    pins = _make_pins()
    _set_enable(pins, 5)
    _set_encoder(pins, 10)
    engine = GatedSegmentEngine([(5, [10])], pins)
    assert engine.enabled == [False]

    consumed = engine.handle_incoming_pin_message(channel=9, number=80, value=127)
    assert consumed is True, "a segment's enable message must never reach the DAW"
    assert engine.enabled == [True]

    consumed = engine.handle_incoming_pin_message(channel=9, number=80, value=0)
    assert consumed is True
    assert engine.enabled == [False]


def test_value_pin_suppressed_while_disabled_and_passed_through_while_enabled():
    pins = _make_pins()
    _set_enable(pins, 5)
    _set_encoder(pins, 10)
    engine = GatedSegmentEngine([(5, [10])], pins)

    suppress = engine.handle_incoming_pin_message(channel=9, number=50, value=1)
    assert suppress is True, "value CC must be suppressed while the segment is disabled"

    engine.handle_incoming_pin_message(channel=9, number=80, value=127)  # enable it
    passthrough = engine.handle_incoming_pin_message(channel=9, number=50, value=1)
    assert passthrough is False, "value CC must reach the DAW once the segment is enabled"


def test_unrelated_message_returns_false():
    pins = _make_pins()
    _set_enable(pins, 5)
    _set_encoder(pins, 10)
    engine = GatedSegmentEngine([(5, [10])], pins)
    assert engine.handle_incoming_pin_message(channel=2, number=99, value=1) is False


def test_shifted_destination_gated_same_as_plain():
    pins = _make_pins()
    _set_enable(pins, 5)
    _set_encoder(pins, 10, shift_pin=20, shift_number=60)
    engine = GatedSegmentEngine([(5, [10])], pins)

    assert engine.handle_incoming_pin_message(channel=9, number=60, value=1) is True, (
        "the shifted CC must be gated the same as the plain one while disabled"
    )
    engine.handle_incoming_pin_message(channel=9, number=80, value=127)  # enable
    assert engine.handle_incoming_pin_message(channel=9, number=60, value=1) is False


def test_multi_value_pin_segment_gates_both_together():
    # FX Rack's shape: one enable button gates two encoders at once.
    pins = _make_pins()
    _set_enable(pins, 5)
    _set_encoder(pins, 10, number=50)
    _set_encoder(pins, 20, number=51)
    engine = GatedSegmentEngine([(5, [10, 20])], pins)

    assert engine.handle_incoming_pin_message(channel=9, number=50, value=1) is True
    assert engine.handle_incoming_pin_message(channel=9, number=51, value=1) is True

    engine.handle_incoming_pin_message(channel=9, number=80, value=127)  # enable

    assert engine.handle_incoming_pin_message(channel=9, number=50, value=1) is False
    assert engine.handle_incoming_pin_message(channel=9, number=51, value=1) is False


def test_multiple_segments_are_independent():
    pins = _make_pins()
    _set_enable(pins, 5, number=80)
    _set_encoder(pins, 10, number=50)
    _set_enable(pins, 6, number=81)
    _set_encoder(pins, 11, number=51)
    engine = GatedSegmentEngine([(5, [10]), (6, [11])], pins)

    engine.handle_incoming_pin_message(channel=9, number=80, value=127)  # enable segment 0 only
    assert engine.enabled == [True, False]
    assert engine.handle_incoming_pin_message(channel=9, number=50, value=1) is False, "segment 0 enabled"
    assert engine.handle_incoming_pin_message(channel=9, number=51, value=1) is True, "segment 1 still disabled"


def test_state_change_callback_fires_on_enable_toggle():
    pins = _make_pins()
    _set_enable(pins, 5)
    _set_encoder(pins, 10)
    states = []
    engine = GatedSegmentEngine([(5, [10])], pins, on_state_change=states.append)
    engine.handle_incoming_pin_message(channel=9, number=80, value=127)
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
