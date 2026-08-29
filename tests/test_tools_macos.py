"""Finding QEMU and adb on macOS, where PATH is not the whole story.

THE BUG THIS COVERS, measured on the Mac mini 2026-08-29. A Finder-launched
`.app` is started by launchd, which does NOT give it the login shell's PATH —
it gets `/usr/bin:/bin:/usr/sbin:/sbin`. Homebrew installs into
`/opt/homebrew/bin` (Apple silicon) or `/usr/local/bin` (Intel), and neither is
on that PATH. `find_qemu`/`find_adb` fell through to `shutil.which` and then
straight into a WINDOWS-ONLY block of well-known locations, so on macOS they
returned None even though `brew install qemu android-platform-tools` had been
run — and the same probe reported `tools_ok: True` when run from a terminal.
Same machine, same code, same data; only PATH differed.

Why that DEADLOCKED the app rather than merely warning:

    ready = (plan is empty) and tools_ok        # main.py bootstrap_status

Once the runtime images are installed the plan is empty, so there is nothing
left to download and no progress events are emitted. `tools_ok` was
permanently False, so `ready` was permanently False, so the app sat in
BootstrapView showing its DEFAULT label — "Preparing…" — with no progress bar
and no error, forever. Nothing the user could do inside the app cleared it.

Windows already had exactly this class of fallback (ProgramFiles\\qemu,
LOCALAPPDATA\\Android\\Sdk\\platform-tools). This is the macOS half.

NOTE ON THE FIXTURES. These deliberately do NOT patch `sys.platform`:
`bootstrap.sys` is the real `sys` module, so patching it is process-wide and
pytest's own fixtures then take their POSIX branch and call `os.getuid()`,
which does not exist on Windows. Patching `current_os()` — the function the
rest of bootstrap already routes platform decisions through — is both narrower
and closer to how the code actually asks the question.
"""
from pathlib import Path

import pytest

import bootstrap


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A macOS host, from bootstrap's point of view, with no tools on PATH."""
    monkeypatch.setattr(bootstrap, "current_os", lambda: "mac")
    # macOS binaries have no .exe suffix; on a Windows test host _exe() would
    # otherwise look for "qemu-system-aarch64.exe".
    monkeypatch.setattr(bootstrap, "_exe", lambda name: name)
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("OMNI_QEMU_DIR", raising=False)
    monkeypatch.delenv("OMNI_ADB_DIR", raising=False)
    # These tests run on a Windows box, where find_qemu/find_adb's own Windows
    # fallbacks are still live (they key off sys.platform, which is
    # deliberately not patched here — see the module docstring). Point them at
    # empty dirs so a real Android SDK on the test machine cannot answer for a
    # simulated Mac.
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "no-pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "no-pf86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local"))
    # launchd's PATH: shutil.which finds nothing. That is the whole point.
    monkeypatch.setattr(bootstrap.shutil, "which", lambda *a, **k: None)
    return bootstrap.runtime_dir()


def _make_qemu(d: Path) -> Path:
    """A directory that looks like a COMPLETE macOS QEMU install."""
    d.mkdir(parents=True, exist_ok=True)
    names = [bootstrap._qemu_system_name()] + [
        t for t in bootstrap._QEMU_TOOLS if not t.startswith("qemu-system")
    ]
    for n in names:
        (d / n).write_bytes(b"\x7fELF")
    return d


def _make_adb(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "adb").write_bytes(b"\x7fELF")
    return d


def test_a_mac_with_no_tools_installed_still_reports_none(mac, tmp_path, monkeypatch):
    """The fallback must not INVENT tools. A Mac that genuinely has no QEMU
    has to keep saying so, or the deadlock is merely traded for a launch that
    fails later with a worse message."""
    monkeypatch.setattr(bootstrap, "_MAC_TOOL_DIRS",
                        (tmp_path / "nope-a", tmp_path / "nope-b"), raising=False)
    assert bootstrap.find_qemu(mac) is None
    assert bootstrap.find_adb(mac) is None
    assert bootstrap.engine_ready(mac)["tools_ok"] is False


def test_homebrew_qemu_is_found_with_nothing_on_path(mac, tmp_path, monkeypatch):
    """Apple silicon Homebrew. The app is launched by Finder, so PATH is
    useless and the location has to be known."""
    brew = _make_qemu(tmp_path / "opt" / "homebrew" / "bin")
    monkeypatch.setattr(bootstrap, "_MAC_TOOL_DIRS",
                        (tmp_path / "missing", brew), raising=False)
    assert bootstrap.find_qemu(mac) == brew


def test_homebrew_adb_is_found_with_nothing_on_path(mac, tmp_path, monkeypatch):
    brew = _make_adb(tmp_path / "opt" / "homebrew" / "bin")
    monkeypatch.setattr(bootstrap, "_MAC_TOOL_DIRS",
                        (tmp_path / "missing", brew), raising=False)
    assert bootstrap.find_adb(mac) == brew


def test_both_tools_found_makes_the_app_ready(mac, tmp_path, monkeypatch):
    """The end the user actually cares about: tools_ok True is what lets
    bootstrap_status report ready and leave the Preparing screen."""
    brew = tmp_path / "opt" / "homebrew" / "bin"
    _make_qemu(brew)
    _make_adb(brew)
    monkeypatch.setattr(bootstrap, "_MAC_TOOL_DIRS", (brew,), raising=False)
    eng = bootstrap.engine_ready(mac)
    assert eng["qemu_ok"] is True and eng["adb_ok"] is True
    assert eng["tools_ok"] is True
    assert eng["qemu_hint"] is None


def test_search_order_prefers_the_first_prefix(mac, tmp_path, monkeypatch):
    """Apple silicon before Intel: on an arm Mac with both prefixes populated,
    /usr/local/bin is usually a leftover x86_64 install under Rosetta."""
    first = _make_qemu(tmp_path / "opt" / "homebrew" / "bin")
    second = _make_qemu(tmp_path / "usr" / "local" / "bin")
    monkeypatch.setattr(bootstrap, "_MAC_TOOL_DIRS", (first, second), raising=False)
    assert bootstrap.find_qemu(mac) == first


def test_the_runtime_dir_still_wins_over_a_system_install(mac, tmp_path, monkeypatch):
    """Ordering is load-bearing and unchanged: an install we control beats one
    we merely found, so ensure_tools' output is never shadowed by Homebrew."""
    ours = _make_qemu(bootstrap.qemu_dir(mac))
    brew = _make_qemu(tmp_path / "opt" / "homebrew" / "bin")
    monkeypatch.setattr(bootstrap, "_MAC_TOOL_DIRS", (brew,), raising=False)
    assert bootstrap.find_qemu(mac) == ours


def test_env_override_still_wins(mac, tmp_path, monkeypatch):
    d = _make_qemu(tmp_path / "custom")
    brew = _make_qemu(tmp_path / "opt" / "homebrew" / "bin")
    monkeypatch.setattr(bootstrap, "_MAC_TOOL_DIRS", (brew,), raising=False)
    monkeypatch.setenv("OMNI_QEMU_DIR", str(d))
    assert bootstrap.find_qemu(mac) == d


def test_the_fallback_is_mac_only(tmp_path, monkeypatch):
    """A Linux host must not start claiming /opt/homebrew — its tools come
    from apt and its hint says so."""
    monkeypatch.setattr(bootstrap, "current_os", lambda: "linux")
    monkeypatch.setattr(bootstrap, "_exe", lambda name: name)
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("OMNI_QEMU_DIR", raising=False)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda *a, **k: None)
    brew = _make_qemu(tmp_path / "opt" / "homebrew" / "bin")
    monkeypatch.setattr(bootstrap, "_MAC_TOOL_DIRS", (brew,), raising=False)
    assert bootstrap.find_qemu(bootstrap.runtime_dir()) is None
