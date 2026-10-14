"""Manages the serial connection to the Arduino: RUN/CONFIG mode transitions,
live MIDI bridging while in RUN mode, and the config command/response framing
while in CONFIG mode. See PLAN.md for the wire protocol this implements."""

import queue
import threading
import time
from typing import Callable, Optional

import serial
import serial.tools.list_ports

import protocol

BAUD_RATE = 115200
DEFAULT_TIMEOUT_S = 5.0
RESEND_INTERVAL_S = 0.3
# Opening the port pulses DTR, auto-resetting the Mega; its bootloader stays
# in its own listening window for a bit after that and, empirically, ANY
# byte arriving during that window (not just a recognized one) keeps it
# holding, delaying handoff to the sketch well past when it'd otherwise be
# ready -- so the first send has to wait this out in silence rather than
# retry into it. 1.8s is comfortably past the observed ~1.5s handoff.
BOOT_SETTLE_S = 1.8

MonitorCallback = Callable[[str, str, bytes], None]  # (direction, kind, raw)

# Polled ~20x/s by the Test tab -- kept out of the monitor so it doesn't
# drown everything else.
_UNLOGGED_COMMANDS = {protocol.CMD_READ_PINS}


class SerialLink:
    def __init__(self, on_midi_from_device: Callable[[bytes], None],
                 on_monitor: Optional[MonitorCallback] = None,
                 on_lost: Optional[Callable[[], None]] = None):
        self._on_midi_from_device = on_midi_from_device
        self._on_monitor = on_monitor
        # Called once, from whichever thread hit the error, when the port
        # stops working without disconnect() having been called -- i.e. the
        # board was unplugged. Not called for a deliberate disconnect().
        self._on_lost = on_lost
        self._lost_lock = threading.Lock()
        self._ser: Optional[serial.Serial] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._mode = "run"  # "run" or "config"
        self._midi_parser = protocol.RunningStatusMidiParser()
        self._ack_detector = protocol.SysexAckDetector(protocol.SYSEX_CONFIG_ACK)
        self._frame_parser = protocol.FrameParser()
        self._pending_replies: "queue.Queue" = queue.Queue()
        self._write_lock = threading.Lock()
        # Serializes whole request/reply round-trips: the pin-test keepalive
        # pings from its own thread, and without this two in-flight commands
        # could each pick up the other's reply off _pending_replies.
        self._command_lock = threading.Lock()
        self._connected_at = 0.0

    @staticmethod
    def list_ports():
        return [p.device for p in serial.tools.list_ports.comports()]

    def connect(self, port: str):
        self._ser = serial.Serial(port, BAUD_RATE, timeout=0.05)
        self._connected_at = time.time()  # see BOOT_SETTLE_S in enter_config_mode()
        self._stop.clear()
        self._mode = "run"
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def disconnect(self):
        with self._lost_lock:
            self._stop.set()
        # on_lost fires on the reader thread, and its handler closes the
        # session -- which lands back here on that same thread.
        if self._reader_thread and self._reader_thread is not threading.current_thread():
            self._reader_thread.join(timeout=1.0)
        self._close_port()

    def _close_port(self):
        ser, self._ser = self._ser, None
        if ser:
            try:
                ser.close()
            except (serial.SerialException, OSError):
                pass

    def _mark_lost(self):
        """The port errored out underneath us (board unplugged). Drops it so
        `connected` goes False, and tells the owner once."""
        with self._lost_lock:
            if self._stop.is_set():
                return  # deliberate disconnect() already underway, or already lost
            self._stop.set()
        self._close_port()
        self._pending_replies.put(None)  # wake a command waiting on a reply that will never come
        if self._on_lost:
            self._on_lost()

    def _write(self, data: bytes) -> bool:
        ser = self._ser  # local: the reader thread may clear self._ser at any moment
        if ser is None:
            return False
        try:
            with self._write_lock:
                ser.write(data)
            return True
        except (serial.SerialException, OSError):
            self._mark_lost()
            return False

    @property
    def connected(self) -> bool:
        ser = self._ser
        return ser is not None and ser.is_open

    @property
    def mode(self) -> str:
        return self._mode

    def send_midi_to_device(self, raw_bytes: bytes):
        """Send a decoded MIDI message (e.g. from the DAW) to the Arduino, to
        drive light/PWM output pins. Only meaningful in RUN mode."""
        if not self.connected or self._mode != "run":
            return
        if self._write(raw_bytes):
            self._log("out", "midi", raw_bytes)

    def enter_config_mode(self, timeout=DEFAULT_TIMEOUT_S) -> bool:
        """Sends the enter-config SysEx trigger, resending it every
        RESEND_INTERVAL_S until acked or `timeout` elapses.

        The very first send is held back until BOOT_SETTLE_S after
        connect() rather than fired immediately: opening the port pulses
        DTR and auto-resets the Mega, and empirically, writing anything to
        it while its bootloader is still in its own listening window keeps
        it from ever handing off to the sketch — it just sits there mute
        until the writes stop. So this has to wait that out in silence
        first, then it's safe to resend on a short interval in case a
        single send is still somehow missed (harmless once already in
        config mode — feedConfigFrameByte ignores anything that isn't its
        own frame-start byte while waiting for one)."""
        if not self.connected:
            return False
        deadline = time.time() + timeout
        next_send = max(time.time(), self._connected_at + BOOT_SETTLE_S)
        while time.time() < deadline:
            if self._mode == "config":
                return True
            now = time.time()
            if now >= next_send:
                if not self._write(protocol.SYSEX_ENTER_CONFIG):
                    return False
                self._log("out", "ctrl", protocol.SYSEX_ENTER_CONFIG)
                next_send = now + RESEND_INTERVAL_S
            time.sleep(0.02)
        return self._mode == "config"

    def resume_run_mode(self, timeout=DEFAULT_TIMEOUT_S) -> bool:
        reply = self._send_config_command(protocol.CMD_RESUME_RUN, b"", timeout)
        if reply is not None:
            self._mode = "run"
            return True
        return False

    def ping(self, timeout=DEFAULT_TIMEOUT_S):
        reply = self._send_config_command(protocol.CMD_PING, b"", timeout)
        if reply is None:
            return None
        cmd, payload = reply
        if cmd != (protocol.CMD_PING | 0x80) or len(payload) != 3:
            return None
        return {
            "firmware_version": payload[0],
            "config_format_version": payload[1],
            "device_id": payload[2],
        }

    def set_device_id(self, new_id: int, timeout=DEFAULT_TIMEOUT_S) -> bool:
        reply = self._send_config_command(protocol.CMD_SET_DEVICE_ID, bytes([new_id & 0xFF]), timeout)
        if reply is None:
            return False
        cmd, _payload = reply
        return cmd == (protocol.CMD_SET_DEVICE_ID | 0x80)

    def test_pin(self, pin: int, level: int, timeout=DEFAULT_TIMEOUT_S) -> bool:
        reply = self._send_config_command(protocol.CMD_TEST_PIN, bytes([pin & 0xFF, level & 0xFF]), timeout)
        if reply is None:
            return False
        cmd, _payload = reply
        return cmd == (protocol.CMD_TEST_PIN | 0x80)

    def read_pins(self, timeout=DEFAULT_TIMEOUT_S):
        """Returns protocol.parse_pin_state()'s dict, or None."""
        reply = self._send_config_command(protocol.CMD_READ_PINS, b"", timeout)
        if reply is None:
            return None
        cmd, payload = reply
        if cmd != (protocol.CMD_READ_PINS | 0x80) or len(payload) != protocol.PIN_STATE_SIZE:
            return None
        return protocol.parse_pin_state(payload)

    def read_config(self, timeout=DEFAULT_TIMEOUT_S) -> Optional[bytes]:
        reply = self._send_config_command(protocol.CMD_READ_CONFIG, b"", timeout)
        if reply is None:
            return None
        cmd, payload = reply
        if cmd != (protocol.CMD_READ_CONFIG | 0x80):
            return None
        return payload

    def write_config(self, blob: bytes, timeout=DEFAULT_TIMEOUT_S) -> bool:
        reply = self._send_config_command(protocol.CMD_WRITE_CONFIG, blob, timeout)
        if reply is None:
            return False
        cmd, _payload = reply
        return cmd == (protocol.CMD_WRITE_CONFIG | 0x80)

    def _send_config_command(self, cmd: int, payload: bytes, timeout: float):
        if self._mode != "config" or not self.connected:
            return None
        frame = protocol.build_frame(cmd, payload)
        with self._command_lock:
            if not self._write(frame):
                return None
            if cmd not in _UNLOGGED_COMMANDS:
                self._log("out", "ctrl", frame)
            try:
                return self._pending_replies.get(timeout=timeout)
            except queue.Empty:
                return None

    def _wait_for_mode(self, mode: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._mode == mode:
                return True
            time.sleep(0.02)
        return False

    def _log(self, direction: str, kind: str, raw: bytes):
        if self._on_monitor:
            self._on_monitor(direction, kind, raw)

    def _read_loop(self):
        while not self._stop.is_set():
            ser = self._ser
            if not ser:
                break
            try:
                data = ser.read(256)
            except (serial.SerialException, OSError, TypeError, AttributeError):
                # TypeError/AttributeError: pyserial's posix read can trip
                # over its own fd being closed mid-call by _close_port().
                self._mark_lost()
                break
            for b in data:
                self._handle_byte(b)

    def _handle_byte(self, b: int):
        if self._mode == "run":
            if self._ack_detector.feed(b):
                self._mode = "config"
                self._log("in", "ctrl", protocol.SYSEX_CONFIG_ACK)
                return
            message = self._midi_parser.feed(b)
            if message:
                self._log("in", "midi", message)
                self._on_midi_from_device(message)
        else:
            frame = self._frame_parser.feed(b)
            if frame:
                cmd, payload = frame
                if (cmd & 0x3F) not in _UNLOGGED_COMMANDS:
                    self._log("in", "ctrl", bytes([cmd]) + payload)
                if cmd == (protocol.CMD_RESUME_RUN | 0x80):
                    self._mode = "run"
                self._pending_replies.put((cmd, payload))
