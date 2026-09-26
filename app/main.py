"""pywebview entrypoint: creates the Api and window. See PLAN.md for the
wire protocol and multi-device design this implements, and device_session.py
/ api.py for the session and bridge-API logic itself."""

from pathlib import Path

import webview

from api import Api
from device_session import _log

UI_DIR = Path(__file__).parent / "ui"


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
