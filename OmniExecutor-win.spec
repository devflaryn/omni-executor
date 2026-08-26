# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Omni Executor Windows app (omni-exec.exe).

Mirrors OmniExecutor.spec (macOS): freezes main.py (the pywebview + React
GUI) together with the sibling `omnidroid` engine package, so the frozen
app's `--omnidroid` in-binary dispatch (see main.py's `main()` /
`engine_prefix()`) can `from omnidroid.cli import main as engine_main`
with NO omnidroid.exe and no source checkout next to the app at runtime.

ONE-DIR, deliberately. A one-file build unpacks the whole bundle to a fresh
%TEMP%\\_MEIxxxx on EVERY launch, and main.py re-executes ITSELF
(`sys.executable --omnidroid`) for each engine call -- so a one-file build
pays that unpack again per engine subprocess, and `config._app_root()`
(which resolves to the exe's directory when frozen) would point at a
throwaway temp dir. One-dir keeps engine dispatch cheap and the app root
stable. build-windows.ps1 zips the folder for distribution.

Build with:  python -m PyInstaller --noconfirm OmniExecutor-win.spec
(normally invoked via .\\build-windows.ps1, which builds the frontend first.)
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

PROJECT_DIR = os.path.dirname(os.path.abspath(SPEC))
# Sibling checkout: ...\Omni Apps\omnidroid  (PROJECT_DIR is ...\omni-executor)
OMNIDROID_REPO = os.path.normpath(os.path.join(PROJECT_DIR, "..", "omnidroid"))

hiddenimports = []
# This app's own modules. Declared for the record rather than out of need:
# a control build without this line still bundled all three, because
# PyInstaller walks bytecode and does find a plain `import x` inside a
# function. Naming them keeps a future dynamic import (importlib, __import__)
# from silently dropping out of the bundle.
hiddenimports += ["accountsync", "accountcreator", "cloud", "bootstrap", "updates", "windowchrome"]
hiddenimports += collect_submodules("omnidroid")
# Selenium drives the "add account" browser login (omnidroid/accounts.py). It
# is imported LAZILY inside the login functions, so PyInstaller's static
# analysis does not see it and the frozen engine reported "selenium is not
# installed" on a machine where it was.
hiddenimports += collect_submodules("selenium")
# The built-in VNC viewer (omnidroid/vncview.py, spawned as the hidden
# `_vncview` subcommand by the View button). It imports tkinter and PIL
# INSIDE its run function, so static analysis never sees them and the frozen
# viewer died on startup with "Pillow is required for the built-in viewer".
# Naming them here also pulls in PyInstaller's tkinter hook, which ships the
# Tcl/Tk runtime the viewer window needs.
hiddenimports += ["tkinter", "PIL.Image", "PIL.ImageTk"]

# ---------------------------------------------------------------- dev mode
# DEV MODE IS NOT SHIPPED. `devserver` (omni-executor) and `omnidroid.devserver`
# redirect every call bound for http://72.62.59.232 to a local omni-backend, so
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
    (os.path.join(PROJECT_DIR, "frontend", "dist"), "frontend/dist"),
]
datas += collect_data_files("omnidroid")
# Selenium Manager is a NATIVE BINARY shipped as package data
# (selenium/webdriver/common/windows/selenium-manager.exe). Without it
# _resolve_chromedriver() cannot download a chromedriver matching the
# installed Chrome, so collecting the Python modules alone is not enough.
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
    excludes=dev_excludes,
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Placeholder icon at packaging/omni-exec.ico -- swap the file for real
# branding, no spec change needed. Absent is fine (PyInstaller uses its own).
_icon = os.path.join(PROJECT_DIR, "packaging", "omni-exec.ico")
if not os.path.isfile(_icon):
    _icon = None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="omni-exec",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # GUI app: no console window. The engine subprocesses this spawns are
    # already created with CREATE_NO_WINDOW (see main.py), so nothing flashes
    # a black box at the user.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="omni-exec",
)
