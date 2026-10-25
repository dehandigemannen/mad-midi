"""Paces and coalesces app-to-device MIDI so the Mega's 64-byte serial RX
buffer never overflows.

Everything sent to the device only drives light/PWM pins, i.e. state, not
events: if a light gets two values before the first went out, only the
newest matters. So pending Note/CC messages are keyed by their target
(note vs CC, channel, number) and a newer value replaces an unsent older
one in place. A value-mirror echoing a 30-click encoder burst becomes one
CC instead of 30.

The writer thread then sends at most PACE_BYTES per PACE_WINDOW_S. The
firmware drains RX only between loop() passes, and a pass can block for
several ms while its own TX buffer is full (e.g. an encoder burst going
out); bytes arriving faster than that get dropped, and a dropped light-off
leaves a lamp stuck on.
"""

import itertools
import threading
from collections import OrderedDict
from typing import Callable

PACE_BYTES = 12  # 4 three-byte messages
PACE_WINDOW_S = 0.005  # -> at most ~2.4 bytes/ms, a fifth of 115200 baud

_COALESCE_TYPES = {0x80: "note", 0x90: "note", 0xB0: "cc"}


def _key(raw: bytes, seq: int):
    if len(raw) == 3:
        kind = _COALESCE_TYPES.get(raw[0] & 0xF0)
        if kind:
            return (kind, raw[0] & 0x0F, raw[1])
    return ("raw", seq)  # anything else goes out as-is, never merged


class DeviceMidiPacer:
    def __init__(self, write: Callable[[bytes], None],
                 max_bytes: int = PACE_BYTES, window_s: float = PACE_WINDOW_S):
        self._write = write
        self._max_bytes = max_bytes
        self._window_s = window_s
        self._pending: "OrderedDict" = OrderedDict()
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopped.set()
            self._pending.clear()
            self._cond.notify()

    def submit(self, raw: bytes) -> None:
        with self._cond:
            if self._stopped.is_set():
                return
            # Assigning to an existing key keeps its queue position, so a
            # light that keeps changing still goes out in its original turn
            self._pending[_key(raw, next(self._seq))] = raw
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._stopped.is_set():
                    self._cond.wait()
                if self._stopped.is_set():
                    return
                batch = []
                size = 0
                while self._pending:
                    raw = next(iter(self._pending.values()))
                    if batch and size + len(raw) > self._max_bytes:
                        break
                    self._pending.popitem(last=False)
                    batch.append(raw)
                    size += len(raw)
            for raw in batch:
                self._write(raw)
            if self._stopped.wait(self._window_s):
                return
