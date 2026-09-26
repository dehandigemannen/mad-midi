"""Standalone checks for sequencer.SequencerEngine's tick/transport logic —
no hardware, pywebview, or serial connection needed. No test framework is
installed in this project yet, so these are plain functions with asserts,
each runnable directly and also auto-discoverable if pytest is added later.

Run with: python3 app/tests/test_sequencer.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model
import protocol
from sequencer import SUBDIVISION_TICKS, SequencerEngine


def _make_engine(
    subdivision="16th",
    num_steps=2,
    button_pins=None,
    enabled_light_pins=None,
):
    steps = []
    for i in range(num_steps):
        step = config_model.SequencerStepConfig(note=60 + i)
        if button_pins and i in button_pins:
            step.button_pin = button_pins[i]
        if enabled_light_pins and i in enabled_light_pins:
            step.enabled_light_pin = enabled_light_pins[i]
        steps.append(step)
    config = config_model.SequencerConfig(channel=9, subdivision=subdivision, steps=steps)

    pins = [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]
    for i, pin_index in (button_pins or {}).items():
        pins[pin_index].role = "button_cc"
        pins[pin_index].toggle = False  # latching switch: level mode, not edge-toggled
        pins[pin_index].channel = 9
        pins[pin_index].number = 100 + i
    for i, pin_index in (enabled_light_pins or {}).items():
        pins[pin_index].role = "light_digital"
        pins[pin_index].channel = 9
        pins[pin_index].number = 90 + i

    daw_messages = []
    device_messages = []
    states = []
    engine = SequencerEngine(
        config,
        pins,
        send_to_daw=daw_messages.append,
        send_to_device=device_messages.append,
        on_state_change=states.append,
    )
    return engine, daw_messages, device_messages, states


def _run_ticks(engine, count):
    for _ in range(count):
        engine.handle_realtime("clock")


def test_steps_advance_every_subdivision_ticks():
    engine, daw_messages, _device_messages, _states = _make_engine(subdivision="16th", num_steps=2)
    engine.set_step_enabled(0, True)
    engine.set_step_enabled(1, True)
    engine.handle_realtime("start")
    assert engine.current_step == 0

    ticks = SUBDIVISION_TICKS["16th"]
    assert ticks == 6

    _run_ticks(engine, ticks - 1)
    assert engine.current_step == 0, "should not advance before a full step's worth of ticks"
    assert len(daw_messages) == 0

    _run_ticks(engine, 1)
    assert engine.current_step == 1, "should advance exactly on the subdivision boundary"
    assert len(daw_messages) == 1, "step 0's Note On should have fired"
    status, note, _vel = daw_messages[0]
    assert status == 0x90 | 9  # Note On, channel 10 (0-indexed 9)
    assert note == 60  # step 0's note


def test_disabled_step_produces_no_output():
    engine, daw_messages, device_messages, _states = _make_engine(subdivision="16th", num_steps=1)
    # step 0 left disabled (default)
    engine.handle_realtime("start")
    _run_ticks(engine, SUBDIVISION_TICKS["16th"])
    assert daw_messages == []
    assert device_messages == []


def test_stop_halts_and_continue_resumes_from_held_position():
    engine, daw_messages, _device_messages, _states = _make_engine(subdivision="16th", num_steps=4)
    for i in range(4):
        engine.set_step_enabled(i, True)
    engine.handle_realtime("start")

    ticks = SUBDIVISION_TICKS["16th"]
    _run_ticks(engine, ticks * 2)  # advance two full steps: 0 -> 1 -> 2
    assert engine.current_step == 2
    assert engine.running is True

    engine.handle_realtime("stop")
    assert engine.running is False
    held_step = engine.current_step
    daw_messages.clear()

    _run_ticks(engine, ticks * 3)  # clock keeps arriving while stopped
    assert engine.current_step == held_step, "must not advance while stopped"
    assert daw_messages == [], "must not trigger notes while stopped"

    engine.handle_realtime("continue")
    assert engine.running is True
    assert engine.current_step == held_step, "continue must resume from the held position, not step 0"


def test_incoming_button_message_sets_step_from_level_and_is_consumed():
    """The button is a latching switch wired for level (not toggle) mode —
    the incoming CC value already *is* the switch's position, so the engine
    must set state directly from it rather than inverting (inverting would
    desync from the switch's real position after any missed edge)."""
    engine, _daw_messages, _device_messages, states = _make_engine(
        subdivision="16th", num_steps=2, button_pins={0: 5}
    )
    assert engine.step_enabled[0] is False

    consumed = engine.handle_incoming_pin_message(channel=9, number=100, value=127)
    assert consumed is True, "a step's button message must be consumed (suppressed from the DAW)"
    assert engine.step_enabled[0] is True
    assert states[-1]["step_enabled"][0] is True

    # Two "on" messages in a row (e.g. debounce chatter) must not flip it off
    # — a blind invert would get this wrong; a direct level-set does not.
    engine.handle_incoming_pin_message(channel=9, number=100, value=127)
    assert engine.step_enabled[0] is True

    engine.handle_incoming_pin_message(channel=9, number=100, value=0)
    assert engine.step_enabled[0] is False

    not_consumed = engine.handle_incoming_pin_message(channel=9, number=42, value=127)
    assert not_consumed is False, "an unrelated message must be forwarded normally"


def test_enabled_light_tracks_step_on_off_state():
    # enabled_light is a persistent on/off indicator, distinct from
    # step_light (which only flashes on trigger) -- it must follow
    # set_step_enabled directly, both via the app and via the step's own
    # button message.
    engine, _daw_messages, device_messages, _states = _make_engine(
        subdivision="16th", num_steps=1, button_pins={0: 5}, enabled_light_pins={0: 20}
    )
    assert device_messages == []

    engine.set_step_enabled(0, True)
    assert tuple(device_messages[-1]) == (0xB0 | 9, 90, 127)

    engine.set_step_enabled(0, False)
    assert tuple(device_messages[-1]) == (0xB0 | 9, 90, 0)

    engine.handle_incoming_pin_message(channel=9, number=100, value=127)  # step 0's button, pressed
    assert tuple(device_messages[-1]) == (0xB0 | 9, 90, 127)


def test_rebuilt_engine_keeps_transport_and_pattern():
    # Every UI edit rebuilds the engine (DeviceSession.rebuild_engines); a
    # rebuild mid-playback must not drop back to stopped/all-off.
    old, _, _, _ = _make_engine(num_steps=4)
    old.handle_realtime("start")
    old.set_step_enabled(1, True)
    old.set_step_enabled(3, True)
    _run_ticks(old, SUBDIVISION_TICKS["16th"] + 1)  # playhead on step 1 (enabled), 1 tick in

    new, daw_messages, _, _ = _make_engine(num_steps=4)
    new.adopt_runtime_state(old)
    assert new.running
    assert new.current_step == old.current_step
    assert new.step_enabled == [False, True, False, True]

    # keeps counting from the carried-over tick position, no Start needed
    _run_ticks(new, SUBDIVISION_TICKS["16th"] - 1)
    assert any(m[0] & 0xF0 == 0x90 for m in daw_messages), "rebuilt engine didn't play on"


def test_rebuilt_engine_clamps_to_new_step_count():
    old, _, _, _ = _make_engine(num_steps=4)
    old.handle_realtime("start")
    old.set_step_enabled(3, True)
    _run_ticks(old, SUBDIVISION_TICKS["16th"] * 3)  # current_step == 3

    new, _, _, _ = _make_engine(num_steps=2)
    new.adopt_runtime_state(old)
    assert new.current_step < 2
    assert len(new.step_enabled) == 2


def test_close_sends_pending_note_off():
    engine, daw_messages, _, _ = _make_engine(num_steps=1)
    engine.handle_realtime("start")
    engine.set_step_enabled(0, True)
    _run_ticks(engine, SUBDIVISION_TICKS["16th"])  # step 0 fires, note-off still pending
    assert daw_messages[-1][0] & 0xF0 == 0x90
    engine.close()
    assert daw_messages[-1][0] & 0xF0 == 0x80, "closing mid-gate left a hanging note"


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
