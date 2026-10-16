"""Standalone checks for unplug detection: serial_link.SerialLink noticing its
port dying (read or write error) and api.Api detaching the session so the
entry goes offline and auto-reconnect can pick the board back up.

Run with: python3 app/tests/test_link_lost.py
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import serial

import serial_link
from api import Api


class FakeSerial:
    """Stands in for serial.Serial: reads return nothing until `unplug()`,
    after which reads and writes raise like pyserial does on a vanished
    macOS cu.* port."""

    def __init__(self, *args, **kwargs):
        self.is_open = True
        self.unplugged = False
        self.written = []

    def unplug(self):
        self.unplugged = True

    def read(self, n):
        if self.unplugged:
            raise serial.SerialException("device reports readiness to read but returned no data")
        time.sleep(0.01)
        return b""

    def write(self, data):
        if self.unplugged:
            raise serial.SerialException("write failed: [Errno 6] Device not configured")
        self.written.append(bytes(data))

    def flush(self):
        if self.unplugged:
            raise serial.SerialException("write failed: [Errno 6] Device not configured")

    def close(self):
        self.is_open = False


def _wait_until(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _make_link(on_lost):
    serial_link.serial.Serial = FakeSerial
    link = serial_link.SerialLink(on_midi_from_device=lambda raw: None, on_lost=on_lost)
    link.connect("/dev/cu.fake")
    return link


def test_unplug_during_read_reports_lost_once():
    calls = []
    link = _make_link(lambda: calls.append(threading.current_thread()))
    fake = link._ser
    assert link.connected
    fake.unplug()
    assert _wait_until(lambda: calls), "on_lost never fired"
    time.sleep(0.05)
    assert len(calls) == 1
    assert not link.connected
    assert not fake.is_open


def test_unplug_seen_on_write_reports_lost_without_raising():
    calls = []
    link = _make_link(lambda: calls.append(1))
    link._stop.set()  # park the reader so the write is what notices
    link._reader_thread.join(timeout=1.0)
    link._stop.clear()
    link._ser.unplug()
    link.send_midi_to_device(b"\x90\x3c\x7f")  # must not raise into the DAW callback
    assert calls == [1]
    assert not link.connected


def test_pending_command_wakes_up_when_lost():
    link = _make_link(lambda: None)
    link._mode = "config"
    fake = link._ser
    threading.Timer(0.1, fake.unplug).start()
    started = time.time()
    assert link.ping(timeout=5.0) is None
    assert time.time() - started < 2.0, "ping sat out its whole timeout"


def test_deliberate_disconnect_is_not_reported_as_lost():
    calls = []
    link = _make_link(lambda: calls.append(1))
    link.disconnect()
    time.sleep(0.05)
    assert calls == []
    assert not link.connected


def test_lost_callback_can_close_its_own_link():
    """Api's handler calls session.close() -> link.disconnect() on the
    reader thread itself; that must not try to join itself."""
    errors = []
    holder = {}

    def on_lost():
        try:
            holder["link"].disconnect()
        except Exception as exc:
            errors.append(exc)

    holder["link"] = _make_link(on_lost)
    holder["link"]._ser.unplug()
    assert _wait_until(lambda: not holder["link"]._reader_thread.is_alive())
    assert errors == []


class FakeSession:
    def __init__(self, port, device_id):
        self.port = port
        self.device_id = device_id
        self.closed = False

    def close(self):
        self.closed = True


def _make_api():
    api = Api.__new__(Api)  # skip __init__: no poller thread, no preset migration
    api._devices = {}
    api._device_ports = {}
    api._lock = threading.Lock()
    api._busy_ports = set()
    return api


def test_api_detaches_lost_session():
    api = _make_api()
    session = FakeSession("/dev/cu.usbmodem1", 3)
    api._devices[session.port] = session
    api._device_ports[3] = session.port
    api._on_session_lost(session)
    assert api._devices == {}
    assert api._device_ports == {}
    assert session.closed


def test_api_ignores_lost_session_it_no_longer_owns():
    """e.g. flash_firmware() already detached it, and a new session now
    holds the same port."""
    api = _make_api()
    stale = FakeSession("/dev/cu.usbmodem1", 3)
    current = FakeSession("/dev/cu.usbmodem1", 3)
    api._devices[current.port] = current
    api._device_ports[3] = current.port
    api._on_session_lost(stale)
    assert api._devices == {current.port: current}
    assert api._device_ports == {3: current.port}
    assert not current.closed


def test_api_drops_sessions_whose_port_vanished():
    api = _make_api()
    gone = FakeSession("/dev/cu.usbmodem1", 3)
    here = FakeSession("/dev/cu.usbmodem2", 4)
    flashing = FakeSession("/dev/cu.usbmodem3", 5)
    for s in (gone, here, flashing):
        api._devices[s.port] = s
        api._device_ports[s.device_id] = s.port
    api._busy_ports.add(flashing.port)
    original = serial_link.SerialLink.list_ports
    serial_link.SerialLink.list_ports = staticmethod(lambda: [here.port])
    try:
        api._drop_vanished_sessions()
    finally:
        serial_link.SerialLink.list_ports = original
    assert gone.closed and not here.closed and not flashing.closed
    assert set(api._devices) == {here.port, flashing.port}


if __name__ == "__main__":
    tests = [fn for name, fn in list(globals().items()) if name.startswith("test_") and callable(fn)]
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
