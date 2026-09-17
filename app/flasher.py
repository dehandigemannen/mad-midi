"""Wraps a bundled avrdude to flash firmware.hex onto the Mega 2560, so this
app never depends on the user having PlatformIO or the Arduino IDE
installed.

The bundled binary (vendor/avrdude/darwin/avrdude) is x86_64. On Apple
Silicon it runs via Rosetta 2 (present on virtually any Mac that's ever had
Xcode/dev tools installed), so one binary currently covers both Intel and
Apple Silicon Macs. A native arm64 build can be added later without any
interface change here.
"""

import subprocess
from pathlib import Path

VENDOR_DIR = Path(__file__).parent / "vendor"
AVRDUDE_DIR = VENDOR_DIR / "avrdude" / "darwin"
AVRDUDE_BIN = AVRDUDE_DIR / "avrdude"
AVRDUDE_CONF = AVRDUDE_DIR / "avrdude.conf"
FIRMWARE_HEX = VENDOR_DIR / "firmware.hex"


class FlashError(RuntimeError):
    pass


def flash(port: str, hex_path: Path = FIRMWARE_HEX) -> str:
    """Flashes hex_path onto an Arduino Mega 2560 at `port`. Returns
    avrdude's combined stdout/stderr on success; raises FlashError on
    failure."""
    if not AVRDUDE_BIN.exists():
        raise FlashError(f"avrdude not found at {AVRDUDE_BIN}")
    if not hex_path.exists():
        raise FlashError(f"firmware hex not found at {hex_path}")

    cmd = [
        str(AVRDUDE_BIN),
        "-C", str(AVRDUDE_CONF),
        "-v",
        "-p", "atmega2560",
        "-c", "wiring",
        "-P", port,
        "-b", "115200",
        "-D",
        "-U", f"flash:w:{hex_path}:i",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        # avrdude can genuinely stall (e.g. the port wasn't actually free) --
        # surface that as a normal FlashError rather than letting
        # TimeoutExpired escape uncaught. An unhandled exception here would
        # propagate through the pywebview JS bridge with nothing to catch
        # it, which can leave the calling JS promise unresolved forever --
        # i.e. the app just sits there looking hung instead of reporting
        # a failure.
        partial = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        raise FlashError(f"avrdude timed out after 60s (port may not have been free):\n{partial}")
    if result.returncode != 0:
        raise FlashError(result.stdout + result.stderr)
    return result.stdout + result.stderr
