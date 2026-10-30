# Omni Executor

Desktop app for **native omnidroid**: every saved Roblox account launches the
executor APK (`OmniExecutor-v0.apk`) on omnidroid's warm Androids -- real
Android userspace run as ordinary desktop processes, no VM, no QEMU, no adb --
in a window of its own. There is one kind of launch: no Gaming/Farming modes,
no GPU policy, no base images, no x86/arm choice.

The app drives omnidroid over its MCP server (`droid.py`): it starts
`omnidroid serve` as its own child (the pool ends with the app) or uses the
pool already serving on port 7457 if the omnidroid app is open. A launch is
`start_instance {apk, options: "--cookie <file> --place <id>"}` -- the
account's cookie goes to omnidroid's roblox plugin as a private FILE in the
runtime dir, never on a command line, and is deleted when the instance stops.

## How it is put together

Three pieces, and it is worth knowing which is which before changing anything:

| | what it is | where |
|---|---|---|
| **shell** | a Tauri (Rust) app: the window, and nothing else | `src-tauri/` |
| **frontend** | React + Tailwind, embedded in the shell at compile time | `frontend/` |
| **backend** | Python: the whole `Api`, and the omnidroid client (`droid.py`) | `main.py` and friends |

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

Running from source needs no download: `droid.py` finds the sibling
`../omnidroid` checkout's release build and `../omni-exec-android/OmniExecutor-v0.apk`.
A shipped app downloads both on first launch from the dist API's `native`
channel (`bootstrap.py`): the omnidroid bundle to `<runtime>/omnidroid` and the
APK to `<runtime>/apk/OmniExecutor.apk`.

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
- **Accounts** — add/create/remove accounts and launch or stop them. Tick
  several rows to launch or stop them together. Each row shows omnidroid's
  own state for its instance (waiting, installing, starting, running).
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

**The guests are not redirected.** The QEMU engine moved the guest's packets
to the local backend with an `iptables` rule; native omnidroid has no such
hook. The app still hands `OMNI_DEV_SERVER` to the pool it starts, so a
redirect added to omnidroid later picks the setting up with no change here.

### It does not ship

`devserver.py` is listed in `excludes` in all
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
