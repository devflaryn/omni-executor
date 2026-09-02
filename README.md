# Omni Executor

Desktop app for the **omnidroid** engine — each account is an isolated,
headless Android (Bliss OS) instance managed via QEMU. The GUI drives the
engine exclusively through its CLI (`omnidroid.exe`, JSON output); see
`omnidroid.md` for the engine's own manual.

## How it is put together

Three pieces, and it is worth knowing which is which before changing anything:

| | what it is | where |
|---|---|---|
| **shell** | a Tauri (Rust) app: the window, and nothing else | `src-tauri/` |
| **frontend** | React + Tailwind, embedded in the shell at compile time | `frontend/` |
| **backend** | Python: the whole `Api`, and — via `--omnidroid` — the engine | `main.py` and friends |

The shell spawns the backend as a child process and they speak
newline-delimited JSON over its stdin/stdout (`rpc.py`). No port, no token, no
firewall prompt. `api("engine_start", ...)` in the frontend still lands on
`Api.engine_start` in Python exactly as it did under pywebview; only the
transport changed.

Shipped, the two binaries sit side by side in one folder — `omni-exec(.exe)`
is the shell, `omni-exec-py(.exe)` the backend — which is load-bearing:
`updates.app_dir()` is the backend's own directory, so it has to be at the root
of the install for the file-by-file updater to replace the right tree.

## Setup

Needs Python, Node **and** a Rust toolchain ([rustup.rs](https://rustup.rs)).

```sh
pip install -r requirements.txt
npm install                    # the Tauri CLI (root)
npm --prefix frontend install  # the app's own deps
```

Put `omnidroid.exe` (Windows) or `omnidroid` (Linux) next to `main.py`.
The engine self-bootstraps on first use; the Accounts tab shows a readiness
banner (with a "Run setup" button) if anything is missing.

## Run

```sh
npm run tauri dev              # the app
```

`python main.py` on its own does nothing but tell you that — it has no window.
`python main.py --rpc` runs the backend alone, serving the bridge on stdio,
which is what the shell does for you.

- **Home** — accounts / running / open scripts at a glance, New script,
  Launch all, Stop all, Add account, recent scripts.
- **Editor** — tabbed Lua editor with syntax colours. Tabs, text and the
  active tab persist across relaunches (`editor.json` next to
  `settings.json`). Run targets every running instance ("All") or one.
- **Instances** — create/start/stop/remove accounts, live engine state.
  Tick several rows to launch or stop them together with the launch bay's
  mode.
  Double-click or "Open viewer" attaches a controllable noVNC view; closing
  the viewer only disconnects — instances keep running until explicitly
  stopped.
- **Settings** — profile + theme.

## Frontend development

```sh
npm --prefix frontend run dev    # hot-reload dev server (browser preview)
npm --prefix frontend run build  # what the shell embeds (frontend/dist)
```

The shell bakes `frontend/dist` in at COMPILE time, so a release build must
build the frontend before cargo runs. All three build scripts already do.

**`--release` is not what makes a production build — `--features custom-protocol`
is.** That feature is the only thing that embeds the frontend; without it the
binary loads `build.devUrl` instead and shows "localhost refused to connect"
anywhere Vite is not running. The build scripts pass it and then verify the
hashed asset name actually appears in the binary, because this failed silently
once and looked fine on a machine that had a dev server up.

Add `?mock` to the dev URL for a pretend backend (a few accounts, two
running) so every view has data in a plain browser; see `src/devMock.js`.

## Window chrome

Frameless and transparent on all three platforms, so the page draws its own
33 px rounded sheet and its own titlebar (`TitleBar.jsx`). Every gesture is
handed back to the OS through Tauri (`frontend/src/window.js`), so snapping,
the native move loop and the native size loop all still apply.

| | macOS | Windows | Linux |
|---|---|---|---|
| frame | overlay titlebar, hidden title | undecorated | undecorated |
| controls | native traffic lights, top-**left** | ours, **right** | ours, **right** |
| corner | CSS 33 px | CSS 33 px **+ `SetWindowRgn`** | CSS 33 px |
| resize | native frame | corner/edge strips | corner/edge strips |

Dragging works from the titlebar and nowhere else — a window you can throw
across the desktop by grabbing a list row is a window that fights you.

**The Windows corner needs help, and this is why.** WebView2's windowed
hosting honours alpha 0 but has no *partial* per-pixel alpha, so a page-drawn
`border-radius` alone leaves hard steps at the corners. `src-tauri/src/chrome.rs`
cuts the same 33 px round-rect out of the HWND, anchored to the CLIENT rect
(an undecorated window still carries an invisible ~8 px resize frame, so
cutting the window rect puts the arc in the wrong place). The radius is one
number in three files — `chrome.rs`, `--window-radius` in `styles.css`, and the
pre-mount `html::before` in `index.html` — and they must agree.

Verify a change to any of it by CAPTURING the window (`PrintWindow` with
`PW_RENDERFULLCONTENT`), never against a white backdrop.

## Dev mode — running against a local omni-backend

Every call this app makes to us goes to one server, `http://179.198.197.7`,
through three bases: `/api/v1/…` (auth, accounts, presence), `/omni/dist/…`
(the update manifest and every blob) and `/omni/exec/…` (the remote-execute
bridge). Dev mode rewrites the **origin** of any URL pointing at that IP so it
lands on omni-backend running here instead. Nothing else changes — a URL that
was never ours (Google's platform-tools zip, a hand-set `apiBase`) goes out
exactly as written.

```sh
cd ../omni-backend && npm run dev     # PORT=5500

set OMNI_DEV_SERVER=1                 # -> http://127.0.0.1:5500
python main.py
```

Or, without an env var, `%APPDATA%\omni-executor\dev.json`:

```json
{"devServer": "http://127.0.0.1:5500"}
```

`OMNI_DEV_SERVER=http://10.0.0.4:5500` points at a backend elsewhere on the
LAN; `OMNI_DEV_SERVER=0` turns it off for one run without deleting the file.
Settings → Server prints whatever the api base resolved to, so a redirected
session says so on screen.

**The guest is redirected too.** The in-VM executor's server address is
compiled into its native library, so omnidroid moves the packets instead — one
`iptables` DNAT rule per boot sends the guest's calls to `179.198.197.7` to the
host's backend at `10.0.2.2:5500` (QEMU slirp's alias for the host loopback).
The engine inherits `OMNI_DEV_SERVER` from this app, so one switch moves both
halves; `omnidroid/devserver.py` has the details and `omnidroid/configs/dev.json`
is the file-based equivalent for driving the engine directly.

### It does not ship

`devserver.py` and `omnidroid/devserver.py` are listed in `excludes` in all
three PyInstaller specs, so they are **absent from a release bundle**. Every
call site imports them in a `try/except ImportError` and falls back to the
production server, which means a customer's copy has no code to switch on —
an env var or a dropped-in `dev.json` does nothing there.

To build a bundle that keeps them (the point of the feature: exercising a real
frozen build, updater included, against a local backend):

```powershell
$env:OMNI_DEV_BUILD = "1"      # prints a DO NOT PUBLISH warning
.\build-windows.ps1
```

Never set it for a build that gets published. The setup stub
(`OmniExecutorSetup.spec`) excludes `devserver` unconditionally, with no
opt-in: it is the one file a stranger downloads.
