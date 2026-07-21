# Omni-Executor Resync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resync the omni-executor control panel to the current omnidroid engine — fix the stale CLI calls (create→login, farming+place, derived mode list) and delete the executor's own VNC stack in favor of `omnidroid view`.

**Architecture:** Mostly the pywebview API bridge (`main.py`, invokes `run_engine([...])`) + React (`AccountsView.jsx`). One tiny cross-repo change in omnidroid (expose modes). Net code reduction. Python bridge logic is unit-tested (new suite, `run_engine` mocked); React is gated by `npm run build` + a live GUI smoke.

**Tech Stack:** Python 3 (pywebview bridge), pytest (new — executor has NO test suite today), React/Vite (frontend), omnidroid CLI.

## Global Constraints

- **Two repos.** Task 1 is in **omnidroid** (`/Users/berat/Desktop/Omni Apps/omnidroid`, separate git repo, omnidroid tests git-tracked, `unittest`, `python3 tests/test_x.py`). Tasks 2+ are in **omni-executor** (`/Users/berat/Desktop/Omni Apps/omni-executor`). Commit in the repo you're editing.
- **Executor has NO test suite today.** Establish one under `omni-executor/tests/` using **pytest** (9.1.1 available). Mock the engine: `run_engine` is the single subprocess entry — patch it, never spawn a real engine in a unit test.
- **`run_engine(args, progress=None, timeout=None)`** (main.py) hardcodes `stdin=subprocess.DEVNULL`. Do NOT change its threading model. Token login therefore uses **`--token-file`** (write the pasted token to a private temp file, pass the path, delete after) — NOT `--token-stdin`. Keeps the token out of argv/`ps` and off the shared function.
- **Scope = omni-executor only, EXCEPT the one omnidroid `modes` line** (Task 1). No other omnidroid change. The instance-communication protocol is OUT of scope.
- React has no JS test setup; frontend tasks are verified by `cd frontend && npm run build` passing with NO dead viewer/websockify references, plus the live smoke (Task 8).
- Work on `main` in each repo.

## File Structure

| File | Responsibility | Status |
|---|---|---|
| omnidroid `omnidroid/engine.py` | version report gains `"modes"` | Modify (rep ~4080-4099) |
| omnidroid `tests/test_version_modes.py` | assert version --json has modes incl farming | Create |
| `main.py` | login (browser/token), start (mode+place), view delegate; remove websockify | Modify |
| `omni-executor/tests/test_engine_argv.py` | argv construction + mode derive + JSON shaping | Create |
| `frontend/src/components/AccountsView.jsx` | Add-account (2 paths), mode dropdown from engine, place, View→engine_view | Modify |
| `frontend/src/viewer/ViewerApp.jsx`, `viewer/main.jsx` | the embedded noVNC viewer | **Delete** |
| `requirements.txt` | drop `websockify` | Modify |

---

## Task 1: omnidroid — expose `modes` in the version report (OFFLINE, TDD) [omnidroid repo]

**Files:**
- Modify: `omnidroid/engine.py` (version report dict, ~4080-4099)
- Test: `tests/test_version_modes.py` (omnidroid repo)

**Interfaces:**
- Produces: `omnidroid version --json` output includes `"modes": ["playable","hard","brutal","farming"]` (the live `list(MODES)`).

- [ ] **Step 1: Write the failing test**

Create `omnidroid/tests/test_version_modes.py`:

```python
#!/usr/bin/env python3
"""version --json exposes the engine's mode list (so GUIs derive it).

    python3 tests/test_version_modes.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from omnidroid import engine as omni  # noqa: E402


class VersionModes(unittest.TestCase):
    def test_version_report_has_modes(self):
        # cmd_version builds a report dict; capture it via the JSON path.
        import io
        import json
        from contextlib import redirect_stdout
        buf = io.StringIO()

        class A:
            json = True
        with redirect_stdout(buf):
            omni.cmd_version(A())
        rep = json.loads(buf.getvalue())
        self.assertIn("modes", rep)
        self.assertIn("farming", rep["modes"])
        self.assertEqual(set(rep["modes"]), set(omni.MODES))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run (from omnidroid repo root): `python3 tests/test_version_modes.py`
Expected: FAIL — `AssertionError: 'modes' not found in rep` (or a KeyError). If `cmd_version` doesn't emit clean JSON to stdout in `--json` mode, read `cmd_version` (engine.py ~4080) and adapt the capture to however it emits (it calls `emit_json(rep)`); the assertion on `rep` content is the point.

- [ ] **Step 3: Write minimal implementation**

In `omnidroid/engine.py`, in the version `rep = {...}` dict (~4098, right after the `"commands": [...]` entry), add one line:

```python
           "modes": list(MODES),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_version_modes.py`
Expected: PASS.

- [ ] **Step 5: Full omnidroid suite (no regressions)**

Run: `python3 -m pytest tests/ -q`
Expected: prior baseline + 1 new, all green.

- [ ] **Step 6: Commit** (in the omnidroid repo)

```bash
git add omnidroid/engine.py tests/test_version_modes.py
git commit -m "feat(version): expose modes list in version --json

Lets a GUI (omni-executor) derive its mode picker from the engine instead
of hardcoding it, so farming and any future mode appear automatically."
```

---

## Task 2: Executor test scaffold + `run_engine` mock helper (OFFLINE) [executor]

**Why:** the executor has no tests. Establish the harness ONCE so Tasks 3–6 just add cases.

**Files:**
- Create: `omni-executor/tests/__init__.py` (empty), `omni-executor/tests/conftest.py`, `omni-executor/tests/test_engine_argv.py`

**Interfaces:**
- Produces: a pytest suite importing `main` with a `captured_engine` fixture that patches `main.run_engine` to record the argv it was called with and return a canned dict.

- [ ] **Step 1: Write the harness + a first real assertion**

Create `omni-executor/tests/conftest.py`:

```python
import os
import sys
from unittest import mock
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    """Patch main.run_engine; record argv, return a canned result per call."""
    calls = []
    canned = {"ok": True}

    def fake_run_engine(args, progress=None, timeout=None):
        calls.append(list(args))
        return dict(canned)

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    return calls
```

Create `omni-executor/tests/test_engine_argv.py`:

```python
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


def test_engine_list_argv(captured):
    api = main.Api()
    api.engine_list()
    assert ["list", "--json"] in captured
```

- [ ] **Step 2: Run to verify it passes (harness sanity)**

Run (from executor root): `python3 -m pytest tests/ -q`
Expected: PASS — 1 test. If `main.Api()` needs constructor args or global setup, read `main.py`'s `Api.__init__` and adapt (construct it the way `main()` does). If importing `main` triggers a webview/GUI side effect at module top level, guard the test by importing only what's needed or setting any env the module checks; note the adaptation.

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "test(executor): pytest scaffold + run_engine capture fixture"
```

---

## Task 3: `create`→`login` — browser + token (OFFLINE, TDD) [executor]

**Files:**
- Modify: `main.py` (replace `engine_create` ~342-350; add two methods)
- Test: `omni-executor/tests/test_engine_argv.py`

**Interfaces:**
- Consumes: `run_engine`, the `captured` fixture.
- Produces:
  - `engine_login_browser()` → `run_engine(["login"], ...)`.
  - `engine_login_token(token)` → writes `token` to a private temp file, calls `run_engine(["login", "--token-file", <path>, "--json"], ...)`, deletes the temp file in a `finally`. Rejects an empty/non-str token with `{"ok": False, "error": "bad_token"}`.
  - `engine_create` is REMOVED.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine_argv.py`:

```python
def test_login_browser_argv(captured):
    main.Api().engine_login_browser()
    assert ["login"] in captured


def test_login_token_writes_file_and_calls_login(captured, tmp_path, monkeypatch):
    # force the temp file into tmp_path so we can assert it's cleaned up
    import tempfile
    seen = {}

    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*a, **k):
        fd, path = real_mkstemp(dir=str(tmp_path))
        seen["path"] = path
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
    res = main.Api().engine_login_token("COOKIE123")
    # the login argv used --token-file with a path
    argvs = [c for c in captured if c and c[0] == "login"]
    assert any("--token-file" in c for c in argvs)
    # temp file removed after
    assert not os.path.exists(seen["path"])


def test_login_token_rejects_empty(captured):
    res = main.Api().engine_login_token("")
    assert res["ok"] is False
    assert "token" in res["error"]
    assert captured == []  # never called the engine


def test_engine_create_is_gone():
    assert not hasattr(main.Api, "engine_create")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_engine_argv.py -q`
Expected: FAIL — `AttributeError: 'Api' object has no attribute 'engine_login_browser'` and `test_engine_create_is_gone` fails (still present).

- [ ] **Step 3: Write minimal implementation**

In `main.py`, DELETE `engine_create` (~342-350) and add:

```python
    def engine_login_browser(self):
        """Add an account by interactive Roblox sign-in (omnidroid opens its
        own browser). The account is saved under its Roblox username."""
        return run_engine(["login"], progress=self._progress("login"), timeout=360)

    def engine_login_token(self, token):
        """Add an account from a pasted Roblox cookie/token. Written to a
        private temp file (never the argv/ps) and passed via --token-file."""
        if not isinstance(token, str) or not token.strip():
            return {"ok": False, "error": "bad_token",
                    "message": "A Roblox cookie/token is required."}
        import tempfile
        fd, path = tempfile.mkstemp(prefix="omniexec-tok-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token.strip())
            return run_engine(["login", "--token-file", path, "--json"],
                              progress=self._progress("login"), timeout=120)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
```

(If `self._progress` requires a known account name and "login" isn't one, pass `None` for progress instead — check `_progress`'s signature and use whatever the other methods pass; the argv is what the tests assert.)

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_engine_argv.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_engine_argv.py
git commit -m "feat(executor): replace removed create with login (browser + token-file)

engine_create (dead ['create',name]) -> engine_login_browser (['login']) and
engine_login_token (writes the pasted cookie to a private temp file, passes
--token-file, deletes it). Matches the diskless login-based account model."
```

---

## Task 4: `engine_start` gains mode + place; `engine_modes` derives from version (OFFLINE, TDD) [executor]

**Files:**
- Modify: `main.py` (`engine_start` ~352; add `engine_modes`)
- Test: `tests/test_engine_argv.py`

**Interfaces:**
- Produces:
  - `engine_modes() -> list[str]`: returns `version --json`'s `"modes"` (via `run_engine(["version","--json"])`); on an old engine with no `modes`, returns the fallback constant `["playable","hard","brutal","farming"]`.
  - `engine_start(name, mode=None, place=None)`: argv `["start", name, "--json"]`, plus `["--mode", mode]` when `mode` is a non-empty string (NO hardcoded whitelist — any engine-advertised mode), plus `["--place", str(place)]` when `place` is a non-empty id.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine_argv.py`:

```python
def test_start_with_mode_and_place(captured):
    main.Api().engine_start("alice", mode="farming", place="8737899170")
    start = next(c for c in captured if c and c[0] == "start")
    assert "alice" in start
    assert "--mode" in start and "farming" in start
    assert "--place" in start and "8737899170" in start


def test_start_bare_has_no_mode_or_place(captured):
    main.Api().engine_start("alice")
    start = next(c for c in captured if c and c[0] == "start")
    assert "--mode" not in start
    assert "--place" not in start


def test_engine_modes_from_version(monkeypatch):
    monkeypatch.setattr(main, "run_engine",
                        lambda *a, **k: {"ok": True,
                                         "modes": ["playable", "farming"]})
    assert main.Api().engine_modes() == ["playable", "farming"]


def test_engine_modes_fallback_includes_farming(monkeypatch):
    monkeypatch.setattr(main, "run_engine", lambda *a, **k: {"ok": True})  # no modes
    modes = main.Api().engine_modes()
    assert "farming" in modes
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_engine_argv.py -q`
Expected: FAIL — `engine_modes` missing; `engine_start` rejects `place=`/ignores it.

- [ ] **Step 3: Write minimal implementation**

Replace `engine_start` and add `engine_modes` in `main.py`:

```python
    _FALLBACK_MODES = ["playable", "hard", "brutal", "farming"]

    def engine_modes(self):
        """Mode list DERIVED from the engine's version report; falls back to a
        constant (incl. farming) on an older engine that doesn't report modes."""
        rep = run_engine(["version", "--json"], timeout=30)
        modes = rep.get("modes") if isinstance(rep, dict) else None
        return modes if isinstance(modes, list) and modes else list(self._FALLBACK_MODES)

    def engine_start(self, name, mode=None, place=None):
        error = self._bad_name(name)   # existing validator (main.py ~286)
        if error:
            return error
        args = ["start", name, "--json"]
        if isinstance(mode, str) and mode.strip():
            args += ["--mode", mode.strip()]
        if place is not None and str(place).strip():
            args += ["--place", str(place).strip()]
        result = run_engine(args, progress=self._progress(name), timeout=300)
        self._push("accounts-changed", {})
        return result
```

(Use the SAME name-validation the current `engine_start` uses — read lines ~352-360 and preserve it. If it validated inline rather than via a helper, keep that inline. The tests don't exercise validation, but don't drop it.)

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_engine_argv.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_engine_argv.py
git commit -m "feat(executor): start --mode (any engine mode) + --place; derive modes

engine_modes() reads the version report's modes (fallback incl farming).
engine_start passes --mode for any advertised mode and --place for a join."
```

---

## Task 5: Delete the VNC stack; delegate View to omnidroid (OFFLINE, TDD) [executor]

**Files:**
- Modify: `main.py` (remove `_ensure_proxy`, `_proxies`, `open_viewer`'s websockify path, `_shutdown` teardown; add `engine_view`)
- Modify: `requirements.txt` (drop `websockify`)
- Delete: `frontend/src/viewer/ViewerApp.jsx`, `frontend/src/viewer/main.jsx`
- Test: `tests/test_engine_argv.py`

**Interfaces:**
- Produces: `engine_view(name)` → `run_engine(["view", name, "--start"], ...)` fire-and-forget; returns the result, surfaces a non-ok cleanly. `open_viewer`, `_ensure_proxy`, `_proxies` removed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine_argv.py`:

```python
def test_engine_view_argv(captured):
    main.Api().engine_view("alice")
    assert ["view", "alice", "--start"] in captured


def test_websockify_machinery_removed():
    assert not hasattr(main.Api, "_ensure_proxy")
    assert not hasattr(main.Api, "open_viewer")
    assert not hasattr(main.Api, "viewer_close")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_engine_argv.py -q`
Expected: FAIL — `engine_view` missing; `open_viewer`/`_ensure_proxy` still present.

- [ ] **Step 3: Write minimal implementation**

In `main.py`, remove the whole embedded-viewer machinery (it's larger than just the proxy — confirm each by reading):
- `_ensure_proxy` (~385), `open_viewer` (~426), `viewer_close` (~496).
- In `__init__` (~218-223): the `self._viewers = {}`, `self._proxies = {}`, and `self._proxy_lock = threading.Lock()` lines (all three exist only for the embedded viewer).
- The websockify/viewer teardown inside `_shutdown` (~513) — keep the REST of `_shutdown` (any non-viewer cleanup).
- Any now-unused imports (`websockify` is spawned via `sys.executable -m websockify`, and `threading` may still be used elsewhere — only drop an import if grep shows no other use).
- Add:

```python
    def engine_view(self, name):
        """Open omnidroid's own native viewer window onto the instance
        (--start boots it if stopped). Fire-and-forget; the engine owns the
        window. We only report a launch failure."""
        res = run_engine(["view", name, "--start"], timeout=60)
        if isinstance(res, dict) and res.get("ok") is False:
            return res
        return {"ok": True}
```

In `requirements.txt`, delete the `websockify>=0.11` line (and its comment).
Delete the files `frontend/src/viewer/ViewerApp.jsx` and `frontend/src/viewer/main.jsx`.

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_engine_argv.py -q`
Expected: PASS. Grep to confirm no lingering refs: `grep -rn "websockify\|_ensure_proxy\|open_viewer\|_proxies" main.py` → only comments/none.

- [ ] **Step 5: Commit**

```bash
git add main.py requirements.txt
git rm frontend/src/viewer/ViewerApp.jsx frontend/src/viewer/main.jsx
git commit -m "refactor(executor): delete own VNC stack, delegate View to omnidroid

Remove websockify bridge + embedded noVNC viewer; engine_view runs
`omnidroid view <name> --start` (engine's native window). Net code removal."
```

---

## Task 6: Frontend wiring — Add-account (2 paths), mode dropdown, place, View (BUILD-GATED) [executor]

**Files:**
- Modify: `frontend/src/components/AccountsView.jsx`
- (No JS unit tests exist; gate is `npm run build` + the live smoke.)

**Interfaces:**
- Consumes: `engine_login_browser`, `engine_login_token`, `engine_modes`, `engine_start(name,mode,place)`, `engine_view` (via the `api(...)` bridge).

- [ ] **Step 1: Rewire the component**

In `frontend/src/components/AccountsView.jsx`:
- Replace the name-typed create (calls `api("engine_create", name)`, ~128) with an "Add account" that offers TWO actions: "Sign in with browser" → `api("engine_login_browser")`; "Paste cookie" → a textarea whose value goes to `api("engine_login_token", token)`. Refresh the list on success (the component already listens for `accounts-changed`). Remove the account-name input.
- Populate the "Performance mode" dropdown (~367-371) from `api("engine_modes")` (fetch on mount) instead of a hardcoded list, so `farming` appears. Keep `launchOpts.mode` as the selected value.
- Add a "Place ID (optional)" input to `launchOpts` and pass it: launch calls `api("engine_start", name, launchOpts.mode, launchOpts.place)`.
- Replace `openViewer` (~141-142, `api("open_viewer", name)`) with `api("engine_view", name)`. The button label/handler stays "View"/"Open viewer" but now opens the engine's window. Remove the "Hide this window once the viewer opens" option (~398) — there is no in-app viewer to hide anymore.

- [ ] **Step 2: Build the frontend (the offline gate)**

Run: `cd frontend && npm run build`
Expected: build SUCCEEDS. Then confirm no dead references remain:
`grep -rn "engine_create\|open_viewer\|noVNC\|novnc\|websockify\|ViewerApp" frontend/src` → nothing (or only removed-file-free matches).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AccountsView.jsx
git commit -m "feat(executor-ui): add-account (browser/paste), engine modes, place, View

Add-account offers browser sign-in or paste-cookie; mode dropdown is derived
from engine_modes (farming appears); a place-id input feeds --place; View
calls engine_view (omnidroid's window). No embedded viewer to hide anymore."
```

---

## Task 7: 1C audit — verify remaining calls against the engine (VERIFICATION) [executor]

**Deliverable:** a short `docs/superpowers/runbooks/C-cli-audit.md` recording that each remaining `engine_*` call matches the current engine, plus any fix commits.

- [ ] **Step 1** — For each of `engine_version`, `engine_doctor`, `engine_bases`, `engine_use_base`, `engine_setup`, `engine_list`, `engine_stop`, `engine_remove`, and any `session`/`accounts` call: compare its argv + expected JSON keys against the current omnidroid subparser (read the omnidroid engine's `add_parser("<cmd>")` + its `--json` output). Record match/mismatch in the runbook.
- [ ] **Step 2** — For any mismatch found, fix it in `main.py` with a matching pytest case in `tests/test_engine_argv.py` (argv assertion), commit per fix.
- [ ] **Step 3** — Commit the audit runbook: `git add docs/superpowers/runbooks/C-cli-audit.md && git commit -m "docs(executor): C1 CLI audit — remaining calls verified vs current engine"`.

---

## Task 8: LIVE GUI smoke runbook (MANUAL — user, on the Mac)

**Deliverable:** `docs/superpowers/runbooks/C-gui-smoke.md` with recorded results. NOT a unit test — the pywebview+React app can't be driven headless.

- [ ] **Step 1** — Write the runbook: launch `python main.py`; (a) Add account via **browser** → sign in → account appears; (b) Add account via **paste cookie** → account appears; (c) Start an account with a **mode** (incl. farming) and a **place id** → it boots and joins; (d) click **View** → omnidroid's native window opens onto the instance; (e) **Stop**; (f) **Remove**. Record pass/fail + notes per step.
- [ ] **Step 2** — (User) run it on the Mac; record results.
- [ ] **Step 3** — Commit: `git add docs/superpowers/runbooks/C-gui-smoke.md && git commit -m "docs(executor): C live GUI smoke results"`.

---

## Self-review notes (for the executor)

- **Two repos:** Task 1 commits in omnidroid; Tasks 2–8 in omni-executor. Don't cross the streams in one commit.
- **Token safety:** `--token-file` + temp-file-deleted-in-finally is deliberate (run_engine is DEVNULL; keeps the cookie out of argv/ps). Do NOT switch to `--token-stdin` without reworking run_engine's stderr-pump threading.
- **Offline vs gated:** Tasks 1–5, 7 are TDD (pytest/unittest). Task 6 (React) has no JS tests here — its gate is `npm run build` + Task 8's live smoke. Task 8 is manual (GUI).
- **Preserve, don't rewrite:** keep `engine_start`'s existing name validation and `_shutdown`'s non-websockify cleanup. The plan removes only the viewer/websockify pieces.
- **Adapt to real signatures:** `Api()` construction, `_progress`, and `_validate_name` specifics must be read from the live `main.py` — the plan flags each spot where the implementer confirms against the actual code.
