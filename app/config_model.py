"""Preset data model: a full 70-pin configuration, plus JSON persistence and
conversion to/from the binary EEPROM blob format (protocol.py)."""

import dataclasses
import json
from pathlib import Path
from typing import List

import protocol

PRESETS_DIR = Path(__file__).parent / "presets"


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

    @property
    def reserved(self) -> bool:
        return self.pin in protocol.RESERVED_PINS

    def to_tuple(self):
        flags = (protocol.FLAG_INVERT if self.invert else 0) | (protocol.FLAG_TOGGLE if self.toggle else 0)
        role_val = protocol.ROLE_BY_NAME.get(self.role, protocol.ROLE_UNUSED)
        return (
            role_val, self.channel & 0x0F, self.number & 0x7F,
            self.in_min & 0xFF, self.in_max & 0xFF,
            self.out_min & 0xFF, self.out_max & 0xFF, flags,
        )

    @staticmethod
    def from_tuple(pin: int, t) -> "PinConfig":
        role_val, channel, number, in_min, in_max, out_min, out_max, flags = t
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
        )

    def to_dict(self):
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d) -> "PinConfig":
        return PinConfig(**d)


@dataclasses.dataclass
class Preset:
    name: str
    pins: List[PinConfig] = dataclasses.field(default_factory=list)
    device_id: int = 0  # 0 = unassigned; set on the device via SET_DEVICE_ID, not by writing a preset

    @staticmethod
    def blank(name: str) -> "Preset":
        return Preset(name=name, pins=[PinConfig(pin=i) for i in range(protocol.PIN_COUNT)])

    def to_blob(self) -> bytes:
        by_pin = {p.pin: p for p in self.pins}
        tuples = [by_pin.get(i, PinConfig(pin=i)).to_tuple() for i in range(protocol.PIN_COUNT)]
        return protocol.build_config_blob(tuples, device_id=self.device_id)

    @staticmethod
    def from_blob(name: str, blob: bytes) -> "Preset":
        device_id, tuples = protocol.parse_config_blob(blob)
        pins = [PinConfig.from_tuple(i, t) for i, t in enumerate(tuples)]
        return Preset(name=name, pins=pins, device_id=device_id)

    def to_dict(self):
        return {
            "name": self.name,
            "pins": [p.to_dict() for p in self.pins],
            "device_id": self.device_id,
        }

    @staticmethod
    def from_dict(d) -> "Preset":
        return Preset(
            name=d["name"],
            pins=[PinConfig.from_dict(p) for p in d["pins"]],
            device_id=d.get("device_id", 0),
        )

    def save(self, directory: Path = PRESETS_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.name}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    @staticmethod
    def load(name: str, directory: Path = PRESETS_DIR) -> "Preset":
        path = directory / f"{name}.json"
        return Preset.from_dict(json.loads(path.read_text()))

    @staticmethod
    def list_presets(directory: Path = PRESETS_DIR) -> List[str]:
        if not directory.exists():
            return []
        return sorted(p.stem for p in directory.glob("*.json"))
