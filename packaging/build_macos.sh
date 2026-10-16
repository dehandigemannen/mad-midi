#!/usr/bin/env bash
# Builds dist/Mad Midi.app and a zipped copy of it.
#
#   packaging/build_macos.sh            # arm64 (Apple Silicon)
#   ARCH=x86_64 packaging/build_macos.sh   # Intel -- not supported yet
#
# Run from any directory; uses the project's .venv.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCH="${ARCH:-arm64}"
VERSION="${VERSION:-$(git -C "$ROOT" describe --tags --always 2>/dev/null || echo 0.0.0)}"
PY="$ROOT/.venv/bin/python"

case "$ARCH" in
  arm64) ;;
  x86_64|universal2)
    echo "ARCH=$ARCH isn't supported yet (python-rtmidi and pyobjc wheels in .venv are arm64-only)." >&2
    exit 1 ;;
  *) echo "Unknown ARCH=$ARCH" >&2; exit 1 ;;
esac

if [[ ! -x "$PY" ]]; then
  echo "No .venv found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements-build.txt" >&2
  exit 1
fi
"$PY" -m pip install -q -r "$ROOT/requirements-build.txt"

# The bundled avrdude must stay executable inside the app.
chmod +x "$ROOT/app/vendor/avrdude/darwin/avrdude"

MAD_MIDI_ARCH="$ARCH" MAD_MIDI_VERSION="$VERSION" \
  "$PY" -m PyInstaller --noconfirm --clean \
    --distpath "$ROOT/dist" --workpath "$ROOT/build" \
    "$ROOT/packaging/mad-midi.spec"

ZIP="$ROOT/dist/Mad-Midi-$VERSION-macos-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$ROOT/dist/Mad Midi.app" "$ZIP"

echo
echo "Built: $ROOT/dist/Mad Midi.app"
echo "       $ZIP"
