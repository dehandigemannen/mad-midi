# PyInstaller spec for Mad Midi.app. Build via packaging/build_macos.sh,
# which sets MAD_MIDI_ARCH (arm64 now; x86_64 / universal2 later).

import os
from pathlib import Path

ROOT = Path(SPECPATH).parent
APP = ROOT / "app"
TARGET_ARCH = os.environ.get("MAD_MIDI_ARCH", "arm64")
VERSION = os.environ.get("MAD_MIDI_VERSION", "0.0.0")

a = Analysis(
    [str(APP / "main.py")],
    pathex=[str(APP)],
    datas=[
        (str(APP / "ui"), "ui"),
        # avrdude is x86_64-only (runs under Rosetta), so it goes in as data
        # rather than a binary: PyInstaller would reject it as the wrong arch
        # for an arm64 build.
        (str(APP / "vendor"), "vendor"),
    ],
    # mido picks its backend by name at runtime (importlib), so PyInstaller
    # can't see it.
    hiddenimports=["rtmidi", "mido.backends.rtmidi", "mido.backends.rtmidi_utils"],
    excludes=["tkinter", "app.tests"],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Mad Midi",
    console=False,
    target_arch=TARGET_ARCH,
    codesign_identity=os.environ.get("MAD_MIDI_CODESIGN_IDENTITY"),
)

coll = COLLECT(exe, a.binaries, a.datas, name="Mad Midi")

app = BUNDLE(
    coll,
    name="Mad Midi.app",
    bundle_identifier="nl.dehandigemannen.madmidi",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Mad Midi",
        "CFBundleShortVersionString": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
