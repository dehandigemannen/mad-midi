"""pywebview entrypoint: creates the Api and window. See PLAN.md for the
wire protocol and multi-device design this implements, and device_session.py
/ api.py for the session and bridge-API logic itself."""

import sys

import webview

from api import Api
from device_session import _log
from paths import LOG_DIR, RESOURCE_DIR

UI_DIR = RESOURCE_DIR / "ui"


def _redirect_logs():
    """A .app launched from Finder has nowhere useful for stderr to go, so
    the bundled build writes its _log() output to a file instead."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sys.stderr = open(LOG_DIR / "mad-midi.log", "a", buffering=1)


def main():
    if LOG_DIR is not None:
        _redirect_logs()
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
