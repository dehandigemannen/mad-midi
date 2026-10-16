"""Sidebar entry data model: a full 70-pin configuration for either an
adopted device (id = its persistent device_id) or an offline draft (id =
"draft-<hex>"), plus JSON persistence and conversion to/from the binary
EEPROM blob format (protocol.py). One JSON file per entry, in ENTRIES_DIR."""

import dataclasses
import json
from pathlib import Path
from typing import List, Optional

import protocol
from paths import USER_DATA_DIR

ENTRIES_DIR = USER_DATA_DIR / "devices"

# Superseded by ENTRIES_DIR (one JSON per device/draft, no named-preset
# library) — left in place only so migrate_legacy_presets() can pick up
# configs saved before that change.
LEGACY_PRESETS_DIR = USER_DATA_DIR / "presets"


@dataclasses.dataclass
class PinConfig:
    pin: int
    role: str = "unused"
    channel: int = 0
    number: int = 0
    in_min: int = 0
    in_max: int = 0
    out_min: int = 0
    out_max: int = 0
    invert: bool = False
    toggle: bool = False
    match_cc: bool = False  # light_digital/light_pwm only: match a CC instead of a Note
    accel: bool = True  # encoder_a only: scale relative CC step size with turn speed
    shift_pin: int = protocol.NO_SHIFT_PIN  # pot_cc only: shift-group pin (mirrors encoder_a's in_min)
    shift_channel: int = 0  # pot_cc only: shifted-state MIDI channel
    shift_number: int = 0  # pot_cc only: shifted-state MIDI CC number
    # light_digital/light_pwm only: mirror this button/shift pin's live state
    # directly instead of matching an incoming Note/CC. NO_SHIFT_PIN = off.
    listen_pin: int = protocol.NO_SHIFT_PIN
    # encoder_a/pot_cc only: index of a light_pwm pin whose brightness should
    # track this control's live value (a VU meter). App-only bookkeeping --
    # deliberately excluded from to_tuple()/from_tuple(), since the firmware
    # has no idea this is happening (it's driven the same way a sequencer
    # step's PWM meter always was: a synthetic CC sent to the target pin's
    # own configured channel/number). NO_SHIFT_PIN = off. See value_mirror.py.
    mirror_pin: int = protocol.NO_SHIFT_PIN
    # encoder_a only: multiply every relative step this encoder sends by
    # this factor before it reaches the DAW. App-only, like mirror_pin --
    # see encoder_scaler.py. 1 = unchanged.
    step_size: int = 1

    @property
    def reserved(self) -> bool:
        return self.pin in protocol.RESERVED_PINS

    def to_tuple(self):
        flags = (
            (protocol.FLAG_INVERT if self.invert else 0)
            | (protocol.FLAG_TOGGLE if self.toggle else 0)
            | (protocol.FLAG_MATCH_CC if self.match_cc else 0)
            | (protocol.FLAG_ACCEL_ENABLE if self.accel else 0)
        )
        role_val = protocol.ROLE_BY_NAME.get(self.role, protocol.ROLE_UNUSED)
        return (
            role_val, self.channel & 0x0F, self.number & 0x7F,
            self.in_min & 0xFF, self.in_max & 0xFF,
            self.out_min & 0xFF, self.out_max & 0xFF, flags,
            self.shift_pin & 0xFF, self.shift_channel & 0x0F, self.shift_number & 0x7F,
            self.listen_pin & 0xFF,
        )

    @staticmethod
    def from_tuple(pin: int, t) -> "PinConfig":
        (role_val, channel, number, in_min, in_max, out_min, out_max, flags,
         shift_pin, shift_channel, shift_number, listen_pin) = t
        return PinConfig(
            pin=pin,
            role=protocol.ROLE_NAMES.get(role_val, "unused"),
            channel=channel,
            number=number,
            in_min=in_min,
            in_max=in_max,
            out_min=out_min,
            out_max=out_max,
            invert=bool(flags & protocol.FLAG_INVERT),
            toggle=bool(flags & protocol.FLAG_TOGGLE),
            match_cc=bool(flags & protocol.FLAG_MATCH_CC),
            accel=bool(flags & protocol.FLAG_ACCEL_ENABLE),
            shift_pin=shift_pin,
            shift_channel=shift_channel,
            shift_number=shift_number,
            listen_pin=listen_pin,
        )

    def to_dict(self):
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d) -> "PinConfig":
        return PinConfig(**d)

    @staticmethod
    def equal(a: "PinConfig", b: "PinConfig") -> bool:
        # Compare only blob-relevant fields (to_tuple()), not the full
        # dataclass — app-only bookkeeping fields (e.g. mirror_pin) never
        # reach the EEPROM blob and must not affect the "in sync" check.
        return a.to_tuple() == b.to_tuple()


DEFAULT_STEP_COUNT = 4


@dataclasses.dataclass
class SequencerStepConfig:
    """One step's physical control cluster. Pin fields are pin indices (or
    None if that function isn't wired up yet). The button is a latching
    switch — its own light is expected to be wired directly to it in
    hardware, not driven by this app — and it doubles as the step's on/off
    control (no separate mute). Note/velocity never change dynamically once
    triggered. pwm_pin mirrors the encoder's live CC output as PWM brightness
    (a value meter, not an enabled/disabled indicator). enabled_light_pin is a
    separate lamp that tracks the step's on/off (enabled/disabled) state
    rather than flashing on trigger like step_light_pin — it may be assigned
    to either a plain digital pin (on/off) or a PWM-capable pin (driven at
    full brightness), unlike step_light_pin which is digital-only."""
    note: int = 60
    control_type: str = "encoder"  # "encoder" | "pot" -- which role this step's own value control uses
    button_pin: Optional[int] = None
    encoder_pin: Optional[int] = None  # encoder_a pin index (pin+1 is encoder_b) if control_type == "encoder", else a pot_cc pin index
    shift_pin: Optional[int] = None
    pwm_pin: Optional[int] = None
    step_light_pin: Optional[int] = None
    enabled_light_pin: Optional[int] = None

    def to_dict(self):
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d) -> "SequencerStepConfig":
        d = dict(d)
        if "active_light_pin" in d:  # renamed — active light replaced by the PWM value meter
            d.setdefault("pwm_pin", d.pop("active_light_pin"))
        return SequencerStepConfig(**{k: v for k, v in d.items() if k in SequencerStepConfig.__dataclass_fields__})


@dataclasses.dataclass
class SequencerConfig:
    """App-only sequencer config for an Entry: which pin plays which role for
    each step, plus the shared subdivision/channel. Not part of the EEPROM
    blob — the step pattern (on/off) and playback position live in memory
    only, on the DeviceSession's SequencerEngine, not here."""
    channel: int = 0
    subdivision: str = "16th"  # "8th" | "16th" | "32nd" | "16th_triplet"
    enabled: bool = True  # off = DeviceSession never builds a SequencerEngine for this entry
    steps: List[SequencerStepConfig] = dataclasses.field(
        default_factory=lambda: _default_steps(DEFAULT_STEP_COUNT)
    )

    def to_dict(self):
        return {
            "channel": self.channel,
            "subdivision": self.subdivision,
            "enabled": self.enabled,
            "steps": [s.to_dict() for s in self.steps],
        }

    @staticmethod
    def from_dict(d) -> "SequencerConfig":
        steps = [SequencerStepConfig.from_dict(s) for s in d.get("steps", [])]
        return SequencerConfig(
            channel=d.get("channel", 0),
            subdivision=d.get("subdivision", "16th"),
            enabled=d.get("enabled", True),
            steps=steps or _default_steps(DEFAULT_STEP_COUNT),
        )


def _default_steps(count: int) -> List[SequencerStepConfig]:
    """Auto-assigns an incrementing note per step (still freely editable) so
    a fresh sequencer is immediately usable without manual note entry."""
    return [SequencerStepConfig(note=60 + i) for i in range(count)]


DRONE_MAX_SEGMENTS = 8


@dataclasses.dataclass
class DroneSegmentConfig:
    """One drone's physical control cluster. A drone never sends a Note --
    only its value control's ordinary CC output, which reaches the DAW only
    while the drone is enabled (see gated_segments.GatedSegmentEngine). The
    on/shift lights need no engine support at all: they're wired via
    PinConfig.listen_pin directly at the enable_pin/shift_pin (firmware
    mirrors a toggle button's logical on/off state, not just its raw
    press/release edges -- see roles.cpp's applyListenLights())."""
    control_type: str = "encoder"  # "encoder" | "pot" -- which role this segment's own value control uses
    control_pin: Optional[int] = None      # encoder_a or pot_cc: the value control
    enable_pin: Optional[int] = None       # button_cc+toggle; gates control_pin's CC to the DAW
    shift_pin: Optional[int] = None        # button_shift
    pwm_pin: Optional[int] = None          # light_pwm; sets control_pin.mirror_pin (see value_mirror.py)
    on_light_pin: Optional[int] = None     # light_digital/pwm, listen_pin -> enable_pin
    shift_light_pin: Optional[int] = None  # light_digital/pwm, listen_pin -> shift_pin

    def to_dict(self):
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d) -> "DroneSegmentConfig":
        return DroneSegmentConfig(**{k: v for k, v in d.items() if k in DroneSegmentConfig.__dataclass_fields__})


@dataclasses.dataclass
class DroneConfig:
    """App-only drone config for an Entry, same treatment as SequencerConfig:
    not part of the EEPROM blob, and a drone segment's live enabled state
    (see gated_segments.GatedSegmentEngine.enabled) lives in memory only on
    the DeviceSession's DroneEngine, not here."""
    enabled: bool = False  # off = DeviceSession never builds a DroneEngine for this entry
    channel: int = 0  # shared MIDI channel for every segment's control/enable pins
    segments: List[DroneSegmentConfig] = dataclasses.field(default_factory=list)

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "channel": self.channel,
            "segments": [s.to_dict() for s in self.segments],
        }

    @staticmethod
    def from_dict(d) -> "DroneConfig":
        return DroneConfig(
            enabled=d.get("enabled", False),
            channel=d.get("channel", 0),
            segments=[DroneSegmentConfig.from_dict(s) for s in d.get("segments", [])],
        )


FX_MAX_SEGMENTS = 8


@dataclasses.dataclass
class FxSegmentConfig:
    """One FX rack segment: always two encoders (never pots), an enable
    button gating both encoders' CC to the DAW, and a shift button shared by
    both encoders. No PWM/VU-meter pin -- just the on/shift lights, wired the
    same listen_pin way as DroneSegmentConfig's."""
    encoder1_pin: Optional[int] = None     # encoder_a; pin+1 is its encoder_b partner
    encoder2_pin: Optional[int] = None     # encoder_a; pin+1 is its encoder_b partner
    enable_pin: Optional[int] = None       # button_cc+toggle; gates BOTH encoders' CC
    shift_pin: Optional[int] = None        # button_shift; shared by both encoders
    on_light_pin: Optional[int] = None     # light_digital/pwm, listen_pin -> enable_pin
    shift_light_pin: Optional[int] = None  # light_digital/pwm, listen_pin -> shift_pin

    def to_dict(self):
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d) -> "FxSegmentConfig":
        return FxSegmentConfig(**{k: v for k, v in d.items() if k in FxSegmentConfig.__dataclass_fields__})


@dataclasses.dataclass
class FxRackConfig:
    """App-only FX rack config for an Entry -- same treatment as
    SequencerConfig/DroneConfig: not part of the EEPROM blob, live enabled
    state lives on the DeviceSession's FxRackEngine, not here."""
    enabled: bool = False
    channel: int = 0  # shared MIDI channel for every segment's encoder/enable pins
    segments: List[FxSegmentConfig] = dataclasses.field(default_factory=list)

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "channel": self.channel,
            "segments": [s.to_dict() for s in self.segments],
        }

    @staticmethod
    def from_dict(d) -> "FxRackConfig":
        return FxRackConfig(
            enabled=d.get("enabled", False),
            channel=d.get("channel", 0),
            segments=[FxSegmentConfig.from_dict(s) for s in d.get("segments", [])],
        )


@dataclasses.dataclass
class JoystickConfig:
    """App-only joystick config for an Entry: a single fixed X/Y/rotation +
    button widget, not a repeatable list. Every pin here is an ordinary
    existing role (pot_cc for the three axes, button_note/button_cc for the
    click) -- the firmware has no composite "joystick" concept at all, so
    there's no engine: all four pins just flow through the normal pin-role
    dispatch like any other input."""
    enabled: bool = False
    x_pin: Optional[int] = None       # pot_cc
    y_pin: Optional[int] = None       # pot_cc
    z_pin: Optional[int] = None       # pot_cc (rotation/twist axis)
    button_pin: Optional[int] = None  # button_note | button_cc

    def to_dict(self):
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d) -> "JoystickConfig":
        return JoystickConfig(**{k: v for k, v in d.items() if k in JoystickConfig.__dataclass_fields__})


@dataclasses.dataclass
class Entry:
    id: str  # str(device_id) for a device entry, "draft-<hex>" for a draft
    entry_type: str  # "device" | "draft"
    nickname: str
    pins: List[PinConfig] = dataclasses.field(default_factory=list)
    device_id: int = 0  # meaningful only when entry_type == "device"
    sequencer: SequencerConfig = dataclasses.field(default_factory=SequencerConfig)
    drone: DroneConfig = dataclasses.field(default_factory=DroneConfig)
    fx_rack: FxRackConfig = dataclasses.field(default_factory=FxRackConfig)
    joystick: JoystickConfig = dataclasses.field(default_factory=JoystickConfig)

    @staticmethod
    def blank(id_: str, entry_type: str, nickname: str, device_id: int = 0) -> "Entry":
        return Entry(
            id=id_, entry_type=entry_type, nickname=nickname, device_id=device_id,
            pins=[PinConfig(pin=i) for i in range(protocol.PIN_COUNT)],
        )

    def to_blob(self) -> bytes:
        by_pin = {p.pin: p for p in self.pins}
        tuples = [by_pin.get(i, PinConfig(pin=i)).to_tuple() for i in range(protocol.PIN_COUNT)]
        return protocol.build_config_blob(tuples, device_id=self.device_id)

    @staticmethod
    def from_blob(id_: str, nickname: str, blob: bytes) -> "Entry":
        device_id, tuples = protocol.parse_config_blob(blob)
        pins = [PinConfig.from_tuple(i, t) for i, t in enumerate(tuples)]
        return Entry(id=id_, entry_type="device", nickname=nickname, pins=pins, device_id=device_id)

    def to_dict(self):
        return {
            "id": self.id,
            "entry_type": self.entry_type,
            "nickname": self.nickname,
            "device_id": self.device_id,
            "pins": [p.to_dict() for p in self.pins],
            "sequencer": self.sequencer.to_dict(),
            "drone": self.drone.to_dict(),
            "fx_rack": self.fx_rack.to_dict(),
            "joystick": self.joystick.to_dict(),
        }

    @staticmethod
    def from_dict(d) -> "Entry":
        return Entry(
            id=d["id"],
            entry_type=d.get("entry_type", "device"),
            nickname=d.get("nickname", d["id"]),
            device_id=d.get("device_id", 0),
            pins=[PinConfig.from_dict(p) for p in d["pins"]],
            sequencer=SequencerConfig.from_dict(d["sequencer"]) if "sequencer" in d else SequencerConfig(),
            drone=DroneConfig.from_dict(d["drone"]) if "drone" in d else DroneConfig(),
            fx_rack=FxRackConfig.from_dict(d["fx_rack"]) if "fx_rack" in d else FxRackConfig(),
            joystick=JoystickConfig.from_dict(d["joystick"]) if "joystick" in d else JoystickConfig(),
        )

    @staticmethod
    def _path(id_: str, directory: Path = ENTRIES_DIR) -> Path:
        return directory / f"{id_}.json"

    def save(self, directory: Path = ENTRIES_DIR) -> Path:
        """Writes via a temp file + atomic rename so a concurrent reader (or
        an overlapping save) never sees a half-written/corrupted file."""
        directory.mkdir(parents=True, exist_ok=True)
        path = self._path(self.id, directory)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self.to_dict(), indent=2))
        tmp_path.replace(path)
        return path

    @staticmethod
    def load(id_: str, directory: Path = ENTRIES_DIR) -> "Entry":
        return Entry.from_dict(json.loads(Entry._path(id_, directory).read_text()))

    @staticmethod
    def exists(id_: str, directory: Path = ENTRIES_DIR) -> bool:
        return Entry._path(id_, directory).exists()

    @staticmethod
    def delete(id_: str, directory: Path = ENTRIES_DIR) -> None:
        path = Entry._path(id_, directory)
        if path.exists():
            path.unlink()

    @staticmethod
    def list_ids(directory: Path = ENTRIES_DIR) -> List[str]:
        if not directory.exists():
            return []
        return sorted(p.stem for p in directory.glob("*.json"))


def migrate_legacy_presets() -> None:
    """One-time upgrade path: fold any *.json files left over from the old
    named-preset library into drafts, since that library no longer exists.
    No-op once ENTRIES_DIR has been created."""
    if ENTRIES_DIR.exists() or not LEGACY_PRESETS_DIR.exists():
        return
    ENTRIES_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(LEGACY_PRESETS_DIR.glob("*.json")):
        try:
            d = json.loads(path.read_text())
            pins = [PinConfig.from_dict(p) for p in d["pins"]]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        draft_id = f"draft-{path.stem}"
        Entry(id=draft_id, entry_type="draft", nickname=d.get("name", path.stem), pins=pins).save()


# Export file format: a small envelope around Entry.to_dict() so the file is
# recognisable and can hold one entry or many. read_export_file() also takes
# a bare entry dict, i.e. a file copied straight out of ENTRIES_DIR.
EXPORT_FORMAT = "mad-midi"
EXPORT_VERSION = 1


def write_export_file(path, entries: List["Entry"]) -> None:
    data = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "entries": [e.to_dict() for e in entries],
    }
    Path(path).write_text(json.dumps(data, indent=2))


def read_export_file(path) -> List["Entry"]:
    """Raises ValueError (incl. json.JSONDecodeError) for a file that isn't
    a Mad Midi export."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and data.get("format") == EXPORT_FORMAT:
        if data.get("version", 1) > EXPORT_VERSION:
            raise ValueError("exported by a newer version of Mad Midi")
        raw_entries = data.get("entries", [])
    elif isinstance(data, dict) and "pins" in data:
        raw_entries = [data]
    else:
        raise ValueError("not a Mad Midi device file")
    if not raw_entries:
        raise ValueError("file contains no devices")
    return [Entry.from_dict(d) for d in raw_entries]
