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
