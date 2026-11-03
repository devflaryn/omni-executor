#!/usr/bin/env bash
# Build the Omni Executor macOS app and optionally package it as a .dmg.
#
# Three stages, in this order:
#
#   1. npm          -- build the React frontend into frontend/dist
#   2. cargo tauri  -- build "Omni Executor.app", which EMBEDS frontend/dist
#                      and owns the window, the icon and the Info.plist
#   3. PyInstaller  -- freeze main.py into the headless backend (no engine
#                      inside: native omnidroid is a separate bundle the app
#                      downloads), and drop it INSIDE the bundle at
#                      Contents/Resources/backend/
#
# The shell must be built after the frontend (Tauri bakes the assets in at
# compile time) and before the backend is copied in (the bundle has to exist
# first). Needs Node, Python with PyInstaller, and a Rust toolchain.
set -euo pipefail
cd "$(dirname "$0")"

APP="dist/Omni Executor.app"
BACKEND_DIR="$APP/Contents/Resources/backend"

command -v cargo >/dev/null 2>&1 || {
  echo "cargo not found. Install Rust: https://rustup.rs" >&2; exit 1; }

# Compiled code records where it was built, home folder and all. Strip that.
export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$HOME=/build"

echo "==> building frontend"
( cd frontend && npm install && NODE_ENV=production npm run build )
# A development build of the page writes every source path into it, so refuse one.
if grep -rlaE "jsxDEV|$HOME" frontend/dist/assets >/dev/null 2>&1; then
  echo "frontend/dist is a development build or names $HOME. Rebuild it with NODE_ENV=production." >&2
  exit 1
fi

echo "==> building shell (Tauri) -> $APP"
# --no-bundle would skip the .app itself, which is the one thing we need, so
# the bundle targets are narrowed instead: the .dmg is made below, after the
# backend is inside. A dmg built now would ship a bundle with no backend in it.
npx tauri build --bundles app
rm -rf "$APP"
mkdir -p dist
cp -a "src-tauri/target/release/bundle/macos/Omni Executor.app" "$APP"

echo "==> freezing backend (PyInstaller, one-dir)"
python -m PyInstaller --noconfirm --distpath dist OmniExecutor.spec

echo "==> placing the backend inside the bundle"
rm -rf "$BACKEND_DIR"
mkdir -p "$(dirname "$BACKEND_DIR")"
mv "dist/omni-exec-py" "$BACKEND_DIR"
[ -x "$BACKEND_DIR/omni-exec-py" ] || {
  echo "the backend is missing from $BACKEND_DIR" >&2; exit 1; }

# The in-binary engine dispatch is the thing most likely to be silently broken
# by a packaging change, and it costs a second to prove.
echo "==> smoke test: omni-exec-py --doctor"
if "$BACKEND_DIR/omni-exec-py" --doctor | grep -q '"app_version"'; then
  echo "    backend OK"
else
  echo "    backend smoke test FAILED" >&2
  exit 1
fi

# AFTER the backend is inside: signing a bundle and then adding files to it
# invalidates the signature.
# Nothing that ships may name this machine's home folder.
if grep -rlaF "$HOME" "$APP" >/dev/null 2>&1; then
  echo "the bundle contains $HOME:" >&2
  grep -rlaF "$HOME" "$APP" | head >&2
  exit 1
fi

echo "==> ad-hoc signing"
codesign --force --deep --sign - "$APP"

echo "==> building dmg"
if ! command -v create-dmg >/dev/null 2>&1; then
  echo "create-dmg not found; skipping .dmg (install: brew install create-dmg)"
  echo "==> done: $APP (dmg skipped)"
  exit 0
fi
rm -f "dist/Omni Executor.dmg"
# 640 wide, so 160 and 480 sit either side of the middle. Without a window size
# Finder opens at about 500 and cuts the Applications icon off. The AppleScript
# that sets this needs a logged-in desktop session, so run it there and not over SSH.
create-dmg --volname "Omni Executor" --window-pos 200 120 --window-size 640 400 \
  --icon-size 128 --text-size 14 --hide-extension "Omni Executor.app" \
  --app-drop-link 480 180 --icon "Omni Executor.app" 160 180 \
  "dist/Omni Executor.dmg" "$APP"
echo "==> done: dist/Omni Executor.dmg"
