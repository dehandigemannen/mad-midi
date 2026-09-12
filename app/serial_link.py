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

MonitorCallback = Callable[[str, str, bytes], None]  # (direction, kind, raw)


class SerialLink:
    def __init__(self, on_midi_from_device: Callable[[bytes], None],
                 on_monitor: Optional[MonitorCallback] = None):
        self._on_midi_from_device = on_midi_from_device
        self._on_monitor = on_monitor
        self._ser: Optional[serial.Serial] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._mode = "run"  # "run" or "config"
        self._midi_parser = protocol.RunningStatusMidiParser()
        self._ack_detector = protocol.SysexAckDetector(protocol.SYSEX_CONFIG_ACK)
        self._frame_parser = protocol.FrameParser()
        self._pending_replies: "queue.Queue" = queue.Queue()
        self._write_lock = threading.Lock()

    @staticmethod
    def list_ports():
        return [p.device for p in serial.tools.list_ports.comports()]

    def connect(self, port: str):
        self._ser = serial.Serial(port, BAUD_RATE, timeout=0.05)
        self._stop.clear()
        self._mode = "run"
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def disconnect(self):
        self._stop.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=1.0)
        if self._ser:
            self._ser.close()
        self._ser = None

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    @property
    def mode(self) -> str:
        return self._mode

    def send_midi_to_device(self, raw_bytes: bytes):
        """Send a decoded MIDI message (e.g. from the DAW) to the Arduino, to
        drive light/PWM output pins. Only meaningful in RUN mode."""
        if not self.connected or self._mode != "run":
            return
        with self._write_lock:
            self._ser.write(raw_bytes)
        self._log("out", "midi", raw_bytes)

    def enter_config_mode(self, timeout=DEFAULT_TIMEOUT_S) -> bool:
        if not self.connected:
            return False
        with self._write_lock:
            self._ser.write(protocol.SYSEX_ENTER_CONFIG)
        self._log("out", "ctrl", protocol.SYSEX_ENTER_CONFIG)
        return self._wait_for_mode("config", timeout)

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
        with self._write_lock:
            self._ser.write(frame)
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
            if not self._ser:
                break
            try:
                data = self._ser.read(256)
            except serial.SerialException:
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
                self._log("in", "ctrl", bytes([cmd]) + payload)
                if cmd == (protocol.CMD_RESUME_RUN | 0x80):
                    self._mode = "run"
                self._pending_replies.put((cmd, payload))
