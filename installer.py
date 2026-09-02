"""Omni Executor setup — the one file a new user downloads.

WHY THIS EXISTS, and not a .zip: Windows stamps every file extracted from a
downloaded archive with a `Zone.Identifier` stream, the .NET assembly loader
then refuses to load Python.Runtime.dll out of the Internet zone, and the app
died on launch before a line of its own code ran (see main._unblock_app_files).
Files an INSTALLER writes carry no such mark, so the whole failure mode simply
does not arise. It also ends the `Downloads\\omni-exec\\omni-exec\\omni-exec.exe`
nesting, and gives the user a Start Menu entry and an uninstaller like every
other program on their machine.

It is a STUB: it downloads the current build from the dist API rather than
carrying one, so the setup file is small and a download link never goes stale.
Verification is the same sha256 the app's own updater uses — the artifact and
the code that fetches it are shared with bootstrap.py, deliberately, so there
is one answer to "how do we fetch and verify a build".

PER-USER, so it never needs administrator: everything lands under
%LOCALAPPDATA%\\Programs\\OmniExecutor. (The app asks for elevation later only
if the machine needs Windows Hypervisor Platform turned on.)

    OmniExecutorSetup.exe              install, with a window
    OmniExecutorSetup.exe --silent     install, no window (exit code says it all)
    OmniExecutorSetup.exe --uninstall  remove it again
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import winreg
import zipfile
from pathlib import Path

import bootstrap

APP_NAME = "Omni Executor"
APP_KEY = "OmniExecutor"              # registry key + folder name
EXE_NAME = "omni-exec.exe"            # the Tauri shell: what a shortcut points at
BACKEND_NAME = "omni-exec-py.exe"     # the Python backend it spawns
ARTIFACT = "app-win"
# The uninstall entry Windows reads for "Apps & features".
_UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_KEY}"


class InstallError(Exception):
    pass


# ---------------------------------------------------------------- locations

def install_dir() -> Path:
    """%LOCALAPPDATA%\\Programs\\OmniExecutor — the per-user equivalent of
    Program Files, and writable with no elevation. Deliberately NOT beside the
    runtime dir (%LOCALAPPDATA%\\OmniExec): uninstalling should be able to
    remove the program without touching several gigabytes of base images the
    user may want to keep."""
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(root) / "Programs" / APP_KEY


def start_menu_dir() -> Path:
    root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(root) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"


# ---------------------------------------------------------------- installing

def _find_artifact() -> dict:
    manifest = bootstrap.read_manifest(bootstrap.dist_base())
    for a in manifest.get("artifacts", []):
        if a.get("name") == ARTIFACT:
            return a
    raise InstallError(
        f"the server is not offering a Windows build right now "
        f"({ARTIFACT} is not in its list). Try again later.")


def _app_root(unpacked: Path, declared_root=None) -> Path:
    """The app directory inside the unpacked archive. Same rule as
    updates._staged_build: trust the name the registry declared, else the one
    top-level directory. Guessing among several is how the wrong tree gets
    installed."""
    if declared_root and (unpacked / declared_root).is_dir():
        return unpacked / declared_root
    dirs = [p for p in unpacked.iterdir() if p.is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    raise InstallError("the downloaded build has an unexpected layout")


def _shortcut(path: Path, target: Path, description=""):
    """Create a .lnk. Shells out to WScript.Shell rather than taking a
    pywin32 dependency for four lines of COM — this binary has to stay small,
    and PowerShell is on every Windows that can run the app anyway."""
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{path}');"
        f"$s.TargetPath = '{target}';"
        f"$s.WorkingDirectory = '{target.parent}';"
        f"$s.Description = '{description}';"
        f"$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", script],
                   check=False, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _register_uninstall(target: Path, version: str, size_kb: int):
    """Put it in Apps & features, so it can be removed the normal way.

    HKCU, not HKLM: this is a per-user install and writing the machine-wide
    key would need the administrator rights the whole design avoids."""
    setup_copy = target / "OmniExecutorSetup.exe"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _UNINSTALL_KEY) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, version or "")
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "Omni")
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(target))
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, str(target / EXE_NAME))
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ,
                          f'"{setup_copy}" --uninstall')
        winreg.SetValueEx(k, "QuietUninstallString", 0, winreg.REG_SZ,
                          f'"{setup_copy}" --uninstall --silent')
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)


def install(progress=None) -> Path:
    """Download, verify, place, and wire up. Returns the installed exe."""
    def say(msg, pct=None):
        if progress:
            progress(msg, pct)

    say("Asking the server for the latest version…", 0)
    art = _find_artifact()
    version = art.get("version") or ""
    target = install_dir()

    tmp = Path(tempfile.mkdtemp(prefix="omni-setup-"))
    try:
        total = int(art.get("bytes") or 0)
        blob = tmp / "app.zip"

        def on_bytes(p):
            got = p.get("received", 0)
            pct = (got / total * 100) if total else 0
            say(f"Downloading {APP_NAME} {version} — "
                f"{got / 2**20:.0f} of {total / 2**20:.0f} MB", pct)

        # The app's own fetcher: Range-resumed, retried, and sha256-verified.
        bootstrap.download_blob(bootstrap.dist_base(), art, blob, on_bytes)

        say("Unpacking…", 100)
        unpacked = tmp / "unpacked"
        unpacked.mkdir()
        with zipfile.ZipFile(blob) as zf:
            bootstrap._safe_extract_zip(zf, unpacked)
        source = _app_root(unpacked, art.get("root"))

        # Replace an existing install rather than merging into it: a leftover
        # file from an older build is exactly the kind of thing that loads
        # instead of its replacement and is impossible to diagnose.
        #
        # bootstrap.replace_tree does it a FILE at a time and never renames or
        # deletes a directory, which is the only way that works on Windows: a
        # QEMU still running from a previous session, a `_windowlock` holding
        # an instance window proportional, an Explorer window, or an indexer
        # is each enough to pin the folder itself, and the previous code --
        # rename aside, else delete, else empty -- then fell through to
        # `copytree` and raised
        #
        #     [WinError 183] Cannot create a file when that file already
        #     exists: '...\\Programs\\OmniExecutor'
        #
        # naming a directory the user can plainly see exists and giving them
        # nothing to act on. Reported from a real machine 2026-08-18, and
        # again 2026-08-22 from the copy of this installer that was published
        # before the first attempt at a fix was ever built.
        #
        # _stop_running first regardless: an explicit installer run is one of
        # the two moments it is right to close the app, and fewer locked files
        # means fewer parked leftovers afterwards.
        say("Installing…")
        if target.exists():
            _stop_running(target)
        bootstrap.replace_tree(source, target, log=lambda m: say(m.strip()))

        # Keep a copy of this setup program so the uninstaller still exists
        # after the user deletes their Downloads folder.
        if getattr(sys, "frozen", False):
            try:
                shutil.copy2(sys.executable, target / "OmniExecutorSetup.exe")
            except OSError:
                pass

        say("Creating shortcuts…")
        exe = target / EXE_NAME
        for d in (start_menu_dir(), desktop_dir()):
            try:
                d.mkdir(parents=True, exist_ok=True)
                _shortcut(d / f"{APP_NAME}.lnk", exe, APP_NAME)
            except OSError:
                pass
        size_kb = max(1, sum(f.stat().st_size for f in target.rglob("*")
                             if f.is_file()) // 1024)
        try:
            _register_uninstall(target, version, size_kb)
        except OSError:
            pass          # a missing Apps-&-features entry is not a failed install

        # THE HOST TOOLS, here rather than only at first launch. Setup is the
        # right place for them: it is the one moment the user is already
        # watching a progress bar and expecting to be asked for administrator,
        # and ensure_tools() is idempotent so re-running setup is also how a
        # machine picks up a NEWER published QEMU.
        #
        # That upgrade is the reason this call exists at all. QEMU used to be
        # installed once and never revisited, so a machine whose QEMU came
        # from the vendor installer kept an unpatched build for good — and the
        # patched one is what honours QEMU_WINDOW_PANEL, so the guest's aspect
        # ratio was wrong with nothing reporting the tool as stale.
        #
        # NEVER FATAL. The app installs and runs regardless: it calls
        # ensure_tools() itself on the first-boot screen, so the worst case
        # here is that the work happens a few minutes later instead.
        say("Installing QEMU and adb…")
        try:
            rt = bootstrap.runtime_dir()
            tools = bootstrap.ensure_tools(
                rt, progress=lambda p: say(
                    f"Installing {p.get('artifact', 'tools')}…"))
            got = ", ".join(tools.get("installed") or []) or "already current"
            say(f"Tools: {got}")
        except Exception as e:                       # noqa: BLE001
            say(f"Tools will be installed on first launch ({e}).")

        say("Done.", 100)
        return exe
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _stop_running(target: Path):
    """Close a running copy before replacing it, or the copy fails on a locked
    exe. Only the one we are about to overwrite — never every omni-exec on the
    machine.

    BOTH executables, and the backend is the one that actually matters. The
    shell holds only itself open; the backend holds python3xx.dll and every
    other DLL in `_internal`, so stopping just `omni-exec` leaves the whole
    tree locked and the install fails on a file the user has never heard of.
    (`Get-Process omni-exec` does not match `omni-exec-py` — the name is exact,
    not a prefix — which is precisely how that would have been missed.)

    The backend does exit on its own when its stdin closes, i.e. shortly after
    the shell dies. Shortly is not a guarantee, and a race here costs a failed
    installation, so both are named explicitly."""
    wanted = {str((target / name).resolve()).lower()
              for name in (EXE_NAME, BACKEND_NAME)}
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Process omni-exec,omni-exec-py -ErrorAction SilentlyContinue | "
             "Select-Object Id,Path | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        procs = json.loads(out) if out.strip() else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    if isinstance(procs, dict):
        procs = [procs]
    killed = False
    for p in procs:
        if (p.get("Path") or "").lower() in wanted:
            subprocess.run(["taskkill", "/PID", str(p["Id"]), "/F"],
                           capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            killed = True
    if killed:
        # One wait for the set, not one per process: Windows can hold a file
        # briefly after the handle owner is gone.
        time.sleep(1.0)


# -------------------------------------------------------------- uninstalling

def uninstall(progress=None) -> None:
    """Remove the program. Deliberately LEAVES %LOCALAPPDATA%\\OmniExec — that
    is several gigabytes of base images and the user's accounts, and silently
    destroying it because they uninstalled the launcher would be indefensible.
    The message says where it is so they can delete it themselves."""
    def say(msg, pct=None):
        if progress:
            progress(msg, pct)

    target = install_dir()
    say("Closing Omni Executor…", 10)
    _stop_running(target)

    say("Removing shortcuts…", 30)
    for d in (start_menu_dir(), desktop_dir()):
        try:
            (d / f"{APP_NAME}.lnk").unlink(missing_ok=True)
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _UNINSTALL_KEY)
    except OSError:
        pass

    say("Removing files…", 60)
    # This program is running FROM the directory it is deleting, so its own
    # exe cannot be removed while it lives. Schedule that part for after exit.
    running = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
    if running and target.resolve() in running.parents:
        for item in target.iterdir():
            if item.resolve() == running:
                continue
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() \
                else item.unlink(missing_ok=True)
        subprocess.Popen(
            f'cmd /c timeout /t 3 /nobreak >nul & rmdir /s /q "{target}"',
            shell=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        shutil.rmtree(target, ignore_errors=True)
    say("Done.", 100)


# ------------------------------------------------------------------- the UI

def _gui(mode):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} Setup")
    root.geometry("460x210")
    root.resizable(False, False)
    root.configure(bg="#101014")

    verb = "Uninstall" if mode == "uninstall" else "Install"
    tk.Label(root, text=f"{verb} {APP_NAME}", bg="#101014", fg="#f0f0f4",
             font=("Segoe UI", 14)).pack(pady=(26, 4))
    detail = tk.Label(
        root,
        text=("This removes the program. Your accounts and downloaded images "
              "are kept." if mode == "uninstall" else
              "Installs for you only — no administrator needed."),
        bg="#101014", fg="#9a9aa6", font=("Segoe UI", 9), wraplength=400)
    detail.pack()

    bar = ttk.Progressbar(root, length=380, mode="determinate")
    bar.pack(pady=16)
    state = {"busy": False, "done": False}

    def set_progress(msg, pct=None):
        detail.config(text=msg)
        if pct is not None:
            bar.config(mode="determinate")
            bar["value"] = pct
        root.update_idletasks()

    def run():
        state["busy"] = True
        button.config(state="disabled")
        try:
            if mode == "uninstall":
                uninstall(set_progress)
                detail.config(text=f"{APP_NAME} has been removed.\n"
                                   f"Your data is still in "
                                   f"%LOCALAPPDATA%\\{bootstrap.APP_DIR_NAME}.")
                button.config(text="Close", state="normal",
                              command=root.destroy)
            else:
                exe = install(set_progress)
                detail.config(text=f"{APP_NAME} is installed.")
                button.config(text="Launch", state="normal",
                              command=lambda: (_launch(exe), root.destroy()))
            state["done"] = True
        except Exception as e:  # noqa: BLE001 — every failure belongs on screen
            bar["value"] = 0
            detail.config(text=f"{verb} failed: {e}")
            button.config(text="Retry", state="normal", command=start)
        finally:
            state["busy"] = False

    def start():
        threading.Thread(target=run, daemon=True).start()

    button = ttk.Button(root, text=verb, command=start)
    button.pack()
    root.protocol("WM_DELETE_WINDOW",
                  lambda: None if state["busy"] else root.destroy())
    root.mainloop()
    return 0


def _launch(exe: Path):
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)
    except OSError:
        pass


def _message(title, text):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:  # noqa: BLE001
        print(f"{title}: {text}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    silent = "--silent" in argv or "/S" in argv
    mode = "uninstall" if "--uninstall" in argv else "install"

    if sys.platform != "win32":
        print("This installer is for Windows.", file=sys.stderr)
        return 2

    if not silent:
        return _gui(mode)

    try:
        if mode == "uninstall":
            uninstall()
        else:
            exe = install()
            if "--no-launch" not in argv:
                _launch(exe)
    except Exception as e:  # noqa: BLE001
        print(f"{mode} failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
