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


def test_segment_tracks_its_enable_pin():
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

    engine.observe(channel=9, number=50, value=1)
    assert engine.enabled == [False], "the control's CC doesn't touch the on/off state"
    engine.observe(channel=9, number=80, value=127)
    assert engine.enabled == [True]


def test_unassigned_segment_pins_are_harmless():
    pins = _make_pins()
    config = config_model.DroneConfig(enabled=True, segments=[config_model.DroneSegmentConfig()])
    engine = DroneEngine(config, pins)
    assert engine.enabled == [False]
    engine.observe(channel=0, number=0, value=1)
    assert engine.enabled == [False]


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
    engine.observe(channel=9, number=80, value=127)
    assert engine.enabled == [True, False]


def test_device_session_forwards_control_and_enable_cc_to_daw():
    # The real path from the board to the DAW: with a drone enabled, both
    # the encoder's CC and the enable button's CC must still go out.
    import device_session

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

    sent = []

    class FakeBridge:
        def send_to_daw(self, raw):
            sent.append(bytes(raw))

    session = device_session.DeviceSession.__new__(device_session.DeviceSession)
    session.bridge = FakeBridge()
    session.value_mirror = None
    session.encoder_scaler = None
    session.sequencer = None
    session.fx_rack = None
    session.drone = DroneEngine(config, pins)

    encoder_cc = bytes([0xB9, 50, 1])
    enable_cc = bytes([0xB9, 80, 127])
    session._on_device_midi(encoder_cc)  # segment still off
    session._on_device_midi(enable_cc)
    session._on_device_midi(encoder_cc)
    assert sent == [encoder_cc, enable_cc, encoder_cc], sent
    assert session.drone.enabled == [True]


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
