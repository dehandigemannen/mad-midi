"""Round-trip checks for config_model's JSON export/import — no hardware,
pywebview, or serial connection needed.

Run with: python3 app/tests/test_import_export.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model


def _entry(id_, nickname, device_id=0):
    entry = config_model.Entry.blank(id_, "device", nickname, device_id=device_id)
    entry.pins[2].role = "button_note"
    entry.pins[2].number = 60
    return entry


def test_round_trip_keeps_config():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.json"
        original = [_entry("1", "Left", 1), _entry("2", "Right", 2)]
        config_model.write_export_file(path, original)
        loaded = config_model.read_export_file(path)
        assert [e.to_dict() for e in loaded] == [e.to_dict() for e in original]


def test_reads_bare_entry_file():
    # A file copied straight out of the devices/ folder, no export envelope.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "1.json"
        path.write_text(json.dumps(_entry("1", "Left", 1).to_dict()))
        loaded = config_model.read_export_file(path)
        assert len(loaded) == 1 and loaded[0].nickname == "Left"


def test_rejects_unrelated_json():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.json"
        path.write_text(json.dumps({"hello": "world"}))
        try:
            config_model.read_export_file(path)
        except ValueError:
            return
        raise AssertionError("expected ValueError")


def test_rejects_newer_version():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.json"
        path.write_text(json.dumps({"format": "mad-midi", "version": 99, "entries": []}))
        try:
            config_model.read_export_file(path)
        except ValueError:
            return
        raise AssertionError("expected ValueError")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
