"""Standalone checks for value_mirror.ValueMirrorEngine -- no hardware,
pywebview, or serial connection needed. No test framework is installed in
this project yet, so these are plain functions with asserts, each runnable
directly and also auto-discoverable if pytest is added later.

These were originally sequencer.py tests (the PWM meter used to be
sequencer-only, tied to a step's pwm_pin); they're rewritten here against a
plain pin list with mirror_pin set directly -- no SequencerStepConfig
involved at all -- since that's the whole point of the generalization: the
mirror works for any encoder_a/pot_cc pin, in or out of a composite feature.

Run with: python3 app/tests/test_value_mirror.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model
import protocol
import value_mirror
from value_mirror import ValueMirrorEngine


def _make_pins_with_mirror(control_role, control_pin=10, pwm_pin=6, shift_pin=None):
    """A plain 70-pin list with one encoder_a/pot_cc pin at control_pin,
    mirroring its value onto pwm_pin -- no sequencer/drone/fx involved."""
    pins = [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]
    c = pins[control_pin]
    c.role = control_role
    c.channel = 9
    c.number = 50
    c.mirror_pin = pwm_pin
    if control_role == "encoder_a":
        c.in_min = protocol.NO_SHIFT_PIN
        if shift_pin is not None:
            c.in_min = shift_pin
            c.out_min = 9
            c.out_max = 60  # shifted CC number
    elif control_role == "pot_cc":
        c.shift_pin = protocol.NO_SHIFT_PIN
        if shift_pin is not None:
            c.shift_pin = shift_pin
            c.shift_channel = 9
            c.shift_number = 60

    pins[pwm_pin].role = "light_pwm"
    pins[pwm_pin].channel = 9
    pins[pwm_pin].number = 70

    return pins


def _make_engine(pins):
    device_messages = []
    engine = ValueMirrorEngine(pins, send_to_device=device_messages.append)
    return engine, device_messages


def test_encoder_turn_accumulates_into_pwm():
    # The encoder only ever reports a relative click (1 = CW, 127 = CCW),
    # never an absolute position, so the PWM meter has to accumulate its own
    # running value rather than mirror the raw click straight through (which
    # would barely light up on a CW click and nearly max out on a single CCW
    # one) -- see value_mirror.py's PWM_METER_START/STEP.
    pins = _make_pins_with_mirror("encoder_a")
    engine, device_messages = _make_engine(pins)

    engine.handle_incoming(channel=9, number=50, value=1)  # CW click
    assert len(device_messages) == 1, "the PWM pin should have been driven from the accumulated value"
    status, number, value = device_messages[0]
    assert status == 0xB0 | 9
    assert number == 70  # the mirror target pin's configured CC number
    assert value == value_mirror.PWM_METER_START + value_mirror.PWM_METER_STEP

    engine.handle_incoming(channel=9, number=50, value=127)  # CCW click
    assert device_messages[-1][2] == value_mirror.PWM_METER_START, "a CCW click should move it back down"


def test_accelerated_encoder_turn_scales_pwm_by_magnitude():
    # FLAG_ACCEL_ENABLE turns can send any magnitude 1..63 (CW) / 65..127
    # (CCW), not just the plain 1/127 -- the PWM meter must scale by that
    # magnitude too, not treat every non-1 value as a single CCW click.
    pins = _make_pins_with_mirror("encoder_a")
    engine, device_messages = _make_engine(pins)

    engine.handle_incoming(channel=9, number=50, value=5)  # accelerated CW
    assert device_messages[-1][2] == value_mirror.PWM_METER_START + value_mirror.PWM_METER_STEP * 5

    engine.handle_incoming(channel=9, number=50, value=123)  # accelerated CCW (magnitude 5)
    assert device_messages[-1][2] == value_mirror.PWM_METER_START


def test_pot_value_mirrors_directly_into_pwm():
    # A pot reports its actual absolute position, unlike an encoder's
    # relative clicks -- the PWM meter must mirror it directly, not
    # accumulate.
    pins = _make_pins_with_mirror("pot_cc", control_pin=54)
    engine, device_messages = _make_engine(pins)

    engine.handle_incoming(channel=9, number=50, value=90)
    assert device_messages[-1][2] == 90


def test_shifted_destination_still_mirrors():
    # The meter must track the value control's live output regardless of
    # whether its shift button is currently held -- i.e. both the plain and
    # shifted (channel, number) destinations must match.
    pins = _make_pins_with_mirror("encoder_a", shift_pin=20)
    engine, device_messages = _make_engine(pins)

    engine.handle_incoming(channel=9, number=60, value=1)  # shifted CC, one CW click
    assert len(device_messages) == 1
    assert device_messages[-1][2] == value_mirror.PWM_METER_START + value_mirror.PWM_METER_STEP


def test_pwm_accumulator_clamps_at_bounds():
    pins = _make_pins_with_mirror("encoder_a")
    engine, device_messages = _make_engine(pins)

    for _ in range(100):
        engine.handle_incoming(channel=9, number=50, value=1)  # CW click
    assert device_messages[-1][2] == 127, "must clamp at the top instead of wrapping/overflowing"

    for _ in range(100):
        engine.handle_incoming(channel=9, number=50, value=127)  # CCW click
    assert device_messages[-1][2] == 0, "must clamp at the bottom instead of wrapping/underflowing"


def test_unrelated_message_is_ignored():
    pins = _make_pins_with_mirror("encoder_a")
    engine, device_messages = _make_engine(pins)
    engine.handle_incoming(channel=2, number=99, value=1)
    assert device_messages == []


def test_pin_with_no_mirror_set_produces_no_output():
    pins = [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]
    pins[10].role = "encoder_a"
    pins[10].channel = 9
    pins[10].number = 50
    pins[10].in_min = protocol.NO_SHIFT_PIN
    # mirror_pin left at its default (NO_SHIFT_PIN) -- no meter configured
    engine, device_messages = _make_engine(pins)
    engine.handle_incoming(channel=9, number=50, value=1)
    assert device_messages == []


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
