# Omni Executor

Desktop GUI (pywebview + React) for the **omnidroid** engine — each account is
an isolated, headless Android (Bliss OS) instance managed via QEMU. The GUI
drives the engine exclusively through its CLI (`omnidroid.exe`, JSON output);
see `omnidroid.md` for the engine's own manual.

## Setup

```sh
pip install -r requirements.txt
cd frontend && npm install && npm run build
```

Put `omnidroid.exe` (Windows) or `omnidroid` (Linux) next to `main.py`.
The engine self-bootstraps on first use; the Accounts tab shows a readiness
banner (with a "Run setup" button) if anything is missing.

## Run

```sh
python main.py
```

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
cd frontend && npm run dev     # hot-reload dev server (browser preview)
npm run build                  # what python main.py serves (frontend/dist)
```

Add `?mock` to the dev URL for a pretend backend (a few accounts, two
running) so every view has data in a plain browser; see `src/devMock.js`.

## Window chrome

The titlebar is the frontend's (`TitleBar.jsx`), the window is the OS's:
`windowchrome.py` keeps resize, snap, double-click and shadows native on
each platform. Buttons sit on the right on Windows/Linux; macOS keeps its
traffic lights on the left.

## Dev mode — running against a local omni-backend

Every call this app makes to us goes to one server, `http://72.62.59.232`,
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
`iptables` DNAT rule per boot sends the guest's calls to `72.62.59.232` to the
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
