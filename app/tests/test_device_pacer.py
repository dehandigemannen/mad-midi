"""Standalone checks for device_pacer.DeviceMidiPacer -- no hardware or
serial connection needed.

Run with: python3 app/tests/test_device_pacer.py
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device_pacer import DeviceMidiPacer


def _wait_until(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _make_blocked_pacer(**kwargs):
    """A pacer whose first write blocks until released, so the test can
    queue up messages behind it deterministically."""
    written = []
    gate = threading.Event()

    def write(raw):
        gate.wait()
        written.append(raw)

    pacer = DeviceMidiPacer(write, **kwargs)
    pacer.start()
    return pacer, written, gate


def test_newer_value_for_same_light_replaces_unsent_one():
    pacer, written, gate = _make_blocked_pacer(window_s=0.001)
    pacer.submit(bytes([0xB9, 1, 0]))  # picked up immediately, blocks in write
    time.sleep(0.02)
    for value in (10, 20, 30, 127):
        pacer.submit(bytes([0xB9, 80, value]))
    pacer.submit(bytes([0xB9, 80, 0]))
    gate.set()
    assert _wait_until(lambda: len(written) >= 2)
    time.sleep(0.02)
    assert written == [bytes([0xB9, 1, 0]), bytes([0xB9, 80, 0])]
    pacer.stop()


def test_note_on_and_off_for_same_light_coalesce():
    pacer, written, gate = _make_blocked_pacer(window_s=0.001)
    pacer.submit(bytes([0xB9, 1, 0]))
    time.sleep(0.02)
    pacer.submit(bytes([0x99, 60, 100]))
    pacer.submit(bytes([0x89, 60, 0]))
    gate.set()
    assert _wait_until(lambda: len(written) >= 2)
    time.sleep(0.02)
    assert written[1:] == [bytes([0x89, 60, 0])]
    pacer.stop()


def test_different_lights_all_arrive_in_order():
    pacer, written, gate = _make_blocked_pacer(window_s=0.001)
    pacer.submit(bytes([0xB9, 1, 0]))
    time.sleep(0.02)
    msgs = [bytes([0xB9, n, 127]) for n in range(10, 20)]
    for m in msgs:
        pacer.submit(m)
    gate.set()
    assert _wait_until(lambda: len(written) == 11)
    assert written[1:] == msgs
    pacer.stop()


def test_rate_is_capped():
    written = []
    pacer = DeviceMidiPacer(written.append, max_bytes=6, window_s=0.05)
    for n in range(10):
        pacer.submit(bytes([0xB9, n, 127]))
    pacer.start()
    time.sleep(0.02)
    assert len(written) == 2, "first window should carry only max_bytes"
    pacer.stop()


def test_stop_drops_pending_and_ignores_new():
    written = []
    pacer = DeviceMidiPacer(written.append)
    pacer.stop()
    pacer.submit(bytes([0xB9, 1, 0]))
    pacer.start()
    time.sleep(0.02)
    assert written == []


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
