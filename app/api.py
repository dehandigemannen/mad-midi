"""Api class exposed to the web UI via pywebview's js_api bridge: entry
persistence (devices + drafts), device adoption/reconnect, config-mode
read/write, firmware flashing, and the background auto-reconnect poller.
See PLAN.md for the wire protocol and multi-device design this implements."""

import json
import threading
import time
import uuid
from typing import Dict, Optional

import webview

import config_model
import flasher
import protocol
import serial_link
from device_session import DeviceSession, _log, _pins_equal

AUTO_POLL_INTERVAL_S = 4.0  # how often the background poller checks for known devices coming back online
AUTO_PROBE_COOLDOWN_S = 10.0  # don't retry a port that just failed to identify more than once per this long
AUTO_PORT_HINT = "usbmodem"  # macOS-only for now (see README) — Mega's native USB CDC port always matches this


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

    def reset_entry_pins(self, id_: str):
        """Wipes an entry's saved buffer back to a blank profile — every pin
        back to ROLE_UNUSED and the sequencer back to its defaults (same
        starting point as a brand new entry). Only updates the saved
        buffer, same as any other edit; use "Write to device" afterward to
        push the blank config to the hardware."""
        entry = config_model.Entry.load(id_)
        entry.pins = [config_model.PinConfig(pin=i) for i in range(protocol.PIN_COUNT)]
        entry.sequencer = config_model.SequencerConfig()
        entry.drone = config_model.DroneConfig()
        entry.fx_rack = config_model.FxRackConfig()
        entry.joystick = config_model.JoystickConfig()
        entry.save()
        session = self._session_for_device(entry.device_id) if entry.entry_type == "device" else None
        if session:
            session.active_pins = entry.pins
            session.rebuild_engines(entry)
        return entry.to_dict()

    def update_entry_pins(self, id_: str, pins_dicts, sequencer_dict=None,
                           drone_dict=None, fx_rack_dict=None, joystick_dict=None):
        """Debounced auto-save call fired from the UI on every edit — pins
        and every feature config are always saved together in one
        read-modify-write, so independent debounced saves can never race and
        corrupt the file (pins+sequencer used to be two independent
        debounced saves and could interleave ~400ms apart; see
        scheduleAutoSave() in app.js). Each feature dict is optional so
        existing callers that only touch pins keep working unchanged."""
        entry = config_model.Entry.load(id_)
        entry.pins = [config_model.PinConfig.from_dict(p) for p in pins_dicts]
        if sequencer_dict is not None:
            entry.sequencer = config_model.SequencerConfig.from_dict(sequencer_dict)
        if drone_dict is not None:
            entry.drone = config_model.DroneConfig.from_dict(drone_dict)
        if fx_rack_dict is not None:
            entry.fx_rack = config_model.FxRackConfig.from_dict(fx_rack_dict)
        if joystick_dict is not None:
            entry.joystick = config_model.JoystickConfig.from_dict(joystick_dict)
        entry.save()
        session = self._session_for_device(entry.device_id) if entry.entry_type == "device" else None
        if session:
            session.active_pins = entry.pins
            session.rebuild_engines(entry)
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
        device_entry.drone = draft.drone
        device_entry.fx_rack = draft.fx_rack
        device_entry.joystick = draft.joystick
        device_entry.save()
        session = self._session_for_device(device_entry.device_id)
        if session:
            session.active_pins = device_entry.pins
            session.rebuild_engines(device_entry)
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
        session.rebuild_engines(entry)
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

    def force_adopt_port(self, id_: str, port: str):
        """Like reconnect_entry(), but for when the board on `port` reports a
        different (or no) device id — e.g. a spare/replacement Mega swapped
        in for a broken one. Flashes this entry's firmware onto it, forces
        its device id to match, then writes the entry's saved config —
        making that physical board become this entry from now on.

        Refuses if the board already belongs to a *different* existing
        entry, so this can't be used to silently steal an already-adopted
        board out from under its own profile. The UI is expected to have
        already confirmed the (one-way, destructive) reassignment with the
        user before calling this — see reconnectEntry()'s mismatch handling
        in app.js.

        Mirrors flash_port()/flash_firmware()'s logging and error-handling
        shape (see flash_firmware's docstring for why every step here is
        wrapped instead of letting exceptions escape to the JS bridge)."""
        _log(f"force_adopt_port({id_}, {port}): called")
        entry = config_model.Entry.load(id_)
        if entry.entry_type != "device":
            return {"ok": False, "output": "not a device entry"}

        with self._lock:
            if port in self._devices or port in self._busy_ports:
                _log(f"force_adopt_port({id_}, {port}): port already connected/busy, aborting")
                return {"ok": False, "output": "port is already connected; disconnect it first"}
            self._busy_ports.add(port)

        try:
            _log(f"force_adopt_port({id_}, {port}): probing board's current id before flashing")
            probe = DeviceSession(port, self)
            probe.identify()
            found_id = probe.device_id
            probe.close()
            if found_id and found_id != entry.device_id and config_model.Entry.exists(str(found_id)):
                other = config_model.Entry.load(str(found_id))
                _log(f"force_adopt_port({id_}, {port}): refusing, board already belongs to '{other.nickname}' (id {found_id})")
                return {
                    "ok": False,
                    "output": f'that board already belongs to "{other.nickname}" (id {found_id}) — reassign or remove that entry first',
                }

            _log(f"force_adopt_port({id_}, {port}): invoking avrdude")
            try:
                output = flasher.flash(port)
                _log(f"force_adopt_port({id_}, {port}): avrdude returned OK")
            except flasher.FlashError as exc:
                _log(f"force_adopt_port({id_}, {port}): avrdude FAILED: {exc}")
                return {"ok": False, "output": str(exc)}

            try:
                _log(f"force_adopt_port({id_}, {port}): reopening {port}, identifying")
                session = DeviceSession(port, self)
                session.identify()
                _log(f"force_adopt_port({id_}, {port}): identify() returned, device_id={session.device_id}")
            except Exception as exc:
                _log(f"force_adopt_port({id_}, {port}): reopen/identify raised {exc!r}")
                return {"ok": False, "output": f"flashed, but couldn't reopen the port afterward ({exc})"}

            if session.device_id != entry.device_id:
                _log(f"force_adopt_port({id_}, {port}): forcing device id {entry.device_id} (was {session.device_id})")
                if session.link.enter_config_mode():
                    if session.link.set_device_id(entry.device_id):
                        session.device_id = entry.device_id
                        session._rebuild_bridge()
                    session.link.resume_run_mode()
                if session.device_id != entry.device_id:
                    _log(f"force_adopt_port({id_}, {port}): failed to force device id")
                    session.close()
                    return {"ok": False, "output": "flashed, but couldn't assign the device id — try again"}

            with self._lock:
                self._attach_session(entry, session)
            _log(f"force_adopt_port({id_}, {port}): reattached, writing config back")

            try:
                if session.link.enter_config_mode():
                    write_ok = session.link.write_config(entry.to_blob())
                    session.link.resume_run_mode()
                    if write_ok:
                        session.baseline_pins = entry.pins
                        session.active_pins = entry.pins
                else:
                    _log(f"force_adopt_port({id_}, {port}): couldn't re-enter config mode to write config")
            except Exception as exc:
                _log(f"force_adopt_port({id_}, {port}): write_config step raised {exc!r}")

            _log(f"force_adopt_port({id_}, {port}): returning ok")
            return {"ok": True, "output": output}
        except Exception as exc:
            _log(f"force_adopt_port({id_}, {port}): UNEXPECTED exception: {exc!r}")
            return {"ok": False, "output": f"unexpected error while flashing: {exc}"}
        finally:
            with self._lock:
                self._busy_ports.discard(port)
            _log(f"force_adopt_port({id_}, {port}): busy marker cleared, method exiting")

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
        session.rebuild_engines(entry)

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
        # None of these are part of the EEPROM blob -- preserve them
        fresh.sequencer = entry.sequencer
        fresh.drone = entry.drone
        fresh.fx_rack = entry.fx_rack
        fresh.joystick = entry.joystick
        session.baseline_pins = fresh.pins
        session.active_pins = fresh.pins
        session.rebuild_engines(fresh)
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
        _log(f"flash_port({port}): called")
        with self._lock:
            if port in self._devices or port in self._busy_ports:
                _log(f"flash_port({port}): port already connected/busy, aborting")
                return {"ok": False, "output": "port is already connected; disconnect it first"}
            self._busy_ports.add(port)
        try:
            _log(f"flash_port({port}): invoking avrdude")
            output = flasher.flash(port)
            _log(f"flash_port({port}): avrdude returned OK")
            return {"ok": True, "output": output}
        except flasher.FlashError as exc:
            _log(f"flash_port({port}): avrdude FAILED: {exc}")
            return {"ok": False, "output": str(exc)}
        except Exception as exc:
            _log(f"flash_port({port}): UNEXPECTED exception: {exc!r}")
            return {"ok": False, "output": f"unexpected error while flashing: {exc}"}
        finally:
            with self._lock:
                self._busy_ports.discard(port)
            _log(f"flash_port({port}): busy marker cleared, method exiting")

    # ---- event plumbing ----------------------------------------------------

    def _emit(self, event, payload):
        if not self._window:
            return
        js = f"window.dispatchEvent(new CustomEvent({json.dumps(event)}, {{detail: {json.dumps(payload)}}}))"
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass
