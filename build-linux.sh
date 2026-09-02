#!/usr/bin/env bash
# Build the Omni Executor Linux app (one-dir) and optionally zip it.
#
# RUN THIS ON THE LINUX BOX — PyInstaller cannot cross-build, so the Windows
# build is made on Windows (build-windows.ps1), the macOS one on a Mac
# (build-macos.sh), and this one here. Same source, same CLI on all three.
#
# Linux packaging policy is SYSTEM tooling, never a portable download:
#
#   sudo apt install -y python3-pip python3-venv python3-tk \
#                       libwebkit2gtk-4.1-dev libssl-dev libayatana-appindicator3-dev \
#                       librsvg2-dev libxdo-dev build-essential pkg-config file \
#                       qemu-system-x86 qemu-utils android-tools-adb nodejs npm
#   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
#   python3 -m venv .venv && . .venv/bin/activate
#   pip install -r requirements.txt pyinstaller
#
# python3-tk is not optional: tkinter+PIL are the built-in VNC viewer, and
# PyInstaller can only collect a Tcl/Tk runtime that exists at BUILD time.
#
# The webkit/gi packages are no longer for a Python webview backend -- there
# isn't one. libwebkit2gtk-4.1-dev and libsoup are what TAURI links against for
# the window, and a Rust toolchain (https://rustup.rs) is now a build
# requirement alongside Node and Python.
#
# Usage: ./build-linux.sh [--skip-frontend] [--zip] [--dist DIR]
set -euo pipefail
cd "$(dirname "$0")"

SKIP_FRONTEND=0
ZIP=0
DIST="dist"
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-frontend) SKIP_FRONTEND=1 ;;
        --zip)           ZIP=1 ;;
        --dist)          DIST="$2"; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

case "$(uname -s)" in
    Linux) ;;
    *) echo "PyInstaller cannot cross-build a Linux app. Run this on Linux." >&2
       exit 1 ;;
esac

PYTHON="${PYTHON:-python3}"
[ -x ".venv/bin/python" ] && PYTHON=".venv/bin/python"
echo "==> python: $PYTHON ($($PYTHON --version 2>&1))"

$PYTHON -c "import PyInstaller" 2>/dev/null || {
    echo "PyInstaller is not installed for '$PYTHON'. Run: $PYTHON -m pip install pyinstaller" >&2
    exit 1
}

# The Tauri shell EMBEDS frontend/dist at compile time, so this has to happen
# before cargo runs -- not merely before the app starts.
if [ "$SKIP_FRONTEND" -eq 0 ]; then
    echo "==> building frontend"
    ( cd frontend && npm install --silent && npm run build )
else
    echo "==> skipping frontend build (--skip-frontend)"
fi
[ -f "frontend/dist/index.html" ] || {
    echo "frontend/dist/index.html is missing — the shell would embed nothing." >&2
    exit 1
}

# A published build must NOT carry dev mode. Same guard as build-windows.ps1:
# loud, because the cost of getting it wrong is a customer bundle that can be
# pointed at somebody else's backend.
if [ -n "${OMNI_DEV_BUILD:-}" ]; then
    echo "*** OMNI_DEV_BUILD is set: this bundle will CONTAIN dev mode. DO NOT PUBLISH. ***"
fi

# omni-exec-py is the headless half: the Api the window calls over stdio and,
# via --omnidroid, the engine itself. It is not what the user launches.
echo "==> freezing backend (PyInstaller, one-dir) -> $DIST"
$PYTHON -m PyInstaller --noconfirm --distpath "$DIST" OmniExecutor-linux.spec

BACKEND_DIR="$DIST/omni-exec-py"
[ -x "$BACKEND_DIR/omni-exec-py" ] || {
    echo "build produced no $BACKEND_DIR/omni-exec-py" >&2; exit 1; }

# The window. Cargo rather than `tauri build`: this repo distributes a zip of
# the folder, so the .deb/.AppImage bundles the Tauri CLI would also produce
# are not wanted here.
echo "==> building shell (cargo, release)"
cargo build --release --features custom-protocol --manifest-path src-tauri/Cargo.toml
SHELL_BIN="src-tauri/target/release/omni-exec"
[ -x "$SHELL_BIN" ] || { echo "build produced no $SHELL_BIN" >&2; exit 1; }

# DID THE FRONTEND ACTUALLY GO IN? Without --features custom-protocol the build
# succeeds, the exe runs, and it loads build.devUrl instead of its own assets --
# so it works on a developer's machine with Vite up and shows "localhost refused
# to connect" everywhere else. That shipped once. The proof is cheap: the hashed
# asset name from frontend/dist must appear in the binary.
ASSET="$(basename "$(ls frontend/dist/assets/main-*.js | head -1)")"
[ -n "$ASSET" ] || { echo "no main-*.js in frontend/dist/assets" >&2; exit 1; }
if ! grep -qF "$ASSET" "$SHELL_BIN"; then
    echo "the shell does not embed frontend/dist ($ASSET is not in the binary)." >&2
    echo "It would open a 'localhost refused to connect' page. The" >&2
    echo "custom-protocol feature did not take -- see src-tauri/Cargo.toml." >&2
    exit 1
fi
echo "    frontend embedded OK ($ASSET)"

# One folder, two executables, side by side. The layout is load-bearing:
# updates.app_dir() is the BACKEND's own directory, so the backend has to sit
# at the root of the install for the file-by-file updater to replace the right
# tree, and backend.rs looks for omni-exec-py beside the shell.
APPDIR="$DIST/omni-exec"
echo "==> assembling -> $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR"
cp -a "$BACKEND_DIR/." "$APPDIR/"
cp -a "$SHELL_BIN" "$APPDIR/"
rm -rf "$BACKEND_DIR"

APP="$APPDIR/omni-exec"
BACKEND="$APPDIR/omni-exec-py"
[ -x "$APP" ] || { echo "assembly produced no $APP" >&2; exit 1; }
[ -x "$BACKEND" ] || { echo "assembly produced no $BACKEND" >&2; exit 1; }

# Smoke test: the in-binary engine dispatch is the thing most likely to be
# silently broken by a packaging change, and it costs a second to prove.
echo "==> smoke test: omni-exec-py --omnidroid version --json"
if "$BACKEND" --omnidroid version --json >/dev/null 2>&1; then
    echo "    engine dispatch OK"
else
    echo "    WARNING: engine dispatch failed — the frozen omnidroid is not callable" >&2
fi

if [ "$ZIP" -eq 1 ]; then
    VERSION="$($PYTHON -c 'import re,pathlib; print(re.search(r"^APP_VERSION\s*=\s*[\"\x27]([^\"\x27]+)", pathlib.Path("updates.py").read_text(), re.M).group(1))')"
    OUT="$DIST/omni-exec-linux-$VERSION.zip"
    echo "==> zipping -> $OUT"
    ( cd "$DIST" && zip -qry "$(basename "$OUT")" omni-exec )
    echo "    $(du -h "$OUT" | cut -f1)"
fi

echo "==> done: $APP"
