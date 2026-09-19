"""Sidebar entry data model: a full 70-pin configuration for either an
adopted device (id = its persistent device_id) or an offline draft (id =
"draft-<hex>"), plus JSON persistence and conversion to/from the binary
EEPROM blob format (protocol.py). One JSON file per entry, in ENTRIES_DIR."""

import dataclasses
import json
from pathlib import Path
from typing import List, Optional

import protocol

ENTRIES_DIR = Path(__file__).parent / "devices"

# Superseded by ENTRIES_DIR (one JSON per device/draft, no named-preset
# library) — left in place only so migrate_legacy_presets() can pick up
# configs saved before that change.
LEGACY_PRESETS_DIR = Path(__file__).parent / "presets"


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
        return dataclasses.astuple(a) == dataclasses.astuple(b)


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
    button_pin: Optional[int] = None
    encoder_pin: Optional[int] = None  # encoder_a pin index; pin+1 is encoder_b
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
    step_control: str = "encoder"  # "encoder" | "pot" -- which role each step's value control uses
    steps: List[SequencerStepConfig] = dataclasses.field(
        default_factory=lambda: _default_steps(DEFAULT_STEP_COUNT)
    )

    def to_dict(self):
        return {
            "channel": self.channel,
            "subdivision": self.subdivision,
            "enabled": self.enabled,
            "step_control": self.step_control,
            "steps": [s.to_dict() for s in self.steps],
        }

    @staticmethod
    def from_dict(d) -> "SequencerConfig":
        steps = [SequencerStepConfig.from_dict(s) for s in d.get("steps", [])]
        return SequencerConfig(
            channel=d.get("channel", 0),
            subdivision=d.get("subdivision", "16th"),
            enabled=d.get("enabled", True),
            step_control=d.get("step_control", "encoder"),
            steps=steps or _default_steps(DEFAULT_STEP_COUNT),
        )


def _default_steps(count: int) -> List[SequencerStepConfig]:
    """Auto-assigns an incrementing note per step (still freely editable) so
    a fresh sequencer is immediately usable without manual note entry."""
    return [SequencerStepConfig(note=60 + i) for i in range(count)]


@dataclasses.dataclass
class Entry:
    id: str  # str(device_id) for a device entry, "draft-<hex>" for a draft
    entry_type: str  # "device" | "draft"
    nickname: str
    pins: List[PinConfig] = dataclasses.field(default_factory=list)
    device_id: int = 0  # meaningful only when entry_type == "device"
    sequencer: SequencerConfig = dataclasses.field(default_factory=SequencerConfig)

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
