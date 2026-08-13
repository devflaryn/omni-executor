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
hiddenimports += collect_submodules("omnidroid")
# Selenium drives the "add account" browser login (omnidroid/accounts.py). It
# is imported LAZILY inside the login functions, so PyInstaller's static
# analysis does not see it and the frozen engine reported "selenium is not
# installed" on a machine where it was.
hiddenimports += collect_submodules("selenium")

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
    excludes=[],
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
