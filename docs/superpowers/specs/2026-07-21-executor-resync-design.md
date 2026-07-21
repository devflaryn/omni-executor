# Omni-Executor Resync: CLI Sync + VNC Delegation — Design

**Date:** 2026-07-21
**Status:** Approved design, not yet implemented
**Repo:** omni-executor only. Spec C of the omni-apps brainstorm.

## Problem

The omni-executor control panel (pywebview + React GUI that drives the omnidroid
engine over its CLI) has drifted from the current engine, in two ways the user
reported:

1. **Stale omnidroid commands.** The executor calls engine commands that have
   changed or been removed. The headline: `engine_create` calls
   `["create", name]`, but omnidroid REMOVED the `create` command — the diskless
   account rework made account creation happen through `login` ("the only way to
   create a profile is to log in"). Also the start mode list is hardcoded and
   missing `farming` (added in B1), and start can't pass a `--place`.
2. **Its own VNC viewer.** The executor embeds the live instance view INSIDE its
   own app window via a noVNC + websockify stack it maintains itself, instead of
   using omnidroid's viewer. The user wants one viewer, engine-owned.

Spec C is a **resync + simplification** — no new subsystems, a net code
reduction. It stays strictly inside `omni-executor/`.

## Scope & decisions (locked in brainstorming)

- **Two issues only.** CLI sync (Issue 1) + VNC delegation (Issue 2).
- **Scope = omni-executor, plus ONE minimal omnidroid line.** The work is inside
  `omni-executor/` EXCEPT a single deliberate exception: omnidroid's
  `version --json` report gains a `"modes"` field so the executor can DERIVE its
  mode list instead of hardcoding it (user-approved). That is one line in
  omnidroid, its own commit, in the spirit of "the CLI contract the executor
  syncs to." No other omnidroid change.
- **Out of scope (user-stated):** the instance-communication protocol
  (bootstrapper command channel) — not ready, do not design it. And every other
  directory / any further omnidroid change.
- **Issue 2 = delegate, don't rebuild.** The executor drops its own viewing
  stack and shells out to `omnidroid view <name>`, which opens omnidroid's OWN
  native window (macOS Screen Sharing / a VNC client). The live view is no
  longer embedded in the control panel; that tradeoff is accepted.
- **Issue 1 create→login = two paths.** "Add account" offers (a) browser
  sign-in and (b) paste-cookie, BOTH invoking `omnidroid login` (which
  auto-discovers the Roblox username and saves the account). The executor no
  longer invents account names.
- **Mode list derived from the engine**, not hardcoded, so `farming` and any
  future mode appear automatically without re-drift.
- **`engine_view` passes `--start`** so "View" also boots a stopped instance,
  matching today's implicit behavior.

## Current state (grounded in the code)

- `main.py` is the pywebview API bridge; it invokes the engine via
  `run_engine([...])` (subprocess to `omnidroid(.exe)` next to `main.py`).
  Calls today: `version`, `doctor`, `bases`, `use-base`, `setup`, `list`,
  `accounts`, `create` (BROKEN), `start`, `stop`, `remove`, `session`.
- Account creation: `engine_create(name)` → `["create", name, "--json"]`
  (`main.py:342/348`). `create` no longer exists in omnidroid; `login` (with
  the shared `--token/--token-file/--token-stdin` flags + a default browser
  flow) is the replacement.
- Start: `engine_start(name, mode)` (`main.py:352`) hardcodes
  `mode in ("playable","hard","brutal")` and passes no `--place`.
- VNC: `open_viewer` (`main.py:426`) → `_ensure_proxy` (`main.py:385`) starts a
  `websockify` bridge, then opens a pywebview window rendering
  `frontend/src/viewer/ViewerApp.jsx` (noVNC, 150 lines) + `viewer/main.jsx`.
  `_shutdown` (`main.py:513`) tears the websockify proxies down. `websockify` is
  in `requirements.txt`.
- omnidroid's viewer: `view` command (native window;
  macOS Screen Sharing / `_vnc_viewer_command` external client / its own
  `_vncview` capture window). Flags: `name`, `--start`, `--mode`, `--dev`,
  `--native`, `--viewer`, `--timeout`, `--json`. NO websockify/noVNC bridge
  exists in omnidroid — that is why "move to omnidroid's viewer" means
  delegating to a separate window, not embedding.

## Component 0 — Expose modes from omnidroid (the one omnidroid line)

- In omnidroid's `version` report dict (engine.py ~4098, the `rep = {...}` that
  already carries `"contract"` and `"commands"`), add `"modes": list(MODES)`.
- One line, its own commit in the omnidroid repo, offline-unit-testable there
  (assert `version --json` includes `"modes"` containing `farming`). This is the
  ONLY omnidroid change in Spec C; everything else is `omni-executor/`.

## Component 1 — CLI sync (Issue 1)

### 1A. `create` → `login` (the one real feature change)
- Replace `engine_create(name)` with a login-based add path. New API methods:
  - `engine_login_browser()` → `["login"]` (default browser sign-in; omnidroid
    opens its own browser). Return the engine result; UI refreshes the account
    list after.
  - `engine_login_token(token)` → `["login", "--token-stdin", "--json"]` with
    `token` piped to stdin. Username resolved from the token by the engine.
- `AccountsView.jsx`: the "create account (type a name)" UI becomes "Add
  account" with two choices — "Sign in with browser" and "Paste cookie". No
  name field (login auto-names).

### 1B. Start options
- `engine_start(name, mode=None, place=None)`:
  - mode: pass `--mode <mode>` for ANY mode the engine advertises (do NOT
    hardcode the trio). Include `farming`.
  - place: when `place` is a valid id, pass `--place <id>`.
- The mode list the UI renders is DERIVED from the engine. **This requires the
  one-line omnidroid change** (Component 0 below): `version --json` gains
  `"modes": list(MODES)`. The executor reads that list from the `version`
  handshake it ALREADY calls (`engine_version`, `main.py:296`) and renders the
  mode picker from it — so `farming` and any future mode appear with zero
  executor change. If an OLDER engine (no `modes` field) is detected, fall back
  to a constant that INCLUDES `farming`, with a code comment marking it a
  legacy fallback.

### 1C. Audit the rest
- Verify `version`, `doctor`, `bases`, `use-base`, `setup`, `list`, `stop`,
  `remove`, `session`, `accounts` against current signatures + JSON shapes; fix
  any drift found. Success criterion: every `engine_*` call succeeds against the
  current engine (proven by the live smoke pass).
- Keep the existing `version --json` handshake (`engine_version`) as the
  compatibility guard.

## Component 2 — VNC delegation (Issue 2)

### 2A. Delete the executor's viewing stack
- Remove `frontend/src/viewer/ViewerApp.jsx` + `frontend/src/viewer/main.jsx`.
- Remove from `main.py`: `_ensure_proxy`, the `_proxies` dict, `open_viewer`'s
  websockify+webview-window path, and the websockify teardown in `_shutdown`.
- Remove `websockify` from `requirements.txt`; remove bundled noVNC assets.
- Remove any Vite/build entry for the viewer window.

### 2B. Delegate "View" to omnidroid
- New `engine_view(name)` in `main.py` → `["view", name, "--start"]`,
  fire-and-forget (it opens omnidroid's native window and returns after
  launching). `--start` boots a stopped instance, matching today's behavior.
- Surface a clean error if `view` fails (e.g. non-zero exit) rather than
  silently doing nothing.
- `AccountsView.jsx`: the "View"/"Open viewer" button calls `engine_view`.

## Verification

**Offline (new lightweight tests for the changed bridge logic):**
- The executor has no test suite today; add a small one for the pure pieces:
  argv construction for `login` (browser vs token), `start --mode/--place`,
  `view --start`; the engine-derived mode list (includes farming, never
  hardcodes the old trio); JSON-response shaping. These are pure-ish (mock
  `run_engine`/subprocess) and don't need the GUI.
- Frontend builds clean: `cd frontend && npm run build` succeeds with NO dead
  references to the removed viewer/websockify.

**Live (user, on the Mac — the GUI can't be driven headless):**
- Smoke runbook: launch the app → Add account via BOTH browser and paste-cookie
  → the account appears → Start it with a mode (incl. farming) and a place →
  "View" opens omnidroid's native window onto the running instance → Stop →
  Remove. Record pass/fail per step.

## Risks

- **Other undiscovered CLI drift** beyond create/mode/place. Mitigated by the
  1C audit + the live smoke pass exercising every `engine_*` call.
- **Losing the embedded view is a UX change** (separate window now). Accepted by
  the user; it's the point of delegating to one engine-owned viewer.
- **`omnidroid view` host behavior varies** (macOS Screen Sharing vs a VNC
  client vs its own capture window). The executor only launches it; whatever
  omnidroid opens is what the user sees. `engine_view` surfaces a launch error
  but does not try to manage the window.
- **login browser flow is omnidroid's own window**, outside the control panel —
  expected, matches the "delegate to the engine" theme.

## Out of scope

- The instance-communication / bootstrapper command protocol (not ready).
- Any change outside `omni-executor/`.
- Reworking the executor's own app-account login (the user's separate concern;
  not one of the two issues).
- Rebuilding an embedded viewer (explicitly rejected — delegate instead).

## Sequencing

1. Component 0 (omnidroid `"modes"` line) — tiny, unblocks the derived mode
   list; its own omnidroid commit.
2. Component 1A (create→login) — the one broken feature; unblocks adding
   accounts against the current engine.
3. Component 1B/1C (start options + derived mode list + audit) — the rest of
   the CLI sync.
4. Component 2 (VNC delegation) — the deletion + `engine_view`.
5. Frontend build check + the live smoke runbook.
