"""pywebview entrypoint: manages one or more connected Mega devices (each its
own serial link + virtual MIDI bridge) and exposes an Api class as
window.pywebview.api to the web UI. See PLAN.md for the wire protocol and
multi-device design this implements."""

import json
import os
import sys
import threading
import time
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


def _log(msg: str):
    """Temporary diagnostic logging for the flash/reconnect hang investigation
    — cheap since these are all low-frequency lifecycle events, not anything
    in a hot loop. Goes to stderr so it's easy to separate from anything
    else."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

_OUTPUT_ROLES = ("light_digital", "light_pwm")
_INPUT_ROLES = ("button_note", "button_cc", "encoder_a", "pot_cc")

AUTO_POLL_INTERVAL_S = 4.0  # how often the background poller checks for known devices coming back online
AUTO_PROBE_COOLDOWN_S = 10.0  # don't retry a port that just failed to identify more than once per this long
AUTO_PORT_HINT = "usbmodem"  # macOS-only for now (see README) — Mega's native USB CDC port always matches this


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
            if self.sequencer.handle_incoming_pin_message(channel, raw[1], raw[2]):
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
        self._lock = threading.Lock()  # guards _devices/_device_ports against the auto-reconnect thread
        self._auto_probe_failed: Dict[str, float] = {}  # port -> time.time() of its last failed identify
        self._busy_ports: set = set()  # ports mid-flash via flash_firmware()/flash_port(), off-limits to the auto poller
        config_model.migrate_legacy_presets()
        threading.Thread(target=self._auto_reconnect_loop, daemon=True).start()
        _log("Api initialized, auto-reconnect poller thread started")

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

    def update_entry_pins(self, id_: str, pins_dicts, sequencer_dict=None):
        """Debounced auto-save call fired from the UI on every edit — pins
        and sequencer are always saved together in one read-modify-write, so
        two independent debounced saves can never race and corrupt the file
        (they used to: assigning a sequencer pin fired both this call and a
        separate update_entry_sequencer call ~400ms apart, and two concurrent
        non-atomic writes to the same file could interleave)."""
        entry = config_model.Entry.load(id_)
        entry.pins = [config_model.PinConfig.from_dict(p) for p in pins_dicts]
        if sequencer_dict is not None:
            entry.sequencer = config_model.SequencerConfig.from_dict(sequencer_dict)
        entry.save()
        session = self._session_for_device(entry.device_id) if entry.entry_type == "device" else None
        if session:
            session.active_pins = entry.pins
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
            found = session.device_id if session.device_id else "unassigned"
            session.close()
            return {
                "ok": False,
                "error": f"the device on that port has a different id (expected {entry.device_id}, found {found})",
            }

        with self._lock:
            self._attach_session(entry, session)
        return {"ok": True, **self._entry_status(entry)}

    def _attach_session(self, entry: "config_model.Entry", session: "DeviceSession"):
        """Hooks up an already-identified session (session.device_id ==
        entry.device_id confirmed by the caller) as the live connection for
        `entry`. Shared by the manual reconnect_entry() call and the
        background auto-reconnect poller. Caller holds self._lock."""
        self._devices[session.port] = session
        self._device_ports[entry.device_id] = session.port

        blob = None
        if session.link.enter_config_mode():
            blob = session.link.read_config()
            session.link.resume_run_mode()
        session.baseline_pins = (
            config_model.Entry.from_blob(entry.id, entry.nickname, blob).pins if blob is not None else None
        )
        session.active_pins = entry.pins
        session.rebuild_sequencer(entry.sequencer, entry.pins)

    # ---- background auto-reconnect --------------------------------------
    #
    # Polls for previously-adopted devices coming back online (replugged, or
    # the app was started after they were already plugged in) and hooks them
    # up automatically, without the user having to click Connect and pick
    # the port again. Only ever touches ports that look like an Arduino
    # (AUTO_PORT_HINT) and only when there's at least one known device
    # that's currently offline — it never probes when there's nothing to
    # look for, and it never auto-*adopts* a new/unassigned board (that
    # still requires an explicit "+ Device" click), since silently handing
    # out a device id to whatever happens to be plugged in would be a much
    # bigger surprise than silently reconnecting a board the user already
    # set up on purpose.

    def _auto_reconnect_tick(self):
        with self._lock:
            connected_ports = set(self._devices.keys())
            busy_ports = set(self._busy_ports)
            offline_ids = {
                int(i) for i in config_model.Entry.list_ids()
                if i.isdigit() and int(i) not in self._device_ports
            }
        if not offline_ids:
            return

        now = time.time()
        for port in serial_link.SerialLink.list_ports():
            if AUTO_PORT_HINT not in port:
                continue
            if port in connected_ports or port in busy_ports:
                continue
            if now - self._auto_probe_failed.get(port, 0) < AUTO_PROBE_COOLDOWN_S:
                continue

            _log(f"auto-reconnect: probing {port} for offline ids {offline_ids}")
            try:
                session = DeviceSession(port, self)
                session.identify()
            except Exception as exc:
                _log(f"auto-reconnect: probe of {port} raised {exc!r}")
                self._auto_probe_failed[port] = now
                continue

            if session.device_id and session.device_id in offline_ids:
                _log(f"auto-reconnect: {port} matched offline device {session.device_id}, attaching")
                entry = config_model.Entry.load(str(session.device_id))
                with self._lock:
                    self._attach_session(entry, session)
                _log(f"auto-reconnect: device {session.device_id} attached")
                # no push event needed — the UI already polls list_entries()
                # on a timer and will pick up the new connected state there
                offline_ids.discard(session.device_id)
            else:
                _log(f"auto-reconnect: {port} did not match (device_id={session.device_id}), cooling down")
                self._auto_probe_failed[port] = now
                session.close()

    def _auto_reconnect_loop(self):
        while True:
            time.sleep(AUTO_POLL_INTERVAL_S)
            try:
                self._auto_reconnect_tick()
            except Exception:
                pass  # a background poller must never take the app down

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
        """Flashes the connected device, then reconnects it and writes its
        current saved config straight back — so it comes back up already
        playable instead of left on whatever happened to survive in EEPROM.

        avrdude needs exclusive access to the port for its bootloader
        handshake, and our own SerialLink holding it open blocks that
        indefinitely (confirmed empirically — avrdude just hangs until its
        own 60s subprocess timeout), so the port has to be fully released
        first. self._lock is only held briefly around the dict mutations,
        NOT across the multi-second avrdude subprocess or the serial
        round-trips after — holding a lock across seconds of blocking I/O
        needlessly stalls every other Api call trying to touch device
        state for that whole window. self._busy_ports covers the same race
        the long lock hold used to (stopping the auto-reconnect poller from
        grabbing this port mid-flash) without needing the lock held that
        long.

        Every step below is wrapped so nothing can escape this method as an
        uncaught exception: pywebview has nothing to catch it at the JS
        bridge boundary, and an unhandled exception there can leave the
        calling JS promise unresolved forever — i.e. the button just looks
        permanently hung instead of showing an error."""
        _log(f"flash_firmware({id_}): called")
        entry = config_model.Entry.load(id_)
        with self._lock:
            session = self._session_for_device(entry.device_id)
            if not session:
                _log(f"flash_firmware({id_}): no live session, aborting")
                return {"ok": False, "output": "device is not connected"}
            port = session.port
            self._devices.pop(port, None)
            self._device_ports.pop(entry.device_id, None)
            self._busy_ports.add(port)
        _log(f"flash_firmware({id_}): closing existing session on {port}")
        session.close()

        try:
            try:
                _log(f"flash_firmware({id_}): invoking avrdude on {port}")
                output = flasher.flash(port)
                _log(f"flash_firmware({id_}): avrdude returned OK")
            except flasher.FlashError as exc:
                _log(f"flash_firmware({id_}): avrdude FAILED: {exc}")
                return {"ok": False, "output": str(exc)}

            try:
                _log(f"flash_firmware({id_}): reopening {port}")
                new_session = DeviceSession(port, self)
                _log(f"flash_firmware({id_}): port reopened, calling identify()")
                new_session.identify()
                _log(f"flash_firmware({id_}): identify() returned, device_id={new_session.device_id}")
            except Exception as exc:
                _log(f"flash_firmware({id_}): reopen/identify raised {exc!r}")
                return {
                    "ok": True,
                    "output": output,
                    "reconnect_error": f"flashed, but couldn't reopen the port afterward ({exc}) — reconnect manually",
                }

            if new_session.device_id != entry.device_id:
                found = new_session.device_id if new_session.device_id else "unassigned"
                _log(f"flash_firmware({id_}): id mismatch after flash (expected {entry.device_id}, found {found})")
                new_session.close()
                return {
                    "ok": True,
                    "output": output,
                    "reconnect_error": (
                        f"flashed, but couldn't reconnect afterward "
                        f"(expected id {entry.device_id}, found {found}) — reconnect manually"
                    ),
                }

            with self._lock:
                self._attach_session(entry, new_session)
            _log(f"flash_firmware({id_}): reattached, now writing config back")

            try:
                if new_session.link.enter_config_mode():
                    _log(f"flash_firmware({id_}): in config mode, sending write_config")
                    write_ok = new_session.link.write_config(entry.to_blob())
                    _log(f"flash_firmware({id_}): write_config returned {write_ok}, resuming run mode")
                    new_session.link.resume_run_mode()
                    if write_ok:
                        new_session.baseline_pins = entry.pins
                        new_session.active_pins = entry.pins
                else:
                    _log(f"flash_firmware({id_}): couldn't re-enter config mode to write config")
            except Exception as exc:
                _log(f"flash_firmware({id_}): write_config step raised {exc!r}")

            _log(f"flash_firmware({id_}): returning ok")
            return {"ok": True, "output": output}
        except Exception as exc:
            _log(f"flash_firmware({id_}): UNEXPECTED exception: {exc!r}")
            return {"ok": False, "output": f"unexpected error while flashing: {exc}"}
        finally:
            with self._lock:
                self._busy_ports.discard(port)
            _log(f"flash_firmware({id_}): busy marker cleared, method exiting")

    def flash_port(self, port: str):
        """Flash firmware straight to a raw serial port, with no adopted
        entry or live session required. avrdude drives its own bootloader
        reset over the port directly, so this works even on a blank board
        that doesn't yet run firmware speaking our config protocol — the
        normal adopt_port() path needs that protocol to identify the board,
        which is exactly what a never-flashed board can't do yet."""
        with self._lock:
            if port in self._devices or port in self._busy_ports:
                return {"ok": False, "output": "port is already connected; disconnect it first"}
            self._busy_ports.add(port)
        try:
            output = flasher.flash(port)
            return {"ok": True, "output": output}
        except flasher.FlashError as exc:
            return {"ok": False, "output": str(exc)}
        except Exception as exc:
            return {"ok": False, "output": f"unexpected error while flashing: {exc}"}
        finally:
            with self._lock:
                self._busy_ports.discard(port)

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
    _log("main(): creating Api and window")
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
    _log("main(): window created, starting webview event loop")
    webview.start()
    _log("main(): webview.start() returned -- window closed")


if __name__ == "__main__":
    main()
