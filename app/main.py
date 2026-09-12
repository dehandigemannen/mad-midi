"""pywebview entrypoint: manages one or more connected Mega devices (each its
own serial link + virtual MIDI bridge) and exposes an Api class as
window.pywebview.api to the web UI. See PLAN.md for the wire protocol and
multi-device design this implements."""

import json
import os
import uuid
from pathlib import Path
from typing import Dict, Optional

import webview

import config_model
import flasher
import protocol
import serial_link
from midi_bridge import MidiBridge
from sequencer import SequencerEngine

UI_DIR = Path(__file__).parent / "ui"

_OUTPUT_ROLES = ("light_digital", "light_pwm")
_INPUT_ROLES = ("button_note", "button_cc", "encoder_a", "pot_cc")


def _virtual_port_names(port: str, device_id: int):
    label = f"Mad Midi {device_id}" if device_id else f"Mad Midi ({os.path.basename(port)})"
    return f"{label} Out", f"{label} In"


def _pins_equal(a, b) -> bool:
    if a is None or b is None or len(a) != len(b):
        return False
    return all(config_model.PinConfig.equal(x, y) for x, y in zip(a, b))


class DeviceSession:
    def __init__(self, port: str, api: "Api"):
        self.port = port
        self.device_id = 0
        self.fw_version = None
        self.config_format_version = None
        self.active_pins = None  # list[config_model.PinConfig] once known
        self.baseline_pins = None  # snapshot of what's actually flashed to the device
        self.sequencer: Optional[SequencerEngine] = None

        self._api = api
        self.link = serial_link.SerialLink(
            on_midi_from_device=self._on_device_midi,
            on_monitor=self._on_monitor,
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

    def rebuild_sequencer(self, sequencer_config: config_model.SequencerConfig, pins):
        if self.sequencer:
            self.sequencer.close()
        self.sequencer = SequencerEngine(
            sequencer_config,
            pins,
            send_to_daw=self.bridge.send_to_daw,
            send_to_device=self.link.send_midi_to_device,
            on_state_change=self._on_sequencer_state,
        )

    def close(self):
        if self.sequencer:
            self.sequencer.close()
        self.link.disconnect()
        self.bridge.close()

    def _on_device_midi(self, raw: bytes):
        if self.sequencer and len(raw) >= 3:
            channel = raw[0] & 0x0F
            if self.sequencer.handle_incoming_pin_message(channel, raw[1]):
                return
        self.bridge.send_to_daw(raw)

    def _on_daw_message(self, raw: bytes):
        self.link.send_midi_to_device(raw)

    def _on_realtime(self, kind: str):
        if self.sequencer:
            self.sequencer.handle_realtime(kind)

    def _on_sequencer_state(self, state: dict):
        self._api._emit("sequencer_state", {"device_id": self.device_id, **state})

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


class Api:
    def __init__(self):
        self._window: Optional[webview.Window] = None
        self._devices: Dict[str, DeviceSession] = {}  # port -> session, only while connected
        self._device_ports: Dict[int, str] = {}  # device_id -> port, only while connected
        config_model.migrate_legacy_presets()

    def set_window(self, window):
        self._window = window

    def _session_for_device(self, device_id: int) -> Optional[DeviceSession]:
        port = self._device_ports.get(device_id)
        return self._devices.get(port) if port else None

    def _entry_status(self, entry: config_model.Entry):
        result = {"id": entry.id}
        if entry.entry_type == "device":
            session = self._session_for_device(entry.device_id)
            connected = bool(session and session.link.connected)
            result["connected"] = connected
            result["in_sync"] = (
                _pins_equal(session.baseline_pins, entry.pins)
                if connected and session.baseline_pins is not None
                else None
            )
        return result

    # ---- sidebar entries (devices + drafts) -------------------------------

    def list_entries(self):
        entries = []
        for id_ in config_model.Entry.list_ids():
            try:
                entry = config_model.Entry.load(id_)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            info = {
                "id": entry.id,
                "entry_type": entry.entry_type,
                "nickname": entry.nickname,
                "device_id": entry.device_id,
            }
            info.update(self._entry_status(entry))
            entries.append(info)
        return entries

    def get_entry(self, id_: str):
        return config_model.Entry.load(id_).to_dict()

    def create_draft(self, nickname: str):
        draft_id = f"draft-{uuid.uuid4().hex[:8]}"
        entry = config_model.Entry.blank(draft_id, "draft", nickname.strip() or "Untitled draft")
        entry.save()
        return entry.to_dict()

    def rename_entry(self, id_: str, nickname: str):
        entry = config_model.Entry.load(id_)
        entry.nickname = nickname.strip() or entry.nickname
        entry.save()
        return entry.to_dict()

    def remove_entry(self, id_: str):
        entry = config_model.Entry.load(id_)
        if entry.entry_type == "device":
            port = self._device_ports.pop(entry.device_id, None)
            session = self._devices.pop(port, None) if port else None
            if session:
                session.close()
        config_model.Entry.delete(id_)
        return True

    def update_entry_pins(self, id_: str, pins_dicts):
        """Debounced auto-save call fired from the UI on every edit."""
        entry = config_model.Entry.load(id_)
        entry.pins = [config_model.PinConfig.from_dict(p) for p in pins_dicts]
        entry.save()
        session = self._session_for_device(entry.device_id) if entry.entry_type == "device" else None
        if session:
            session.active_pins = entry.pins
            session.rebuild_sequencer(entry.sequencer, entry.pins)
        return self._entry_status(entry)

    def update_entry_sequencer(self, id_: str, sequencer_dict):
        """Debounced auto-save call fired from the Sequencer tab on every edit."""
        entry = config_model.Entry.load(id_)
        entry.sequencer = config_model.SequencerConfig.from_dict(sequencer_dict)
        entry.save()
        session = self._session_for_device(entry.device_id) if entry.entry_type == "device" else None
        if session:
            session.rebuild_sequencer(entry.sequencer, entry.pins)
        return self._entry_status(entry)

    def toggle_step_enabled(self, id_: str, step_index: int):
        """Software fallback for a step's on/off toggle (no physical button
        required) — mirrors what pressing the step's button pin does."""
        entry = config_model.Entry.load(id_)
        session = self._session_for_device(entry.device_id) if entry.entry_type == "device" else None
        if session and session.sequencer:
            session.sequencer.set_step_enabled(
                step_index, not session.sequencer.step_enabled[step_index]
            )
        return True

    def apply_draft(self, draft_id: str, device_entry_id: str):
        draft = config_model.Entry.load(draft_id)
        device_entry = config_model.Entry.load(device_entry_id)
        device_entry.pins = draft.pins
        device_entry.sequencer = draft.sequencer
        device_entry.save()
        session = self._session_for_device(device_entry.device_id)
        if session:
            session.active_pins = device_entry.pins
            session.rebuild_sequencer(device_entry.sequencer, device_entry.pins)
        return self._entry_status(device_entry)

    # ---- device adoption ----------------------------------------------

    def list_ports(self):
        connected_ports = set(self._devices.keys())
        return [p for p in serial_link.SerialLink.list_ports() if p not in connected_ports]

    def _next_free_device_id(self) -> int:
        used = {int(i) for i in config_model.Entry.list_ids() if i.isdigit()}
        for candidate in range(1, 256):
            if candidate not in used:
                return candidate
        return 0

    def adopt_port(self, port: str):
        """Connect a freshly-plugged board: identify it, auto-assign a
        device id if it doesn't have one yet, and attach a live session.

        If this device_id was never adopted before, its saved buffer is
        seeded from whatever's currently on its EEPROM (there's nothing
        else to seed it from). If it's already a known entry (e.g. it was
        adopted from a different port previously), its saved buffer —
        including any edits made while it was offline — is left untouched;
        only the in-memory sync baseline is (re)established from the
        device's actual EEPROM contents, same as reconnect_entry."""
        session = DeviceSession(port, self)
        session.identify()

        if session.device_id == 0:
            new_id = self._next_free_device_id()
            if session.link.enter_config_mode():
                if session.link.set_device_id(new_id):
                    session.device_id = new_id
                    session._rebuild_bridge()
                session.link.resume_run_mode()

        device_id = session.device_id
        if device_id == 0:
            session.close()
            return None

        id_ = str(device_id)
        self._devices[port] = session
        self._device_ports[device_id] = port

        blob = None
        if session.link.enter_config_mode():
            blob = session.link.read_config()
            session.link.resume_run_mode()

        if config_model.Entry.exists(id_):
            entry = config_model.Entry.load(id_)
        else:
            nickname = f"Device {device_id}"
            entry = (
                config_model.Entry.from_blob(id_, nickname, blob)
                if blob is not None
                else config_model.Entry.blank(id_, "device", nickname, device_id=device_id)
            )
            entry.save()

        session.baseline_pins = (
            config_model.Entry.from_blob(id_, entry.nickname, blob).pins if blob is not None else None
        )
        session.active_pins = entry.pins
        session.rebuild_sequencer(entry.sequencer, entry.pins)
        return entry.to_dict()

    def reconnect_entry(self, id_: str, port: str):
        """Re-establish a live connection for an adopted device that's back
        online on `port`, verifying it's actually the same board. Only
        refreshes the in-memory sync baseline from the device's actual
        EEPROM contents — the saved buffer (and any edits made while it was
        offline) is left untouched; use "Read from device" to pull the
        device's contents into the buffer explicitly."""
        entry = config_model.Entry.load(id_)
        if entry.entry_type != "device":
            return {"ok": False, "error": "not a device entry"}

        session = DeviceSession(port, self)
        session.identify()
        if session.device_id != entry.device_id:
            session.close()
            return {"ok": False, "error": "the device on that port has a different id"}

        self._devices[port] = session
        self._device_ports[entry.device_id] = port

        blob = None
        if session.link.enter_config_mode():
            blob = session.link.read_config()
            session.link.resume_run_mode()
        session.baseline_pins = (
            config_model.Entry.from_blob(id_, entry.nickname, blob).pins if blob is not None else None
        )
        session.active_pins = entry.pins
        session.rebuild_sequencer(entry.sequencer, entry.pins)
        return {"ok": True, **self._entry_status(entry)}

    def disconnect_entry(self, id_: str):
        entry = config_model.Entry.load(id_)
        port = self._device_ports.pop(entry.device_id, None)
        session = self._devices.pop(port, None) if port else None
        if session:
            session.close()
        return True

    # ---- config mode read/write ----------------------------------------

    def write_device_config(self, id_: str):
        entry = config_model.Entry.load(id_)
        session = self._session_for_device(entry.device_id)
        if not session or not session.link.connected:
            return False
        if not session.link.enter_config_mode():
            return False
        ok = session.link.write_config(entry.to_blob())
        session.link.resume_run_mode()
        if ok:
            session.baseline_pins = entry.pins
            session.active_pins = entry.pins
        return ok

    def read_device_config(self, id_: str):
        entry = config_model.Entry.load(id_)
        session = self._session_for_device(entry.device_id)
        if not session or not session.link.connected:
            return None
        if not session.link.enter_config_mode():
            return None
        blob = session.link.read_config()
        session.link.resume_run_mode()
        if blob is None:
            return None
        fresh = config_model.Entry.from_blob(id_, entry.nickname, blob)
        fresh.sequencer = entry.sequencer  # not part of the EEPROM blob — preserve it
        session.baseline_pins = fresh.pins
        session.active_pins = fresh.pins
        session.rebuild_sequencer(fresh.sequencer, fresh.pins)
        fresh.save()
        return fresh.to_dict()

    # ---- flashing ----------------------------------------------------

    def flash_firmware(self, id_: str):
        entry = config_model.Entry.load(id_)
        session = self._session_for_device(entry.device_id)
        if not session:
            return {"ok": False, "output": "device is not connected"}
        try:
            output = flasher.flash(session.port)
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
