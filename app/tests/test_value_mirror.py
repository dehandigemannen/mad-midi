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
    # The encoder only ever reports a relative click (65 = CW, 63 = CCW),
    # never an absolute position, so the PWM meter has to accumulate its own
    # running value rather than mirror the raw click straight through (which
    # would barely light up on a CW click and nearly max out on a single CCW
    # one) -- see value_mirror.py's PWM_METER_START/STEP.
    pins = _make_pins_with_mirror("encoder_a")
    engine, device_messages = _make_engine(pins)

    engine.handle_incoming(channel=9, number=50, value=65)  # CW click
    assert len(device_messages) == 1, "the PWM pin should have been driven from the accumulated value"
    status, number, value = device_messages[0]
    assert status == 0xB0 | 9
    assert number == 70  # the mirror target pin's configured CC number
    assert value == value_mirror.PWM_METER_START + value_mirror.PWM_METER_STEP

    engine.handle_incoming(channel=9, number=50, value=63)  # CCW click
    assert device_messages[-1][2] == value_mirror.PWM_METER_START, "a CCW click should move it back down"


def test_accelerated_encoder_turn_scales_pwm_by_magnitude():
    # FLAG_ACCEL_ENABLE turns can send any magnitude 65..127 (CW) / 1..63
    # (CCW), not just the plain 65/63 -- the PWM meter must scale by that
    # magnitude too, not treat every non-1 value as a single CCW click.
    pins = _make_pins_with_mirror("encoder_a")
    engine, device_messages = _make_engine(pins)

    engine.handle_incoming(channel=9, number=50, value=69)  # accelerated CW
    assert device_messages[-1][2] == value_mirror.PWM_METER_START + value_mirror.PWM_METER_STEP * 5

    engine.handle_incoming(channel=9, number=50, value=59)  # accelerated CCW (magnitude 5)
    assert device_messages[-1][2] == value_mirror.PWM_METER_START


def test_old_firmware_twos_complement_still_decoded():
    # Firmware older than BINARY_OFFSET_MIN_FW_VERSION sends 1 = CW, 127 =
    # CCW. Read as binary offset, a single CW click would be 63 steps down
    # and slam the meter to 0.
    pins = _make_pins_with_mirror("encoder_a")
    device_messages = []
    engine = ValueMirrorEngine(pins, send_to_device=device_messages.append, binary_offset=False)

    engine.handle_incoming(channel=9, number=50, value=1)  # CW click
    assert device_messages[-1][2] == value_mirror.PWM_METER_START + value_mirror.PWM_METER_STEP

    engine.handle_incoming(channel=9, number=50, value=127)  # CCW click
    assert device_messages[-1][2] == value_mirror.PWM_METER_START


def test_daw_messages_for_a_meter_are_recognised():
    # The meter pin (6) is a light_pwm matching CC 70 on channel 9 once
    # match_cc is set; a DAW echoing CC 70 back must be caught, anything
    # else must not.
    pins = _make_pins_with_mirror("encoder_a")
    pins[6].match_cc = True
    engine, _ = _make_engine(pins)
    assert engine.is_meter_message(bytes([0xB0 | 9, 70, 100]))
    assert not engine.is_meter_message(bytes([0xB0 | 9, 71, 100])), "other CC"
    assert not engine.is_meter_message(bytes([0xB0 | 3, 70, 100])), "other channel"
    assert not engine.is_meter_message(bytes([0x90 | 9, 70, 100])), "a Note can't drive a CC-matched light"


def test_daw_notes_for_a_note_matched_meter_are_recognised():
    pins = _make_pins_with_mirror("encoder_a")
    pins[6].match_cc = False
    engine, _ = _make_engine(pins)
    assert engine.is_meter_message(bytes([0x90 | 9, 70, 100]))
    assert engine.is_meter_message(bytes([0x80 | 9, 70, 0]))
    assert not engine.is_meter_message(bytes([0xB0 | 9, 70, 100]))


def test_spring_step_eases_back_to_the_middle():
    start = value_mirror.PWM_METER_START
    for v in (127, 0, start + 1, start - 1):
        steps = 0
        while v != start:
            nxt = value_mirror.spring_step(v)
            assert abs(nxt - start) < abs(v - start), "must always get closer to the middle"
            v = nxt
            steps += 1
        assert steps < 100


def test_spring_meter_kicks_by_direction_and_returns_to_middle():
    import time
    pins = _make_pins_with_mirror("encoder_a")
    device_messages = []
    engine = ValueMirrorEngine(pins, send_to_device=device_messages.append, spring_pins=[10])

    engine.handle_incoming(channel=9, number=50, value=65)  # CW click
    assert device_messages[-1][2] == value_mirror.PWM_METER_START + value_mirror.SPRING_PUSH

    time.sleep(1.5)  # well past the ~0.5s settle time
    assert device_messages[-1][2] == value_mirror.PWM_METER_START, "must settle back on the middle"

    engine.handle_incoming(channel=9, number=50, value=63)  # CCW click
    assert device_messages[-1][2] == value_mirror.PWM_METER_START - value_mirror.SPRING_PUSH
    engine.close()


def test_close_stops_the_spring():
    import time
    pins = _make_pins_with_mirror("encoder_a")
    device_messages = []
    engine = ValueMirrorEngine(pins, send_to_device=device_messages.append, spring_pins=[10])
    engine.handle_incoming(channel=9, number=50, value=70)
    engine.close()
    time.sleep(0.1)
    count = len(device_messages)
    time.sleep(0.2)
    assert len(device_messages) == count, "no more meter updates after close()"


def test_pots_and_non_spring_encoders_never_spring():
    import time
    pins = _make_pins_with_mirror("pot_cc", control_pin=54)
    device_messages = []
    engine = ValueMirrorEngine(pins, send_to_device=device_messages.append, spring_pins=[54])
    engine.handle_incoming(channel=9, number=50, value=100)
    time.sleep(0.2)
    assert [m[2] for m in device_messages] == [100], "a pot's meter must hold its real position"

    pins = _make_pins_with_mirror("encoder_a")
    device_messages = []
    engine = ValueMirrorEngine(pins, send_to_device=device_messages.append)
    engine.handle_incoming(channel=9, number=50, value=65)
    time.sleep(0.2)
    assert len(device_messages) == 1, "an encoder without spring keeps its accumulated value"


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

    engine.handle_incoming(channel=9, number=60, value=65)  # shifted CC, one CW click
    assert len(device_messages) == 1
    assert device_messages[-1][2] == value_mirror.PWM_METER_START + value_mirror.PWM_METER_STEP


def test_pwm_accumulator_clamps_at_bounds():
    pins = _make_pins_with_mirror("encoder_a")
    engine, device_messages = _make_engine(pins)

    for _ in range(100):
        engine.handle_incoming(channel=9, number=50, value=65)  # CW click
    assert device_messages[-1][2] == 127, "must clamp at the top instead of wrapping/overflowing"

    for _ in range(100):
        engine.handle_incoming(channel=9, number=50, value=63)  # CCW click
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
