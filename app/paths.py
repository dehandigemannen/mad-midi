"""Where the app finds its bundled resources and keeps its user data.

Running from source, both live next to the code (app/ui, app/vendor,
app/devices), same as always. Running as a PyInstaller-built Mad Midi.app,
resources come from the bundle (sys._MEIPASS) and user data goes to
~/Library/Application Support/Mad Midi, since the bundle is read-only and
gets replaced wholesale on every update."""

import sys
from pathlib import Path

APP_NAME = "Mad Midi"

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    RESOURCE_DIR = Path(sys._MEIPASS)
    USER_DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
    LOG_DIR = Path.home() / "Library" / "Logs" / APP_NAME
else:
    RESOURCE_DIR = Path(__file__).parent
    USER_DATA_DIR = Path(__file__).parent
    LOG_DIR = None
