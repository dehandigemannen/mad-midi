"""Virtual MIDI bridge: connects the Arduino's serial MIDI stream to a
CoreMIDI virtual port pair so any DAW/synth software can see it.

macOS-only for now (CoreMIDI supports virtual ports natively; the same
python-rtmidi/mido call does not create virtual ports on Windows)."""

import threading
from typing import Callable, Optional

import mido

MonitorCallback = Callable[[str, "mido.Message"], None]


class MidiBridge:
    def __init__(self, on_from_daw: Callable[[bytes], None], out_name: str, in_name: str):
        """on_from_daw is called with raw MIDI bytes whenever a message
        arrives on the virtual input port, to be forwarded to the Arduino.

        out_name/in_name should be unique per device when multiple Megas are
        connected at once (e.g. "Mad Midi 3 Out" / "Mad Midi 3 In"), so each
        shows up as its own port in the DAW."""
        self._on_from_daw = on_from_daw
        self._monitor_cb: Optional[MonitorCallback] = None
        self._lock = threading.Lock()
        self._out_port = mido.open_output(out_name, virtual=True)
        self._in_port = mido.open_input(in_name, virtual=True, callback=self._handle_daw_message)

    def set_monitor_callback(self, cb: Optional[MonitorCallback]):
        self._monitor_cb = cb

    def _handle_daw_message(self, msg: "mido.Message"):
        if self._monitor_cb:
            self._monitor_cb("in", msg)
        self._on_from_daw(bytes(msg.bytes()))

    def send_to_daw(self, raw_bytes: bytes):
        """Called with a decoded MIDI message's raw bytes coming from the Arduino."""
        try:
            msg = mido.Message.from_bytes(raw_bytes)
        except (ValueError, IndexError):
            return
        with self._lock:
            self._out_port.send(msg)
        if self._monitor_cb:
            self._monitor_cb("out", msg)

    def close(self):
        self._out_port.close()
        self._in_port.close()
