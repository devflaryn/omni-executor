# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Omni Executor macOS app.

Freezes main.py (the pywebview + React GUI) together with the sibling
`omnidroid` engine package so the frozen app's `--omnidroid` in-binary
dispatch (see main.py's `main()` / `engine_prefix()`) can
`from omnidroid.cli import main as engine_main` without any separate
binary or source checkout sitting next to the .app at runtime.

Build with:  python -m PyInstaller --noconfirm OmniExecutor.spec
(normally invoked via ./build-macos.sh, which also builds the frontend
first and ad-hoc signs + dmg-packages the result.)
"""
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

PROJECT_DIR = os.path.dirname(os.path.abspath(SPEC))
# Sibling checkout: .../Omni Apps/omnidroid  (PROJECT_DIR is .../Omni Apps/omni-executor)
OMNIDROID_REPO = os.path.normpath(os.path.join(PROJECT_DIR, "..", "omnidroid"))

hiddenimports = []
# This app's own modules — see OmniExecutor-win.spec: declared for the record,
# not out of need. Kept identical to the Windows spec on purpose (tests/
# test_packaging.py asserts the two agree).
hiddenimports += ["accountsync", "cloud", "bootstrap"]
hiddenimports += collect_submodules("omnidroid")
# Selenium drives the "add account" browser login (omnidroid/accounts.py). It is
# imported LAZILY inside the login functions, so PyInstaller's static analysis
# does not see it and the frozen engine reports "selenium is not installed" on a
# machine where it is. This was fixed in the Windows spec when it bit there and
# never mirrored here — the Mac build had the same bug waiting.
hiddenimports += collect_submodules("selenium")
# The built-in VNC viewer (omnidroid/vncview.py) imports tkinter and PIL INSIDE
# its run function, so the frozen viewer died with "Pillow is required".
hiddenimports += ["tkinter", "PIL.Image", "PIL.ImageTk"]

datas = [
    (os.path.join(PROJECT_DIR, "frontend", "dist"), "frontend/dist"),
]
datas += collect_data_files("omnidroid")
# Selenium Manager is a NATIVE BINARY shipped as package data; collecting the
# Python modules alone is not enough to resolve a chromedriver.
datas += collect_data_files("selenium", include_py_files=False)

a = Analysis(
    ["main.py"],
    pathex=[PROJECT_DIR, OMNIDROID_REPO],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OmniExecutor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OmniExecutor",
)

# Placeholder icon lives at packaging/icon.icns (a solid-color square
# generated via sips/iconutil — swap it for real branding by replacing
# packaging/icon.icns with a proper 1024x1024-sourced .icns before shipping;
# no spec changes needed since it's referenced by path here).
_icon = os.path.join(PROJECT_DIR, "packaging", "icon.icns")
if not os.path.isfile(_icon):
    _icon = None

app = BUNDLE(
    coll,
    name="Omni Executor.app",
    icon=_icon,
    bundle_identifier="com.omniapps.executor",
    info_plist=os.path.join(PROJECT_DIR, "packaging", "Info.plist"),
)
