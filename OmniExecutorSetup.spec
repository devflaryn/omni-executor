# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OmniExecutorSetup.exe — the file a new user downloads.

ONE-FILE here, unlike the app's own one-dir build, and for the opposite
reason. The app is one-dir because it re-executes itself for every engine call
and must have a stable directory on disk; the installer runs once, does its
work, and exits, so the single-file unpack cost is irrelevant and being ONE
downloadable file is the entire point — a zip is what caused the
Mark-of-the-Web failure this installer exists to eliminate.

It bundles installer.py + bootstrap.py ONLY. Not the engine, not the frontend,
not selenium: it downloads the real build from the dist API, so pulling any of
that in would inflate a ~10 MB stub into another copy of the app for no reason.

Build with:  python -m PyInstaller --noconfirm OmniExecutorSetup.spec
(or via .\\build-windows.ps1 -Installer)
"""
import os

block_cipher = None

PROJECT_DIR = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    ["installer.py"],
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=[],
    # bootstrap for the download/verify/extract path (one answer to "how do we
    # fetch a build"), tkinter for the progress window.
    hiddenimports=["bootstrap", "tkinter", "tkinter.ttk"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The app's dependencies, none of which the stub needs. Excluded rather
    # than left to chance: bootstrap.py is pure stdlib, but a stray transitive
    # import would otherwise quietly triple the download the user waits on.
    # `devserver` is listed unconditionally, with no OMNI_DEV_BUILD escape: the
    # stub is the file on the download page, it is the ONE artifact a stranger
    # runs, and it has no dev use -- there is nothing to test locally about
    # "download the published build". See devserver.py.
    excludes=["webview", "selenium", "PIL", "omnidroid", "clr", "pythonnet",
              "clr_loader", "numpy", "cryptography", "requests", "devserver"],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_icon = os.path.join(PROJECT_DIR, "packaging", "omni-exec.ico")
if not os.path.isfile(_icon):
    _icon = None

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="OmniExecutorSetup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # Windowed: it draws its own tkinter progress window. --silent callers get
    # exit codes, which is what an unattended install actually needs.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)
