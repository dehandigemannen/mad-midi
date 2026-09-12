"""pywebview entrypoint: manages one or more connected Mega devices (each its
own serial link + virtual MIDI bridge) and exposes an Api class as
window.pywebview.api to the web UI. See PLAN.md for the wire protocol and
multi-device design this implements."""

import json
import os
from pathlib import Path
from typing import Dict, Optional

import webview

import config_model
import flasher
import protocol
import serial_link
from midi_bridge import MidiBridge

UI_DIR = Path(__file__).parent / "ui"

_OUTPUT_ROLES = ("light_digital", "light_pwm")
_INPUT_ROLES = ("button_note", "button_cc", "encoder_a", "pot_cc")


def _virtual_port_names(port: str, device_id: int):
    label = f"Mad Midi {device_id}" if device_id else f"Mad Midi ({os.path.basename(port)})"
    return f"{label} Out", f"{label} In"


class DeviceSession:
    def __init__(self, port: str, api: "Api"):
        self.port = port
        self.device_id = 0
        self.fw_version = None
        self.config_format_version = None
        self.active_pins = None  # list[config_model.PinConfig] once known

        self._api = api
        self.link = serial_link.SerialLink(
            on_midi_from_device=self._on_device_midi,
            on_monitor=self._on_monitor,
        )
        self.link.connect(port)
        out_name, in_name = _virtual_port_names(port, self.device_id)
        self.bridge = MidiBridge(on_from_daw=self._on_daw_message, out_name=out_name, in_name=in_name)

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
        self.bridge = MidiBridge(on_from_daw=self._on_daw_message, out_name=out_name, in_name=in_name)

    def close(self):
        self.link.disconnect()
        self.bridge.close()

    def status(self):
        return {
            "port": self.port,
            "connected": self.link.connected,
            "mode": self.link.mode,
            "device_id": self.device_id,
            "fw_version": self.fw_version,
            "config_format_version": self.config_format_version,
        }

    def _on_device_midi(self, raw: bytes):
        self.bridge.send_to_daw(raw)

    def _on_daw_message(self, raw: bytes):
        self.link.send_midi_to_device(raw)

    def _on_monitor(self, direction, kind, raw: bytes):
        self._api._emit("monitor_event", {
            "port": self.port,
            "device_id": self.device_id,
            "direction": direction,
            "kind": kind,
            "raw": list(raw),
            "decoded": _decode_for_display(kind, raw),
        })
        if kind == "midi":
            matches = self._match_pins(direction, raw)
            if matches:
                self._api._emit("pin_activity", {"port": self.port, "direction": direction, "pins": matches})

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


class Api:
    def __init__(self):
        self._window: Optional[webview.Window] = None
        self._devices: Dict[str, DeviceSession] = {}

    def set_window(self, window):
        self._window = window

    # ---- device lifecycle -----------------------------------------------

    def list_ports(self):
        return serial_link.SerialLink.list_ports()

    def list_devices(self):
        return [d.status() for d in self._devices.values()]

    def connect(self, port: str):
        if port in self._devices:
            return self._devices[port].status()
        session = DeviceSession(port, self)
        self._devices[port] = session
        session.identify()
        return session.status()

    def disconnect(self, port: str):
        session = self._devices.pop(port, None)
        if session:
            session.close()
        return {"connected": False, "port": port}

    def get_status(self, port: str):
        session = self._devices.get(port)
        return session.status() if session else {"connected": False, "port": port}

    def assign_device_id(self, port: str, new_id: int):
        session = self._devices.get(port)
        if not session:
            return False
        if not session.link.enter_config_mode():
            return False
        ok = session.link.set_device_id(int(new_id) & 0xFF)
        if ok:
            session.device_id = int(new_id) & 0xFF
            session._rebuild_bridge()
        session.link.resume_run_mode()
        return ok

    # ---- config mode ----------------------------------------------------

    def enter_config(self, port: str):
        session = self._devices.get(port)
        return bool(session and session.link.enter_config_mode())

    def resume_run(self, port: str):
        session = self._devices.get(port)
        return bool(session and session.link.resume_run_mode())

    def read_device_config(self, port: str):
        session = self._devices.get(port)
        if not session:
            return None
        blob = session.link.read_config()
        if blob is None:
            return None
        preset = config_model.Preset.from_blob("device", blob)
        session.active_pins = preset.pins
        session.device_id = preset.device_id
        return preset.to_dict()

    def write_device_config(self, port: str, preset_dict):
        session = self._devices.get(port)
        if not session:
            return False
        preset = config_model.Preset.from_dict(preset_dict)
        preset.device_id = session.device_id  # pin config writes never change device id
        ok = session.link.write_config(preset.to_blob())
        if ok:
            session.active_pins = preset.pins
        return ok

    # ---- presets ----------------------------------------------------

    def list_presets(self):
        return config_model.Preset.list_presets()

    def save_preset(self, preset_dict):
        preset = config_model.Preset.from_dict(preset_dict)
        return str(preset.save())

    def load_preset(self, name):
        return config_model.Preset.load(name).to_dict()

    def new_preset(self, name):
        return config_model.Preset.blank(name).to_dict()

    # ---- flashing ----------------------------------------------------

    def flash_firmware(self, port: str):
        try:
            output = flasher.flash(port)
            return {"ok": True, "output": output}
        except flasher.FlashError as exc:
            return {"ok": False, "output": str(exc)}

    # ---- event plumbing ----------------------------------------------------

    def _emit(self, event, payload):
        if not self._window:
            return
        js = f"window.dispatchEvent(new CustomEvent({json.dumps(event)}, {{detail: {json.dumps(payload)}}}))"
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass


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


def main():
    api = Api()
    window = webview.create_window(
        "Mad Midi",
        str(UI_DIR / "index.html"),
        js_api=api,
        width=1280,
        height=860,
        min_size=(900, 600),
    )
    api.set_window(window)
    webview.start()


if __name__ == "__main__":
    main()
