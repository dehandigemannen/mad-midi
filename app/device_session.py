"""Per-connected-device session: owns the serial link + virtual MIDI bridge
for one Arduino Mega, plus the sequencer engine driven off it, and the
low-level helpers only a session needs (virtual port naming, monitor
decoding, pin-state matching). See PLAN.md for the wire protocol and
multi-device design this implements."""

import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Optional

import config_model
import protocol
import serial_link
from drone import DroneEngine
from fx_rack import FxRackEngine
from midi_bridge import MidiBridge
from sequencer import SequencerEngine
from value_mirror import ValueMirrorEngine

if TYPE_CHECKING:
    from api import Api


def _log(msg: str):
    """Temporary diagnostic logging for the flash/reconnect hang investigation
    — cheap since these are all low-frequency lifecycle events, not anything
    in a hot loop. Goes to stderr so it's easy to separate from anything
    else."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# How often the Test tab's live pin state is polled. Each poll also counts as
# config-mode activity, so it doubles as the keepalive against the
# firmware's 15s CONFIG_TIMEOUT_MS (mode.cpp) -- but if the app dies
# mid-test the device still times out and restores its pins itself.
PIN_TEST_POLL_S = 0.05

_PIN_TEST_LEVELS = {
    "low": protocol.PIN_TEST_LOW,
    "high": protocol.PIN_TEST_HIGH,
    "release": protocol.PIN_TEST_RELEASE,
    "input": protocol.PIN_TEST_INPUT,
}

_OUTPUT_ROLES = ("light_digital", "light_pwm")
_INPUT_ROLES = ("button_note", "button_cc", "encoder_a", "pot_cc")


def _virtual_port_names(port: str, device_id: int):
    label = f"Mad Midi {device_id}" if device_id else f"Mad Midi ({os.path.basename(port)})"
    return f"{label} Out", f"{label} In"


def _pins_equal(a, b) -> bool:
    if a is None or b is None or len(a) != len(b):
        return False
    return all(config_model.PinConfig.equal(x, y) for x, y in zip(a, b))


def _decode_for_display(kind, raw: bytes):
    if kind != "midi" or len(raw) < 3:
        return raw.hex(" ") if raw else ""
    status, d1, d2 = raw[0], raw[1], raw[2]
    msg_type = status & 0xF0
    channel = (status & 0x0F) + 1
    if msg_type == 0x90:
        return f"Ch{channel} Note {'On' if d2 > 0 else 'Off'} {d1} vel={d2}"
    if msg_type == 0x80:
        return f"Ch{channel} Note Off {d1}"
    if msg_type == 0xB0:
        return f"Ch{channel} CC {d1} val={d2}"
    return raw.hex(" ")


class DeviceSession:
    def __init__(self, port: str, api: "Api"):
        self.port = port
        self.device_id = 0
        self.fw_version = None
        self.config_format_version = None
        self.active_pins = None  # list[config_model.PinConfig] once known
        self.baseline_pins = None  # snapshot of what's actually flashed to the device
        self.value_mirror: Optional[ValueMirrorEngine] = None
        self.sequencer: Optional[SequencerEngine] = None
        self.drone: Optional[DroneEngine] = None
        self.fx_rack: Optional[FxRackEngine] = None
        self._pin_test_stop: Optional[threading.Event] = None  # set while a pin test holds config mode

        self._api = api
        self.link = serial_link.SerialLink(
            on_midi_from_device=self._on_device_midi,
            on_monitor=self._on_monitor,
            on_lost=self._on_link_lost,
        )
        self.link.connect(port)
        out_name, in_name = _virtual_port_names(port, self.device_id)
        self.bridge = MidiBridge(
            on_from_daw=self._on_daw_message,
            on_realtime=self._on_realtime,
            out_name=out_name,
            in_name=in_name,
        )

    def identify(self):
        """Briefly enters config mode to read device id/versions, then resumes."""
        if not self.link.enter_config_mode():
            return
        info = self.link.ping()
        if info:
            self.device_id = info["device_id"]
            self.fw_version = info["firmware_version"]
            self.config_format_version = info["config_format_version"]
            self._rebuild_bridge()
        self.link.resume_run_mode()

    def _rebuild_bridge(self):
        self.bridge.close()
        out_name, in_name = _virtual_port_names(self.port, self.device_id)
        self.bridge = MidiBridge(
            on_from_daw=self._on_daw_message,
            on_realtime=self._on_realtime,
            out_name=out_name,
            in_name=in_name,
        )

    def rebuild_engines(self, entry: "config_model.Entry"):
        old_sequencer = self.sequencer
        if self.drone:
            self.drone.close()
        if self.fx_rack:
            self.fx_rack.close()

        # Unconditional and unaffected by any feature's enabled flag -- any
        # encoder_a/pot_cc pin with mirror_pin set gets its VU meter driven
        # regardless of whether it belongs to a plain Table/Board mapping, a
        # disabled Sequencer/Drone, or nothing composite at all.
        self.value_mirror = ValueMirrorEngine(entry.pins, send_to_device=self.link.send_midi_to_device)
        self.sequencer = (
            SequencerEngine(
                entry.sequencer,
                entry.pins,
                send_to_daw=self.bridge.send_to_daw,
                send_to_device=self.link.send_midi_to_device,
                on_state_change=self._on_sequencer_state,
            )
            if entry.sequencer.enabled
            else None
        )
        if old_sequencer:
            if self.sequencer:
                self.sequencer.adopt_runtime_state(old_sequencer)
            old_sequencer.close()
        self.drone = (
            DroneEngine(entry.drone, entry.pins, on_state_change=self._on_drone_state)
            if entry.drone.enabled
            else None
        )
        self.fx_rack = (
            FxRackEngine(entry.fx_rack, entry.pins, on_state_change=self._on_fx_state)
            if entry.fx_rack.enabled
            else None
        )

    # ---- pin test (Test tab) ---------------------------------------------
    #
    # Holds the device in config mode for the whole test session: the
    # firmware only honors CMD_TEST_PIN there (so no MIDI roles fight the
    # driven pins), and restores every tested pin to its role on the way
    # back to RUN mode.

    @property
    def pin_testing(self) -> bool:
        return self._pin_test_stop is not None

    def start_pin_test(self) -> Optional[str]:
        """Returns an error message, or None on success."""
        if self.pin_testing:
            return None
        if (self.fw_version or 0) < protocol.PIN_TEST_MIN_FW_VERSION:
            return "this device's firmware predates pin testing; flash firmware first"
        if not self.link.enter_config_mode():
            return "couldn't put the device into config mode"
        stop = threading.Event()
        self._pin_test_stop = stop
        threading.Thread(target=self._pin_test_poll, args=(stop,), daemon=True).start()
        return None

    def _pin_test_poll(self, stop: threading.Event):
        last = None
        while not stop.wait(PIN_TEST_POLL_S):
            pin_state = self.link.read_pins(timeout=1.0)
            if pin_state is None or pin_state == last or stop.is_set():
                continue
            last = pin_state
            self._api._emit("pin_test_state", {"device_id": self.device_id, **pin_state})

    def stop_pin_test(self):
        if not self._pin_test_stop:
            return
        self._pin_test_stop.set()
        self._pin_test_stop = None
        self.link.resume_run_mode()

    def test_pin(self, pin: int, level: str) -> bool:
        if not self.pin_testing or level not in _PIN_TEST_LEVELS:
            return False
        return self.link.test_pin(pin, _PIN_TEST_LEVELS[level])

    def close(self):
        self.stop_pin_test()
        if self.sequencer:
            self.sequencer.close()
        if self.drone:
            self.drone.close()
        if self.fx_rack:
            self.fx_rack.close()
        self.link.disconnect()
        self.bridge.close()

    def _on_device_midi(self, raw: bytes):
        if len(raw) >= 3:
            channel = raw[0] & 0x0F
            d1, d2 = raw[1], raw[2]
            if self.value_mirror:
                self.value_mirror.handle_incoming(channel, d1, d2)
            if self.drone and self.drone.handle_incoming_pin_message(channel, d1, d2):
                return
            if self.fx_rack and self.fx_rack.handle_incoming_pin_message(channel, d1, d2):
                return
            if self.sequencer and self.sequencer.handle_incoming_pin_message(channel, d1, d2):
                return
        self.bridge.send_to_daw(raw)

    def _on_link_lost(self):
        _log(f"device {self.device_id} on {self.port}: serial link lost (unplugged?)")
        self._api._on_session_lost(self)

    def _on_daw_message(self, raw: bytes):
        self.link.send_midi_to_device(raw)

    def _on_realtime(self, kind: str):
        if self.sequencer:
            self.sequencer.handle_realtime(kind)

    def _on_sequencer_state(self, state: dict):
        self._api._emit("sequencer_state", {"device_id": self.device_id, **state})

    def _on_drone_state(self, enabled):
        self._api._emit("drone_state", {"device_id": self.device_id, "enabled": enabled})

    def _on_fx_state(self, enabled):
        self._api._emit("fx_state", {"device_id": self.device_id, "enabled": enabled})

    def _on_monitor(self, direction, kind, raw: bytes):
        self._api._emit("monitor_event", {
            "device_id": self.device_id,
            "direction": direction,
            "kind": kind,
            "raw": list(raw),
            "decoded": _decode_for_display(kind, raw),
        })
        if kind == "midi":
            matches = self._match_pins(direction, raw)
            if matches:
                self._api._emit("pin_activity", {"device_id": self.device_id, "direction": direction, "pins": matches})

    def _match_pins(self, direction, raw: bytes):
        if not self.active_pins or len(raw) < 3:
            return []
        status, d1, d2 = raw[0], raw[1], raw[2]
        msg_type = status & 0xF0
        channel = status & 0x0F
        roles = _OUTPUT_ROLES if direction == "out" else _INPUT_ROLES
        matches = []
        for p in self.active_pins:
            if p.role not in roles or p.channel != channel or p.number != d1:
                continue
            value = d2 if msg_type != 0x80 else 0
            matches.append({"pin": p.pin, "value": value})
        return matches
