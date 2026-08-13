#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "==> building frontend"
( cd frontend && npm install && npm run build )

echo "==> freezing app (PyInstaller)"
python -m PyInstaller --noconfirm OmniExecutor.spec

echo "==> ad-hoc signing"
codesign --force --deep --sign - "dist/Omni Executor.app"

echo "==> building dmg"
if ! command -v create-dmg >/dev/null 2>&1; then
  echo "create-dmg not found; skipping .dmg (install: brew install create-dmg)"
  echo "==> done: dist/Omni Executor.app (dmg skipped)"
  exit 0
fi
rm -f "dist/Omni Executor.dmg"
create-dmg --volname "Omni Executor" --app-drop-link 480 200 \
  --icon "Omni Executor.app" 160 200 \
  "dist/Omni Executor.dmg" "dist/Omni Executor.app"
echo "==> done: dist/Omni Executor.dmg"
