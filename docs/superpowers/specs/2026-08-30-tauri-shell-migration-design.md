# Tauri shell migration — design

Date: 2026-08-30
Status: approved, in implementation

## Why

The app's window is a pywebview window. Everything the product wants from it —
a frameless transparent sheet with a 33 px corner radius, native traffic lights
on macOS, our own controls on Windows/Linux, drag only from the titlebar,
resize from the corners — is currently bought with Win32 surgery
(`windowchrome.py`: a WndProc subclass, `SetWindowRgn`, a hand-driven
`WM_NCLBUTTONDOWN` move loop) and a per-platform pile of AppKit and GTK
fallbacks. Tauri gives all of it as window configuration on all three
platforms, and removes pywebview, pythonnet and the .NET assembly loader that
made Mark-of-the-Web a fatal launch error.

## Shape

`omni-exec.exe` becomes the **Rust (Tauri) binary**. The existing PyInstaller
one-dir build survives as the **headless backend**, renamed `omni-exec-py.exe`,
sitting in the **same folder**.

    OmniExecutor/                  (Windows / Linux)
      omni-exec.exe                Tauri app  <- the front door
      omni-exec-py.exe             PyInstaller backend + the omnidroid engine
      _internal/...                PyInstaller's tree

    Omni Executor.app/             (macOS)
      Contents/MacOS/omni-exec     Tauri app
      Contents/Resources/backend/  PyInstaller tree, omni-exec-py inside

That placement is load-bearing. `updates.app_dir()` is
`Path(sys.executable).parent`, and `sys.executable` in the backend is
`omni-exec-py.exe`; keeping it at the install root means the whole file-by-file
updater keeps working unchanged. `bootstrap.engine_prefix()` returns
`[sys.executable, "--omnidroid"]`, so **in-binary engine dispatch is
unaffected**: the backend exe is still the engine.

## IPC — newline-delimited JSON over stdio

No ports, no token, no firewall prompt. (This machine blackholes closed
loopback ports and cannot load `localhost` in a Chromium; a socket bridge would
ride exactly that path.)

Request, JS -> Rust -> Python:

    {"id": 7, "method": "engine_start", "args": ["farm_alpha", "gaming"]}

Reply, Python -> Rust -> JS:

    {"id": 7, "ok": true, "result": {...}}
    {"id": 7, "ok": false, "error": "bridge_error", "message": "..."}

Push, Python -> Rust -> JS (the existing `Api._push` bus, verbatim payloads):

    {"event": "engine-progress", "payload": {"scope": "...", "line": "..."}}

* Rust holds the child's stdin behind a mutex and runs one reader thread over
  stdout, matching `id` to a pending oneshot. A 40-second `bootstrap_start`
  therefore never blocks a 5 ms `get_settings`.
* Python dispatches each request on a thread pool, for the same reason.
* **stdout hygiene.** `main.py` and `bootstrap.py` `print()` freely. `rpc.py`
  dups the real stdout to a private fd at startup and repoints `sys.stdout` at
  `sys.stderr`, so only the RPC writer owns the channel and a stray print
  cannot corrupt a frame.
* Lifecycle: the backend exits when stdin closes (parent gone); Rust kills the
  child on window close. Windows spawns with `CREATE_NO_WINDOW`.

## Window chrome

|          | macOS                                   | Windows                        | Linux                    |
|----------|-----------------------------------------|--------------------------------|--------------------------|
| Frame    | `titleBarStyle: Overlay`, `hiddenTitle` | `decorations: false`           | `decorations: false`     |
| Controls | native traffic lights, top-LEFT         | ours, RIGHT                    | ours, RIGHT              |
| Radius   | CSS 33 px on a transparent window       | CSS 33 px + `SetWindowRgn`     | CSS 33 px                |
| Drag     | `startDragging()`, titlebar only        | same                           | same                     |
| Resize   | native frame                            | `startResizeDragging(dir)`     | same                     |

`TitleBar.jsx` already has the right shape (the `chrome.mac` split, the drag
surfaces, `ResizeEdges` with 5 px edges and 14 px corners). It is **retargeted,
not rewritten**.

**The corner-radius risk.** WebView2's windowed hosting has no PARTIAL
per-pixel alpha — a fact this repo already paid for. Tauri uses the same
hosting, so a transparent Tauri window on Windows gives a 33 px arc that is
aliased at best. The cure is the one already proven here: cut a DPI-scaled
round-rect out of the HWND with `CreateRoundRectRgn`/`SetWindowRgn`, re-applied
on resize, DPI change and maximize/restore. `windowchrome.py` is deleted, but
that trick survives, ported to `src-tauri/src/chrome.rs`. Verify by
`PrintWindow` capture against a NON-white backdrop; a white backdrop lies.

## What is deleted

`windowchrome.py`, `tests/test_windowchrome.py`, `import webview` and the
`webview.create_window` / `webview.start` call sites, the six window-control
methods on `Api`, `_show_macos_traffic_lights`, `_open_devtools`, `pywebview`
from `requirements.txt`, and the pywebview / pythonnet hidden-imports from all
three PyInstaller specs. `_unblock_app_files` stays as cheap insurance but its
probe path changes: without pythonnet there is no .NET loader to refuse.

## Packaging

Each build script gains a `cargo tauri build` step plus an assembly step that
lays the Rust exe and the PyInstaller tree into one folder. Zip / installer /
DMG output names do not move, so the dist channel and the setup stub are
unaffected.

`updates.py` gains `_updater_in(build)`: `--apply-update` is handed to the
**Python** exe, which still understands that mode, while the post-swap relaunch
fires the **Rust** exe.

## Testing

* Python: `rpc.py` unit tests — framing, dispatch, error shape, concurrent
  calls, stdout hygiene, stdin-close shutdown. Into the existing pytest suite
  (green here is 3 failed, not 0).
* Rust: multiplexer tests against a fake child that replies out of order.
* Manual: `PrintWindow` capture on each platform for the corners; traffic
  lights left on macOS, controls right elsewhere; drag only from the titlebar;
  resize from all four corners.
