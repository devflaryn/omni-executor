# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Omni Executor Windows BACKEND (omni-exec-py.exe).

NOT the app the user launches. That is omni-exec.exe, the Tauri shell built
from src-tauri/, which spawns this binary and talks to it over stdio (rpc.py).
The two ship side by side in one folder; build-windows.ps1 assembles them.

It no longer carries an engine. The QEMU engine used to be frozen in here and
reached through a `--omnidroid` in-binary dispatch; native omnidroid is a
separate bundle the app downloads to its runtime dir (bootstrap.py, droid.py).

ONE-DIR, deliberately. A one-file build unpacks itself to a fresh temp folder
on every launch -- slow, and exactly the self-extracting behaviour antivirus
heuristics distrust -- and the file-by-file updater (updates.py) needs a
stable install directory to replace.

Build with:  python -m PyInstaller --noconfirm OmniExecutor-win.spec
(normally invoked via .\\build-windows.ps1, which builds the frontend first.)
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

PROJECT_DIR = os.path.dirname(os.path.abspath(SPEC))
hiddenimports = []
# This app's own modules. Declared for the record rather than out of need:
# a control build without this line still bundled all three, because
# PyInstaller walks bytecode and does find a plain `import x` inside a
# function. Naming them keeps a future dynamic import (importlib, __import__)
# from silently dropping out of the bundle.
hiddenimports += ["accountsync", "accountcreator", "cloud", "bootstrap", "updates", "rpc",
                  "droid", "robloxaccounts", "autoexec"]
# Selenium drives the "add account" browser login (robloxaccounts.py). It
# is imported LAZILY inside the login functions, so PyInstaller's static
# analysis does not see it and the frozen engine reported "selenium is not
# installed" on a machine where it was.
hiddenimports += collect_submodules("selenium")
# selenium-stealth masks the page-visible automation signals for the
# account-creation browser (stealth._apply_stealth). Same lazy-import
# problem as selenium itself, and it ships its patch scripts as package
# DATA (selenium_stealth/js/*.js) which the module list alone leaves out
# -- a frozen build without them silently drops to the reduced fallback.
hiddenimports += collect_submodules("selenium_stealth")

# ---------------------------------------------------------------- dev mode
# DEV MODE IS NOT SHIPPED. `devserver`
# redirects every call bound for http://179.198.197.7 to a local omni-backend, so
# an update can be exercised end to end before it is published. Every call site
# imports them in a try/except and falls back to the production server, which
# means keeping them OUT of the bundle is the whole enforcement: a customer's
# copy has no code to switch on, with or without an env var or a dropped-in
# dev.json.
#
# Set OMNI_DEV_BUILD=1 to build a bundle that KEEPS them (that is the point of
# the feature). Never set it for a build that gets published.
DEV_MODULES = ["devserver"]
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
# Selenium Manager is a NATIVE BINARY shipped as package data
# (selenium/webdriver/common/windows/selenium-manager.exe). Without it
# _resolve_chromedriver() cannot download a chromedriver matching the
# installed Chrome, so collecting the Python modules alone is not enough.
datas += collect_data_files("selenium", include_py_files=False)
# selenium_stealth/js/*.js -- read off disk at runtime by each patch
# (Path(__file__).parent.joinpath("js/...").read_text()), so without
# these the import succeeds and every patch then raises.
datas += collect_data_files("selenium_stealth")

a = Analysis(
    ["main.py"],
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The retired QEMU engine package, should a stale sibling checkout ever be
    # importable again: it must never ride along in a build by accident.
    excludes=dev_excludes + ["omnidroid"],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Placeholder icon at packaging/omni-exec.ico -- swap the file for real
# branding, no spec change needed. Absent is fine (PyInstaller uses its own).
_icon = os.path.join(PROJECT_DIR, "packaging", "omni-exec.ico")
if not os.path.isfile(_icon):
    _icon = None

# Version resource (file properties -> Details). An unsigned exe with no
# product, company or version is one more strike in antivirus heuristics;
# packaging/version_info.py builds one from updates.APP_VERSION so the two
# cannot drift.
import sys as _sys
_sys.path.insert(0, os.path.join(PROJECT_DIR, "packaging"))
from version_info import version_resource  # noqa: E402

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
    version=version_resource(PROJECT_DIR, "omni-exec-py.exe",
                             "Omni Executor background service"),
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
