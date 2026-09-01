"""Auto-update: version comparison, what counts as an update, and the guards.

The parts worth testing hard are the ones that decide whether to REPLACE
something. A version comparison that gets a tie wrong reinstalls forever; one
that treats a garbled value as newer hands a self-replace to whatever the
server happened to return.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402
import updates  # noqa: E402


# --------------------------------------------------------------- versions

@pytest.mark.parametrize("candidate,current,expected", [
    ("1.0.1", "1.0.0", True),
    ("1.1.0", "1.0.9", True),
    ("2.0.0", "1.9.9", True),
    ("1.0.10", "1.0.9", True),          # not string ordering
    ("1.0.0", "1.0.0", False),          # a tie is not an update
    ("1.0.0", "1.0.1", False),          # older
    ("1.0.1-beta", "1.0.0", True),      # suffixes tolerated
    ("", "1.0.0", False),
    (None, "1.0.0", False),
    ("garbage", "1.0.0", False),        # unparseable never wins
    ("1.0.1", None, True),              # no current version: anything is newer
])
def test_is_newer(candidate, current, expected):
    assert updates.is_newer(candidate, current) is expected


def test_an_unreadable_server_version_never_triggers_a_replace():
    """The safe direction. This value decides whether the app overwrites
    itself, so anything it cannot parse must sort LOW, not high."""
    for junk in ("", "latest", "v?", "..", None, "NaN"):
        assert not updates.is_newer(junk, "1.0.0")


# ------------------------------------------------------------------ check

def _manifest(app_version=None, app_artifact=True, runtime_artifacts=()):
    artifacts = list(runtime_artifacts)
    if app_artifact:
        artifacts.append({
            "name": "app-win", "kind": "app", "version": app_version,
            "bytes": 1234, "sha256": "a" * 64, "url": "/omni/dist/blob/app-win",
            "dest": "app", "unpack": "zip",
        })
    return {"ok": True, "app": {"version": app_version}, "artifacts": artifacts}


@pytest.fixture
def offline_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "runtime_dir", lambda: tmp_path)
    # bootstrap.configure_engine() sets OMNI_IMAGES_DIR on os.environ for the
    # whole PROCESS, not through monkeypatch — so any earlier test that called
    # it leaves the variable set, and manages_runtime() then reads a foreign
    # image dir. Start every test here from a known state.
    monkeypatch.delenv("OMNI_IMAGES_DIR", raising=False)
    return tmp_path


def test_check_reports_an_app_update(offline_runtime, monkeypatch):
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *a, **k: _manifest("1.0.1"))
    r = updates.check()
    assert r["ok"] is True
    assert r["app"]["update"] is True
    assert r["app"]["available"] == "1.0.1"
    assert r["runtime"]["update"] is False


def test_check_is_quiet_when_current(offline_runtime, monkeypatch):
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.1")
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *a, **k: _manifest("1.0.1"))
    assert updates.check()["app"]["update"] is False


def test_an_announced_version_with_no_build_is_not_offered(offline_runtime, monkeypatch):
    """`app.version` alone is just a number. Offering an update the client
    cannot actually fetch is worse than saying nothing."""
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(bootstrap, "read_manifest",
                        lambda *a, **k: _manifest("9.9.9", app_artifact=False))
    r = updates.check()
    assert r["app"]["update"] is False


def test_offline_is_reported_as_such_not_as_up_to_date(offline_runtime, monkeypatch):
    def boom(*a, **k):
        raise bootstrap.BootstrapError("manifest fetch failed: unreachable")
    monkeypatch.setattr(bootstrap, "read_manifest", boom)
    r = updates.check()
    assert r["ok"] is False
    assert "unreachable" in r["error"]
    # Crucially NOT "no update available" — a machine that could not ask is not
    # a machine that is current.
    assert r["app"]["update"] is False and r["runtime"]["update"] is False


def test_check_reports_stale_runtime_artifacts(offline_runtime, monkeypatch):
    runtime = [{"name": "base-x86", "version": "b1", "bytes": 10, "sha256": "b" * 64,
                "url": "/x", "dest": "images/x86"}]
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.1")
    monkeypatch.setattr(bootstrap, "read_manifest",
                        lambda *a, **k: _manifest("1.0.1", runtime_artifacts=runtime))
    r = updates.check()
    assert r["runtime"]["update"] is True
    assert [a["name"] for a in r["runtime"]["artifacts"]] == ["base-x86"]
    assert r["runtime"]["bytes"] == 10


# ----------------------------------------------------------- the app blob

def test_an_app_build_is_never_in_the_first_boot_plan():
    """A fresh install already has the app it is running. Pulling another copy
    of it during first boot would be tens of megabytes for nothing, and would
    place a second app inside the runtime dir where nothing looks for it."""
    manifest = _manifest("1.0.1", runtime_artifacts=[
        {"name": "base-x86", "sha256": "c" * 64, "url": "/x", "dest": "images/x86"},
    ])
    plan = bootstrap.plan_downloads(manifest, {"artifacts": {}})
    assert [a["name"] for a in plan] == ["base-x86"]


# --------------------------------------------------------------- staging

def test_staged_info_is_none_without_a_staged_build(offline_runtime):
    assert updates.staged_info() is None


def test_staged_info_ignores_a_receipt_whose_build_is_gone(offline_runtime):
    root = updates.staging_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "staged.json").write_text(json.dumps(
        {"version": "1.0.1", "build": str(root / "vanished")}), encoding="utf-8")
    # The receipt says a build is ready; the directory is not there. Reporting
    # it would offer an Install button that cannot possibly work.
    assert updates.staged_info() is None


def test_staged_build_prefers_the_declared_root(tmp_path):
    unpacked = tmp_path / "unpacked"
    (unpacked / "omni-exec").mkdir(parents=True)
    (unpacked / "__MACOSX").mkdir()
    assert updates._staged_build(unpacked, "omni-exec") == unpacked / "omni-exec"


def test_staged_build_refuses_to_guess_between_several(tmp_path):
    unpacked = tmp_path / "unpacked"
    (unpacked / "one").mkdir(parents=True)
    (unpacked / "two").mkdir()
    # Guessing here is how the wrong tree gets copied over a working install.
    assert updates._staged_build(unpacked, None) is None


# ---------------------------------------------------------------- guards

def test_running_from_source_cannot_apply_an_app_update(monkeypatch):
    monkeypatch.setattr(updates, "app_dir", lambda: None)
    ok, reason = updates.can_apply_app()
    assert ok is False
    assert "source" in reason.lower()
    with pytest.raises(updates.UpdateError):
        updates.launch_apply()


def test_applying_refuses_to_replace_a_directory_with_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "app_dir", lambda: tmp_path)
    with pytest.raises(updates.UpdateError, match="itself"):
        updates.apply_staged_app(tmp_path, 999999)


def test_applying_refuses_while_the_old_app_is_still_alive(tmp_path, monkeypatch):
    """Replacing a directory a process is running from is how you get a
    half-broken install and no way back."""
    import os
    monkeypatch.setattr(updates, "app_dir", lambda: tmp_path / "new")
    (tmp_path / "new").mkdir()
    (tmp_path / "old").mkdir()
    with pytest.raises(updates.UpdateError, match="still running"):
        updates.apply_staged_app(tmp_path / "old", os.getpid(), timeout=1)


def test_a_staged_build_that_is_no_longer_newer_is_not_offered(offline_runtime, monkeypatch):
    """The state right after a successful update: the staging dir outlives the
    swap, so without this the app offers to install the version it already is,
    forever."""
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.1")
    root = updates.staging_root()
    build = root / "unpacked" / "omni-exec"
    build.mkdir(parents=True)
    (root / "staged.json").write_text(json.dumps(
        {"version": "1.0.1", "build": str(build)}), encoding="utf-8")
    assert updates.staged_info() is None

    # ...but a genuinely newer one still is.
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.0")
    assert updates.staged_info()["version"] == "1.0.1"


def test_discarding_a_staged_build_removes_it(offline_runtime, monkeypatch):
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.0")
    root = updates.staging_root()
    (root / "unpacked" / "omni-exec").mkdir(parents=True)
    (root / "staged.json").write_text(json.dumps(
        {"version": "1.0.1", "build": str(root / "unpacked" / "omni-exec")}),
        encoding="utf-8")
    assert updates.staged_info() is not None
    updates.discard_staged()
    assert updates.staged_info() is None
    assert not root.exists()


# ------------------------------------------------- images we do not manage

def test_a_checkout_with_its_own_images_is_not_offered_a_runtime_update(
        offline_runtime, monkeypatch):
    """Every dev machine here points the engine at its own image directory.
    Downloading gigabytes into the runtime dir would not change a single byte
    the engine boots, while reporting success."""
    monkeypatch.setenv("OMNI_IMAGES_DIR", str(offline_runtime / "elsewhere"))
    (offline_runtime / "elsewhere").mkdir()
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.1")
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *a, **k: _manifest(
        "1.0.1", runtime_artifacts=[{"name": "base-x86", "sha256": "d" * 64,
                                     "url": "/x", "dest": "images/x86"}]))
    r = updates.check()
    assert r["runtime"]["update"] is False
    assert r["runtime"]["managed"] is False
    assert "does not manage" in r["runtime"]["reason"]


def test_the_managed_runtime_dir_is_still_offered(offline_runtime, monkeypatch):
    monkeypatch.setenv("OMNI_IMAGES_DIR", str(offline_runtime / "images"))
    monkeypatch.setattr(updates, "APP_VERSION", "1.0.1")
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *a, **k: _manifest(
        "1.0.1", runtime_artifacts=[{"name": "base-x86", "sha256": "d" * 64,
                                     "url": "/x", "dest": "images/x86"}]))
    r = updates.check()
    assert r["runtime"]["managed"] is True
    assert r["runtime"]["update"] is True


# ------------------------------------------------------ first-boot gating

def test_a_published_app_build_does_not_make_an_installed_runtime_look_incomplete(
        offline_runtime, monkeypatch):
    """REGRESSION, seen live the first time an app build was published.

    bootstrap_status() decided readiness by comparing every manifest entry
    against installed.json. `app-win` is in the manifest and is never recorded
    there, so a fully-installed machine was judged incomplete and dropped into
    the first-boot setup screen — where it began re-downloading the base
    images it already had.
    """
    manifest = _manifest("1.0.1", runtime_artifacts=[
        {"name": "base-x86", "sha256": "e" * 64, "url": "/x", "dest": "images/x86"},
        # a pointer artifact, which is also never recorded
        {"name": "qemu-win", "sha256": None, "url": "/q", "dest": "qemu"},
    ])
    installed = {"artifacts": {"base-x86": {"sha256": "e" * 64}}}
    # Everything installable is installed -> nothing to do -> ready.
    assert bootstrap.plan_downloads(manifest, installed) == []


# ------------------------------------------------------------- the swap

def _seed(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_the_swap_survives_an_install_folder_that_cannot_be_renamed(
        tmp_path, monkeypatch):
    r"""THE REPORTED FAILURE, end to end.

        [WinError 32] The process cannot access the file because it is being
        used by another process: '...\Programs\OmniExecutor'
            -> '...\Programs\OmniExecutor.old'

    Logged three times on one machine (2026-08-16, -18, -22) and surfaced as a
    PyInstaller crash dialog each time. The holder is ordinary: QEMU is spawned
    detached and outlives the app by design, every instance runs a `_windowlock`
    that is omni-exec.exe out of the install folder, and all of them inherit
    that folder as their working directory -- so anyone with an instance up
    could never update. The swap must not depend on moving the directory.
    """
    import os
    staged = _seed(tmp_path / "staged" / "omni-exec",
                   {"omni-exec.exe": "v2", "_internal/py.dll": "v2 dll"})
    target = _seed(tmp_path / "OmniExecutor",
                   {"omni-exec.exe": "v1", "_internal/py.dll": "v1 dll",
                    "_internal/dropped-in-v2.pyd": "stale"})

    real_rename = os.rename

    def no_directory_moves(src, dst, *a, **k):
        if Path(src).is_dir():
            raise OSError(32, "used by another process")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", no_directory_moves)
    monkeypatch.setattr(updates, "app_dir", lambda: staged)
    monkeypatch.setattr(updates.time, "sleep", lambda _s: None)
    monkeypatch.setattr(updates, "discard_staged", lambda: None)
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: None)

    result = updates.apply_staged_app(target, 999999, log=lambda _m: None)

    assert result["ok"] is True
    assert (target / "omni-exec.exe").read_text(encoding="utf-8") == "v2"
    assert (target / "_internal" / "py.dll").read_text(encoding="utf-8") == "v2 dll"
    assert not (target / "_internal" / "dropped-in-v2.pyd").exists(), \
        "a replace, not a merge"


def test_a_failed_swap_leaves_the_working_build_in_place(tmp_path, monkeypatch):
    """What the user is promised when it does go wrong: the version they
    already have, exactly as it was."""
    staged = _seed(tmp_path / "staged" / "omni-exec",
                   {"omni-exec.exe": "v2", "_internal/py.dll": "v2 dll"})
    target = _seed(tmp_path / "OmniExecutor",
                   {"omni-exec.exe": "v1", "_internal/py.dll": "v1 dll"})

    monkeypatch.setattr(updates, "app_dir", lambda: staged)
    monkeypatch.setattr(updates.time, "sleep", lambda _s: None)
    monkeypatch.setattr(bootstrap.shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "no space")))

    with pytest.raises(OSError):
        updates.apply_staged_app(target, 999999, log=lambda _m: None)

    assert (target / "omni-exec.exe").read_text(encoding="utf-8") == "v1"
    assert (target / "_internal" / "py.dll").read_text(encoding="utf-8") == "v1 dll"


def test_a_failed_swap_never_reaches_the_interpreter(tmp_path, monkeypatch):
    """`--apply-update` runs in a windowed build with no console, so an
    exception escaping it is the "Failed to execute script 'main'" traceback
    dialog the user was shown. It must be a message and a relaunch instead."""
    import main

    target = _seed(tmp_path / "OmniExecutor", {"omni-exec.exe": "v1"})
    shown, launched = [], []

    monkeypatch.setattr(main, "_fatal_dialog",
                        lambda title, msg: shown.append((title, msg)))
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(updates, "apply_staged_app",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(32, "used by another process")))
    monkeypatch.setattr(updates, "_executable_in",
                        lambda t: Path(t) / "omni-exec.exe")
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda cmd, **k: launched.append(cmd))

    code = main._apply_update_mode(["omni-exec.exe", "--apply-update",
                                    str(target), "999999"])

    assert code == 1
    assert shown, "the user is told, in a dialog, not a traceback"
    assert launched, "and put back on the version they already had"
    assert "used by another process" in (tmp_path / "update.log").read_text(
        encoding="utf-8"), "and it is still written down"


# --------------------------------------------------- which binary does what
#
# A build directory holds TWO executables since the move off pywebview:
# omni-exec (the Tauri shell, which owns the window) and omni-exec-py (the
# frozen Python backend, which is also the omnidroid engine). Handing
# `--apply-update` to the wrong one opens a window and updates nothing, so the
# updater has to tell them apart.

def _win(monkeypatch):
    monkeypatch.setattr(updates.sys, "platform", "win32")


def test_the_updater_is_the_backend_not_the_shell(tmp_path, monkeypatch):
    _win(monkeypatch)
    build = tmp_path / "build"
    build.mkdir()
    (build / "omni-exec.exe").write_bytes(b"MZ")
    (build / "omni-exec-py.exe").write_bytes(b"MZ")

    # `--apply-update` is a mode of main.py, and only the backend has it.
    assert updates._updater_in(build).name == "omni-exec-py.exe"
    # What the user launches, and what the swap relaunches afterwards.
    assert updates._executable_in(build).name == "omni-exec.exe"


def test_a_pre_tauri_build_can_still_be_updated_from(tmp_path, monkeypatch):
    """The migration release has to be applicable BY a pywebview client, whose
    own directory has only omni-exec.exe — and that binary does understand
    `--apply-update`, because back then it was the whole app. Without this
    fallback the one update that carries the new shell is the one that cannot
    be installed."""
    _win(monkeypatch)
    build = tmp_path / "old"
    build.mkdir()
    (build / "omni-exec.exe").write_bytes(b"MZ")

    assert updates._updater_in(build).name == "omni-exec.exe"


def test_an_empty_build_has_neither(tmp_path, monkeypatch):
    _win(monkeypatch)
    build = tmp_path / "empty"
    build.mkdir()
    assert updates._updater_in(build) is None
    assert updates._executable_in(build) is None


def test_the_mac_backend_lives_inside_the_bundle(tmp_path, monkeypatch):
    """Tauri builds the .app, so the backend is a resource inside it rather
    than the bundle executable."""
    monkeypatch.setattr(updates.sys, "platform", "darwin")
    app = tmp_path / "Omni Executor.app"
    macos = app / "Contents" / "MacOS"
    backend = app / "Contents" / "Resources" / "backend"
    macos.mkdir(parents=True)
    backend.mkdir(parents=True)
    (macos / "omni-exec").write_bytes(b"\x7fELF")
    (backend / "omni-exec-py").write_bytes(b"\x7fELF")
    (macos / "omni-exec").chmod(0o755)

    assert updates._updater_in(app).name == "omni-exec-py"
    assert updates._executable_in(app).name == "omni-exec"


def test_update_app_restart_does_not_touch_the_window(monkeypatch):
    """It must not try to close the window itself.

    THE REGRESSION THIS PINS. `update_app_restart` used to end with

        threading.Timer(0.6, self.close).start()

    a pywebview leftover: back then the Api object owned a window and had a
    `close()`. Under Tauri the window belongs to the Rust shell and this
    process is a child speaking JSON over stdio, so the timer raised

        AttributeError: 'Api' object has no attribute 'close'

    on a BACKGROUND thread. Nothing failed loudly -- the RPC still answered
    ok: true, the window stayed open, and the helper (which waits for this pid
    to disappear before touching the install) sat out its 90 s timeout and gave
    up. Every auto-update silently did nothing, and the only visible trace was
    an error line inside the update modal.
    """
    import main
    api = main.Api.__new__(main.Api)
    monkeypatch.setattr(main.updates if hasattr(main, "updates") else __import__("updates"),
                        "launch_apply", lambda: 4321)
    res = main.Api.update_app_restart(api)
    assert res["ok"] is True
    assert res["helper_pid"] == 4321
    # The frontend is what closes the window; the backend says so explicitly
    # so a caller cannot mistake "started" for "the window is going away".
    assert res["close_window"] is True


def test_the_api_has_no_close_for_anyone_to_reach_for():
    """The other half: `self.close` was reachable-looking, which is why it was
    written. Nothing on the object is named that, and this fails the moment
    somebody adds one back and wires the window to it."""
    import main
    assert not hasattr(main.Api, "close")
