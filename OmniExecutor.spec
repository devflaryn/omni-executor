# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Omni Executor macOS BACKEND.

NOT the .app the user launches. Tauri builds that (src-tauri/); this produces
the headless backend it spawns and talks to over stdio (rpc.py), which
build-macos.sh then copies into Contents/Resources/backend/ inside the bundle.

It freezes main.py together with the sibling `omnidroid` engine package so the
frozen backend's `--omnidroid` in-binary dispatch (see main.py's `main()` /
`engine_prefix()`) can `from omnidroid.cli import main as engine_main` without
any separate binary or source checkout sitting next to the .app at runtime.
THE BACKEND IS STILL THE ENGINE.

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
hiddenimports += ["accountsync", "accountcreator", "cloud", "bootstrap", "updates", "rpc"]
hiddenimports += collect_submodules("omnidroid")
# Selenium drives the "add account" browser login (omnidroid/accounts.py). It is
# imported LAZILY inside the login functions, so PyInstaller's static analysis
# does not see it and the frozen engine reports "selenium is not installed" on a
# machine where it is. This was fixed in the Windows spec when it bit there and
# never mirrored here — the Mac build had the same bug waiting.
hiddenimports += collect_submodules("selenium")
# selenium-stealth masks the page-visible automation signals for the
# account-creation browser (stealth._apply_stealth). Same lazy-import
# problem as selenium itself, and it ships its patch scripts as package
# DATA (selenium_stealth/js/*.js) which the module list alone leaves out
# -- a frozen build without them silently drops to the reduced fallback.
hiddenimports += collect_submodules("selenium_stealth")
# The built-in VNC viewer (omnidroid/vncview.py) imports tkinter and PIL INSIDE
# its run function, so the frozen viewer died with "Pillow is required".
hiddenimports += ["tkinter", "PIL.Image", "PIL.ImageTk"]

# ---------------------------------------------------------------- dev mode
# DEV MODE IS NOT SHIPPED. `devserver` (omni-executor) and `omnidroid.devserver`
# redirect every call bound for http://179.198.197.7 to a local omni-backend, so
# an update can be exercised end to end before it is published. Every call site
# imports them in a try/except and falls back to the production server, which
# means keeping them OUT of the bundle is the whole enforcement: a customer's
# copy has no code to switch on, with or without an env var or a dropped-in
# dev.json.
#
# collect_submodules("omnidroid") returns omnidroid.devserver, so it is filtered
# from hiddenimports as well as excluded -- a name that is both hidden-imported
# and excluded is PyInstaller's to arbitrate, and this is not a thing to leave
# to a version bump.
#
# Set OMNI_DEV_BUILD=1 to build a bundle that KEEPS them (that is the point of
# the feature). Never set it for a build that gets published.
DEV_MODULES = ["devserver", "omnidroid.devserver"]
DEV_BUILD = os.environ.get("OMNI_DEV_BUILD", "").strip().lower() in ("1", "true", "yes", "on")
if DEV_BUILD:
    print("*** OMNI_DEV_BUILD=1: bundling dev mode (%s). DO NOT PUBLISH. ***"
          % ", ".join(DEV_MODULES))
    dev_excludes = []
else:
    hiddenimports = [h for h in hiddenimports if h not in DEV_MODULES]
    dev_excludes = list(DEV_MODULES)

datas = [
    # The frontend is NOT bundled here any more: the Tauri shell embeds
    # frontend/dist itself (tauri.conf.json's frontendDist), and shipping a
    # second copy inside the backend would only be a stale one.
]
datas += collect_data_files("omnidroid")
# Selenium Manager is a NATIVE BINARY shipped as package data; collecting the
# Python modules alone is not enough to resolve a chromedriver.
datas += collect_data_files("selenium", include_py_files=False)
# selenium_stealth/js/*.js -- read off disk at runtime by each patch
# (Path(__file__).parent.joinpath("js/...").read_text()), so without
# these the import succeeds and every patch then raises.
datas += collect_data_files("selenium_stealth")

a = Analysis(
    ["main.py"],
    pathex=[PROJECT_DIR, OMNIDROID_REPO],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=dev_excludes,
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="omni-exec-py",
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
    name="omni-exec-py",
)

# NO BUNDLE() ANY MORE, and that is the point of this file now.
#
# `Omni Executor.app` is built by Tauri (src-tauri/tauri.conf.json), which owns
# the icon, the identifier and the Info.plist. This spec produces the plain
# one-dir backend that build-macos.sh copies into
#
#     Omni Executor.app/Contents/Resources/backend/
#
# after the bundle exists. Two .app builders would only disagree with each
# other, and the one the user double-clicks has to be the one with the window
# in it. packaging/icon.icns is still the source art — src-tauri/icons/ is
# generated from it — and packaging/Info.plist is kept as the reference for any
# key Tauri's generated plist needs to carry.
