"""Standalone checks for the connect-time state sync ("snapshot"): the app
asks the board for its switch/pot positions once the session is ready, but
only on firmware that understands the request, and Drone/FX on/off state
survives engine rebuilds. No hardware needed.

Run with: python3 app/tests/test_snapshot.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model
import device_session
import protocol
from drone import DroneEngine
from fx_rack import FxRackEngine


class FakeLink:
    def __init__(self, mode="run"):
        self.connected = True
        self._mode = mode
        self.snapshots = 0

    def request_snapshot(self):
        self.snapshots += 1


def _session(fw_version):
    session = device_session.DeviceSession.__new__(device_session.DeviceSession)
    session.link = FakeLink()
    session.fw_version = fw_version
    return session


def test_snapshot_requested_on_new_firmware():
    session = _session(protocol.SNAPSHOT_MIN_FW_VERSION)
    session.request_snapshot()
    assert session.link.snapshots == 1


def test_snapshot_skipped_on_old_or_unknown_firmware():
    for fw in (None, 3):
        session = _session(fw)
        session.request_snapshot()
        assert session.link.snapshots == 0, fw


def test_serial_link_sends_trigger_only_in_run_mode():
    import serial_link

    sent = []
    link = serial_link.SerialLink.__new__(serial_link.SerialLink)
    link._ser = type("OpenPort", (), {"is_open": True})()
    link._mode = "run"
    link._write = lambda data: sent.append(bytes(data)) or True
    link._log = lambda *args: None

    link.request_snapshot()
    assert sent == [protocol.SYSEX_SNAPSHOT]

    link._mode = "config"
    link.request_snapshot()
    assert sent == [protocol.SYSEX_SNAPSHOT], "must not send while in config mode"


def _pins():
    pins = [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]
    pins[5].role = "button_cc"
    pins[5].toggle = True
    pins[5].channel = 9
    pins[5].number = 80
    return pins


def test_drone_on_state_survives_rebuild():
    pins = _pins()
    config = config_model.DroneConfig(
        enabled=True, segments=[config_model.DroneSegmentConfig(enable_pin=5)]
    )
    old = DroneEngine(config, pins)
    old.observe(channel=9, number=80, value=127)
    new = DroneEngine(config, pins)
    new.adopt_runtime_state(old)
    assert new.enabled == [True]


def test_fx_rack_on_state_survives_rebuild_with_more_segments():
    pins = _pins()
    one = config_model.FxRackConfig(enabled=True, segments=[config_model.FxSegmentConfig(enable_pin=5)])
    two = config_model.FxRackConfig(
        enabled=True, segments=[config_model.FxSegmentConfig(enable_pin=5), config_model.FxSegmentConfig()]
    )
    old = FxRackEngine(one, pins)
    old.observe(channel=9, number=80, value=127)
    new = FxRackEngine(two, pins)
    new.adopt_runtime_state(old)
    assert new.enabled == [True, False]


def test_attach_requests_snapshot_after_engines_are_built():
    # The snapshot's MIDI must land on the final engines, so the request has
    # to come after rebuild_engines().
    import api

    calls = []

    class FakeSession:
        port = "p1"
        baseline_pins = None
        active_pins = None

        class link:
            @staticmethod
            def enter_config_mode():
                return False

        def rebuild_engines(self, entry):
            calls.append("rebuild")

        def request_snapshot(self):
            calls.append("snapshot")

    a = api.Api.__new__(api.Api)
    a._devices = {}
    a._device_ports = {}
    entry = config_model.Entry.blank("1", "device", "Test", device_id=1)
    a._attach_session(entry, FakeSession())
    assert calls == ["rebuild", "snapshot"], calls


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
