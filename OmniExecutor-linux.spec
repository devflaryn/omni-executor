# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Omni Executor Linux app (omni-exec).

Mirrors OmniExecutor-win.spec: freezes main.py (the headless backend behind
the Tauri shell -- see that spec's header for the two-binary layout)
together with the sibling `omnidroid` engine package, so the frozen app's
`--omnidroid` in-binary dispatch (see main.py's `main()` / `engine_prefix()`)
can `from omnidroid.cli import main as engine_main` with NO omnidroid binary
and no source checkout next to the app at runtime.

Linux is the SAME x86 target as Windows -- same Bliss base, same arceus
offset, same qemu-system-x86_64 -- so this spec is the Windows one with the
Windows-only bits dropped, not a new design. The differences are exactly two:

  * No icon. .ico is a Windows resource format and PyInstaller's `icon=` is
    a no-op on Linux; desktop icons come from a .desktop file instead.
  * console=False still means "no terminal", but there is no Win32 subsystem
    flag behind it here -- the engine subprocesses inherit stdio normally,
    with no CREATE_NO_WINDOW equivalent to care about.

QEMU IS NOT BUNDLED. Linux packaging policy is SYSTEM qemu from apt (see
omnidroid/build-linux.sh and bootstrap._qemu_hint), so nothing here collects
an emulator; bootstrap deliberately withholds `qemu.download_url` on Linux so
the engine never tries to fetch the portable WINDOWS build.

ONE-DIR, deliberately -- same reasoning as the Windows spec: main.py
re-executes ITSELF (`sys.executable --omnidroid`) for each engine call, and a
one-file build would pay a full unpack per engine subprocess while
`config._app_root()` pointed at a throwaway temp dir.

Build with:  python3 -m PyInstaller --noconfirm OmniExecutor-linux.spec
(normally invoked via ./build-linux.sh, which builds the frontend first.)
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

PROJECT_DIR = os.path.dirname(os.path.abspath(SPEC))
# Sibling checkout: .../Omni Apps/omnidroid  (PROJECT_DIR is .../omni-executor)
OMNIDROID_REPO = os.path.normpath(os.path.join(PROJECT_DIR, "..", "omnidroid"))

hiddenimports = []
# This app's own modules. Declared for the record rather than out of need:
# a control build without this line still bundled all three, because
# PyInstaller walks bytecode and does find a plain `import x` inside a
# function. Naming them keeps a future dynamic import (importlib, __import__)
# from silently dropping out of the bundle.
hiddenimports += ["accountsync", "accountcreator", "cloud", "bootstrap", "updates", "rpc"]
hiddenimports += collect_submodules("omnidroid")
# Selenium drives the "add account" browser login (omnidroid/accounts.py). It
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
# The built-in VNC viewer (omnidroid/vncview.py, spawned as the hidden
# `_vncview` subcommand by the View button). It imports tkinter and PIL
# INSIDE its run function, so static analysis never sees them and the frozen
# viewer died on startup with "Pillow is required for the built-in viewer".
# On Linux tkinter also needs the system python3-tk package present at BUILD
# time, or PyInstaller has no Tcl/Tk runtime to collect.
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
# Selenium Manager is a NATIVE BINARY shipped as package data
# (selenium/webdriver/common/linux/selenium-manager). Without it
# _resolve_chromedriver() cannot download a chromedriver matching the
# installed Chrome, so collecting the Python modules alone is not enough.
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
    # GUI app: no terminal window.
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
