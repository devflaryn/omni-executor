"""Fresh-machine tool installation: QEMU and adb.

The bug these cover is not subtle once you see it, and it made the product
unusable on every machine that was not a dev box:

  * the first-boot screen refused to begin until QEMU was installed
    (BootstrapView gated bootstrap_start on `qemu_ok`),
  * nothing in the app ever installed QEMU -- the engine's own ensure_qemu()
    only fires when an INSTANCE is started, which is unreachable from the
    setup screen, and
  * even if it had fired, the vendor installer is manifested
    requireAdministrator, so the unelevated CreateProcess subprocess uses
    fails with WinError 740 before the installer ever runs.

On top of that the app and the engine disagreed about where QEMU even is:
engine_ready() looked only at PATH, while omnidroid's qemu_bin() on Windows
deliberately never looks at PATH. Each could report ready when the other
could not run.

adb was simply never considered, though omnidroid/adb.py shells the bare name
"adb" and fails identically without it.
"""
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

import bootstrap


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    return monkeypatch


@pytest.fixture
def fresh(win, tmp_path, monkeypatch):
    """A runtime dir on a machine with no QEMU, no adb, nothing on PATH."""
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("OMNI_QEMU_DIR", raising=False)
    monkeypatch.delenv("OMNI_ADB_DIR", raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "no-pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "no-pf86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local"))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda *a, **k: None)
    return bootstrap.runtime_dir()


def _make_qemu(d: Path):
    """A directory that looks like a complete QEMU install."""
    d.mkdir(parents=True, exist_ok=True)
    for n in ("qemu-system-x86_64.exe", "qemu-img.exe", "qemu-io.exe"):
        (d / n).write_bytes(b"MZ")
    return d


# ------------------------------------------------------------- detection

def test_fresh_machine_reports_no_tools(fresh):
    assert bootstrap.find_qemu(fresh) is None
    assert bootstrap.find_adb(fresh) is None
    eng = bootstrap.engine_ready(fresh)
    assert eng["qemu_ok"] is False
    assert eng["adb_ok"] is False
    assert eng["tools_ok"] is False


def test_qemu_in_runtime_dir_is_found(fresh):
    """The engine looks in a product dir, never PATH. Anything installed by
    ensure_tools() must therefore be visible without touching PATH at all."""
    _make_qemu(bootstrap.qemu_dir(fresh))
    assert bootstrap.find_qemu(fresh) == bootstrap.qemu_dir(fresh)
    assert bootstrap.engine_ready(fresh)["qemu_ok"] is True


def test_incomplete_qemu_dir_is_not_accepted(fresh):
    """A directory with the emulator but no qemu-img is not a QEMU install.
    Accepting it defers the failure to the first account creation, which is
    where the overlay disk is made -- far from the cause."""
    d = bootstrap.qemu_dir(fresh)
    d.mkdir(parents=True)
    (d / "qemu-system-x86_64.exe").write_bytes(b"MZ")
    assert bootstrap.find_qemu(fresh) is None


def test_existing_system_qemu_is_reused(fresh, tmp_path, monkeypatch):
    """A machine that already has QEMU must not be made to download 200 MB of
    it again."""
    pf = tmp_path / "pf"
    _make_qemu(pf / "qemu")
    monkeypatch.setenv("ProgramFiles", str(pf))
    assert bootstrap.find_qemu(fresh) == pf / "qemu"
    assert bootstrap.qemu_install_plan(fresh, {})["needed"] is False


def test_env_override_wins(fresh, tmp_path, monkeypatch):
    d = _make_qemu(tmp_path / "custom-qemu")
    monkeypatch.setenv("OMNI_QEMU_DIR", str(d))
    assert bootstrap.find_qemu(fresh) == d


# ------------------------------------------------------------- the plan

def test_plan_needs_admin_without_a_portable_build(fresh, monkeypatch):
    monkeypatch.setattr(bootstrap, "is_elevated", lambda: False)
    plan = bootstrap.qemu_install_plan(fresh, {"artifacts": []})
    assert plan == {"needed": True, "portable": None, "needs_admin": True}


def test_a_hosted_portable_build_removes_the_admin_prompt(fresh):
    """The vendor installer needs elevation; a zip we host does not. When one
    is published the whole UAC step disappears."""
    man = {"artifacts": [{"name": "qemu-portable-win", "sha256": "ab" * 32,
                          "url": "/omni/dist/blob/qemu-portable-win",
                          "bytes": 1}]}
    plan = bootstrap.qemu_install_plan(fresh, man)
    assert plan["needed"] is True
    assert plan["needs_admin"] is False
    assert plan["portable"]["name"] == "qemu-portable-win"


def test_the_portable_build_is_not_a_runtime_download(fresh):
    """It has a sha256 and a dest, so it looks exactly like a base image to
    plan_downloads -- and must not be treated as one.

    Two failures if it were: a fresh machine downloads 76 MB twice (once by
    ensure_tools, once by ensure_runtime), and — much worse — every machine
    that legitimately SKIPPED it because QEMU was already installed would
    report un-ready forever, because readiness is "the plan is empty". An app
    artifact caused precisely that the first time one was published."""
    man = {"artifacts": [
        {"name": "qemu-portable-win", "kind": "tool", "sha256": "ab" * 32,
         "url": "/omni/dist/blob/qemu-portable-win", "bytes": 1, "dest": "qemu"},
        {"name": "base-x86", "sha256": "cd" * 32,
         "url": "/omni/dist/blob/base-x86", "bytes": 1, "dest": "images/x86"},
    ]}
    planned = [a["name"] for a in bootstrap.plan_downloads(man, {"artifacts": {}})]
    assert planned == ["base-x86"]
    # ...but ensure_tools still finds it, because it asks by NAME.
    assert bootstrap.qemu_install_plan(fresh, man)["portable"] is not None


def test_portable_entry_without_a_hash_is_ignored(fresh):
    """A pointer/redirect entry carries no sha256 and is not a downloadable
    blob -- treating one as the portable build would try to verify bytes
    against None (the exact failure plan_downloads was fixed for)."""
    man = {"artifacts": [{"name": "qemu-portable-win", "sha256": None}]}
    assert bootstrap.qemu_install_plan(fresh, man)["portable"] is None


# ------------------------------------------------------------- NSIS command

def test_nsis_target_dir_is_unquoted_even_with_spaces():
    """NSIS's /D= is not a normal argument: it must be LAST and MUST NOT be
    quoted, even for a path with spaces. Quoting it -- the reflex for every
    other program -- makes the installer silently use its DEFAULT location,
    which then looks like the download went to the wrong place."""
    cmd = bootstrap._nsis_command(Path(r"C:\tmp\qemu-setup.exe"),
                                  Path(r"C:\Users\John Doe\AppData\Local\OmniExec\qemu"))
    assert cmd.endswith(r"/D=C:\Users\John Doe\AppData\Local\OmniExec\qemu")
    assert '"C:\\Users\\John Doe' not in cmd
    assert cmd.startswith('"C:\\tmp\\qemu-setup.exe" /S ')


# ------------------------------------------------------------- adb install

def _platform_tools_zip(nested=True):
    buf = io.BytesIO()
    prefix = "platform-tools/" if nested else ""
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{prefix}adb.exe", "MZ-adb")
        zf.writestr(f"{prefix}AdbWinApi.dll", "MZ-dll")
    return buf.getvalue()


def test_adb_zip_is_flattened(fresh, monkeypatch):
    """Google's zip has a top-level platform-tools/ folder, so a naive extract
    leaves adb at <dest>/platform-tools/adb.exe -- one level below where both
    find_adb and the PATH entry look, which resolves to nothing."""
    monkeypatch.setattr(bootstrap, "_download_to",
                        lambda url, dest, progress=None, label="": dest.write_bytes(
                            _platform_tools_zip()))
    d = bootstrap.install_adb_windows(fresh)
    assert (d / "adb.exe").exists()
    assert not (d / "platform-tools").exists()
    assert bootstrap.find_adb(fresh) == d


def test_adb_zip_without_a_wrapper_dir_also_works(fresh, monkeypatch):
    monkeypatch.setattr(bootstrap, "_download_to",
                        lambda url, dest, progress=None, label="": dest.write_bytes(
                            _platform_tools_zip(nested=False)))
    assert (bootstrap.install_adb_windows(fresh) / "adb.exe").exists()


def test_adb_falls_back_when_the_registry_has_no_entry(fresh, monkeypatch):
    """A client newer than the server's registry must still install adb
    rather than fail on a 404 the operator simply has not fixed yet."""
    tried = []

    def fake_download(url, dest, progress=None, label=""):
        tried.append(url)
        if len(tried) == 1:
            raise bootstrap.BootstrapError("404")
        dest.write_bytes(_platform_tools_zip())

    monkeypatch.setattr(bootstrap, "_download_to", fake_download)
    bootstrap.install_adb_windows(fresh)
    assert tried[0].endswith("/omni/dist/blob/adb-win")
    assert tried[1] == bootstrap._ADB_WIN_FALLBACK


# ------------------------------------------------------------- ensure_tools

def test_ensure_tools_installs_both_and_asks_for_admin_once(fresh, monkeypatch):
    """The whole point of batching: a fresh machine needs QEMU installed and
    may need the hypervisor feature turned on, and both need administrator.
    Asking twice for one setup is worse than the thing being manual."""
    prompts = []

    def fake_elevated(command, timeout=1800):
        prompts.append(command)
        _make_qemu(bootstrap.qemu_dir(fresh))
        return 0

    monkeypatch.setattr(bootstrap, "is_elevated", lambda: False)
    monkeypatch.setattr(bootstrap, "run_elevated", fake_elevated)
    monkeypatch.setattr(bootstrap, "_download_to",
                        lambda url, dest, progress=None, label="":
                        dest.write_bytes(_platform_tools_zip()
                                         if "adb" in label else b"MZ"))
    monkeypatch.setattr(bootstrap, "windows_accel_status",
                        lambda *a, **k: {"whpx_ok": True, "hint": None})

    out = bootstrap.ensure_tools(fresh, {"artifacts": []})
    assert len(prompts) == 1, "must be a single UAC prompt"
    assert "/S /D=" in prompts[0] and "Enable-Feature" in prompts[0]
    assert set(out["installed"]) == {"qemu", "adb"}
    assert bootstrap.engine_ready(fresh)["tools_ok"] is True


def test_ensure_tools_is_a_noop_when_everything_is_present(fresh, monkeypatch):
    _make_qemu(bootstrap.qemu_dir(fresh))
    (bootstrap.adb_dir(fresh)).mkdir(parents=True)
    (bootstrap.adb_dir(fresh) / "adb.exe").write_bytes(b"MZ")
    monkeypatch.setattr(bootstrap, "windows_accel_status",
                        lambda *a, **k: {"whpx_ok": True, "hint": None})
    monkeypatch.setattr(bootstrap, "run_elevated", lambda *a, **k:
                        pytest.fail("must not ask for administrator"))
    out = bootstrap.ensure_tools(fresh, {"artifacts": []})
    assert out["installed"] == []
    assert out["whpx_ok"] is True


def test_ensure_tools_skips_elevation_when_a_portable_build_exists(fresh, monkeypatch):
    man = {"artifacts": [{"name": "qemu-portable-win", "sha256": "ab" * 32,
                          "url": "/omni/dist/blob/qemu-portable-win", "bytes": 2}]}

    def fake_portable(rt, artifact, progress=None):
        return _make_qemu(bootstrap.qemu_dir(rt))

    monkeypatch.setattr(bootstrap, "_install_qemu_portable", fake_portable)
    monkeypatch.setattr(bootstrap, "tools_manifest", lambda: man)
    monkeypatch.setattr(bootstrap, "run_elevated", lambda *a, **k:
                        pytest.fail("a portable build must need no elevation"))
    monkeypatch.setattr(bootstrap, "_download_to",
                        lambda url, dest, progress=None, label="":
                        dest.write_bytes(_platform_tools_zip()))
    monkeypatch.setattr(bootstrap, "windows_accel_status",
                        lambda *a, **k: {"whpx_ok": True, "hint": None})
    out = bootstrap.ensure_tools(fresh, man)
    assert "qemu" in out["installed"]


def test_a_portable_build_found_only_in_the_tools_channel_is_actually_used(
        fresh, monkeypatch):
    """Regression: ensure_tools() looked up the tools channel, saw the portable
    build, and then handed install_qemu_windows() the ORIGINAL manifest --
    None. It recomputed a plan that could not see the portable build, and so
    downloaded the 197 MB vendor installer and raised a UAC prompt on a machine
    that needed neither. Shipped in 1.0.10; caught by an end-to-end run against
    the live server, not by a unit test, which is why this one exists."""
    man = {"artifacts": [{"name": "qemu-portable-win", "sha256": "ab" * 32,
                          "url": "/omni/dist/blob/qemu-portable-win", "bytes": 2}]}
    monkeypatch.setattr(bootstrap, "tools_manifest", lambda: man)
    monkeypatch.setattr(bootstrap, "_install_qemu_portable",
                        lambda rt, artifact, progress=None: _make_qemu(
                            bootstrap.qemu_dir(rt)))
    monkeypatch.setattr(bootstrap, "run_elevated", lambda *a, **k: pytest.fail(
        "a published portable build must remove the administrator prompt"))
    monkeypatch.setattr(bootstrap, "_download_to",
                        lambda url, dest, progress=None, label="":
                        dest.write_bytes(_platform_tools_zip()))
    monkeypatch.setattr(bootstrap, "windows_accel_status",
                        lambda *a, **k: {"whpx_ok": True, "hint": None})
    # No manifest argument at all -- exactly how bootstrap_start calls it.
    out = bootstrap.ensure_tools(fresh)
    assert set(out["installed"]) == {"qemu", "adb"}


def test_install_qemu_windows_asks_the_tools_channel_itself(fresh, monkeypatch):
    """It is a public entry point, so it must not assume a caller already
    looked for a portable build."""
    man = {"artifacts": [{"name": "qemu-portable-win", "sha256": "ab" * 32,
                          "url": "/omni/dist/blob/qemu-portable-win", "bytes": 2}]}
    monkeypatch.setattr(bootstrap, "tools_manifest", lambda: man)
    monkeypatch.setattr(bootstrap, "_install_qemu_portable",
                        lambda rt, artifact, progress=None: _make_qemu(
                            bootstrap.qemu_dir(rt)))
    monkeypatch.setattr(bootstrap, "run_elevated", lambda *a, **k: pytest.fail(
        "must not elevate when a portable build is published"))
    assert bootstrap.install_qemu_windows(fresh) == bootstrap.qemu_dir(fresh)


def test_the_tools_channel_is_only_consulted_when_qemu_is_missing(fresh, monkeypatch):
    """A machine that already has QEMU must not make a network call to learn
    it does not need one."""
    asked = []
    monkeypatch.setattr(bootstrap, "tools_manifest",
                        lambda: asked.append(1) or {"artifacts": []})
    _make_qemu(bootstrap.qemu_dir(fresh))
    (bootstrap.adb_dir(fresh)).mkdir(parents=True)
    (bootstrap.adb_dir(fresh) / "adb.exe").write_bytes(b"MZ")
    monkeypatch.setattr(bootstrap, "windows_accel_status",
                        lambda *a, **k: {"whpx_ok": True, "hint": None})
    bootstrap.ensure_tools(fresh)
    assert asked == []


def test_an_unreachable_tools_channel_falls_back_to_the_installer(fresh, monkeypatch):
    """A server that is down must cost a UAC prompt, not the whole install."""
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *a, **k: (_ for _ in ()).throw(
        bootstrap.BootstrapError("unreachable")))
    assert bootstrap.tools_manifest() == {}
    monkeypatch.setattr(bootstrap, "is_elevated", lambda: False)
    plan = bootstrap.qemu_install_plan(fresh, bootstrap.tools_manifest())
    assert plan["needed"] is True and plan["needs_admin"] is True


def test_whpx_is_enabled_when_qemu_exists_and_reports_it_off(fresh, monkeypatch):
    """The one case the batched prompt cannot cover: QEMU was already there,
    so no elevation was needed for it, and the feature turns out to be off
    only once QEMU exists to be asked."""
    _make_qemu(bootstrap.qemu_dir(fresh))
    (bootstrap.adb_dir(fresh)).mkdir(parents=True)
    (bootstrap.adb_dir(fresh) / "adb.exe").write_bytes(b"MZ")
    monkeypatch.setattr(bootstrap, "windows_accel_status",
                        lambda *a, **k: {"whpx_ok": False, "hint": "off"})
    monkeypatch.setattr(bootstrap, "is_elevated", lambda: False)
    monkeypatch.setattr(bootstrap, "run_elevated",
                        lambda command, timeout=1800: bootstrap._DISM_REBOOT_REQUIRED)
    out = bootstrap.ensure_tools(fresh, {"artifacts": []})
    assert out["reboot_required"] is True
    assert "whpx" in out["installed"]


# ------------------------------------------------------------- WHPX helper

def test_dism_reboot_code_is_success_not_failure(win, monkeypatch):
    """3010 is DISM saying "done, now restart". Reporting it as a failure
    would tell a user their successful setup had failed."""
    monkeypatch.setattr(bootstrap, "is_elevated", lambda: False)
    monkeypatch.setattr(bootstrap, "run_elevated",
                        lambda command, timeout=1800: 3010)
    res = bootstrap.enable_whpx()
    assert res == {"ok": True, "reboot_required": True, "exit_code": 3010}


def test_dism_zero_means_already_on(win, monkeypatch):
    monkeypatch.setattr(bootstrap, "is_elevated", lambda: False)
    monkeypatch.setattr(bootstrap, "run_elevated", lambda command, timeout=1800: 0)
    res = bootstrap.enable_whpx()
    assert res["ok"] is True and res["reboot_required"] is False


def test_enabling_whpx_clears_the_cached_probe(win, monkeypatch):
    """The cached "off" answer is what gates the Start button; leaving it
    would keep offering to enable an already-enabled feature."""
    bootstrap._whpx_cache["win"] = {"os": "win", "whpx_ok": False, "hint": "x"}
    monkeypatch.setattr(bootstrap, "is_elevated", lambda: False)
    monkeypatch.setattr(bootstrap, "run_elevated", lambda command, timeout=1800: 3010)
    bootstrap.enable_whpx()
    assert "win" not in bootstrap._whpx_cache


# ------------------------------------------------------------- engine wiring

def test_configure_engine_points_the_engine_at_the_installed_qemu(fresh, monkeypatch):
    """omnidroid's qemu_bin() consults config `qemu.dir` FIRST and never PATH
    on Windows, so this is the only thing that makes an install in the runtime
    dir reachable by the engine."""
    d = _make_qemu(bootstrap.qemu_dir(fresh))
    bootstrap.configure_engine(fresh)
    cfg = json.loads((fresh / "paths.json").read_text())
    assert cfg["qemu"]["dir"] == str(d)


def test_configure_engine_puts_adb_on_path(fresh, monkeypatch):
    """omnidroid/adb.py runs the BARE NAME "adb" -- there is no config knob
    for it, so PATH is the only route."""
    a = bootstrap.adb_dir(fresh)
    a.mkdir(parents=True)
    (a / "adb.exe").write_bytes(b"MZ")
    monkeypatch.setenv("PATH", "C:\\Windows")
    bootstrap.configure_engine(fresh)
    assert str(a) in bootstrap.os.environ["PATH"].split(bootstrap.os.pathsep)


def test_tool_env_is_idempotent(fresh, monkeypatch):
    """configure_engine runs on EVERY launch; PATH must not grow each time."""
    a = bootstrap.adb_dir(fresh)
    a.mkdir(parents=True)
    (a / "adb.exe").write_bytes(b"MZ")
    monkeypatch.setenv("PATH", "C:\\Windows")
    for _ in range(3):
        bootstrap._apply_tool_env(fresh)
    assert bootstrap.os.environ["PATH"].split(bootstrap.os.pathsep).count(str(a)) == 1


def test_tools_live_outside_the_app_dir(fresh):
    """Not next to the exe: the updater REPLACES that whole tree, so a QEMU
    installed there is destroyed by every app update -- and Program Files is
    not writable without elevation in the first place."""
    assert bootstrap.qemu_dir(fresh).parent == fresh
    assert bootstrap.adb_dir(fresh).parent == fresh
