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
#                       libgirepository-2.0-dev libcairo2-dev pkg-config \
#                       gir1.2-webkit2-4.1 python3-gi python3-gi-cairo \
#                       qemu-system-x86 qemu-utils android-tools-adb nodejs npm
#   python3 -m venv .venv && . .venv/bin/activate
#   pip install -r requirements.txt pyinstaller
#
# python3-tk and the webkit/gi packages are not optional: tkinter+PIL are the
# built-in VNC viewer, and pywebview's GTK backend is what draws the window at
# all. PyInstaller can only collect a Tcl/Tk runtime that exists at BUILD time.
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

# The frozen app SERVES frontend/dist; without it the window opens blank.
if [ "$SKIP_FRONTEND" -eq 0 ]; then
    echo "==> building frontend"
    ( cd frontend && npm install --silent && npm run build )
else
    echo "==> skipping frontend build (--skip-frontend)"
fi
[ -f "frontend/dist/index.html" ] || {
    echo "frontend/dist/index.html is missing — the app would serve a blank window." >&2
    exit 1
}

# A published build must NOT carry dev mode. Same guard as build-windows.ps1:
# loud, because the cost of getting it wrong is a customer bundle that can be
# pointed at somebody else's backend.
if [ -n "${OMNI_DEV_BUILD:-}" ]; then
    echo "*** OMNI_DEV_BUILD is set: this bundle will CONTAIN dev mode. DO NOT PUBLISH. ***"
fi

echo "==> freezing app (PyInstaller, one-dir) -> $DIST"
$PYTHON -m PyInstaller --noconfirm --distpath "$DIST" OmniExecutor-linux.spec

APP="$DIST/omni-exec/omni-exec"
[ -x "$APP" ] || { echo "build produced no $APP" >&2; exit 1; }

# Smoke test: the in-binary engine dispatch is the thing most likely to be
# silently broken by a packaging change, and it costs a second to prove.
echo "==> smoke test: omni-exec --omnidroid version --json"
if "$APP" --omnidroid version --json >/dev/null 2>&1; then
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
