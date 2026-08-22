"""WHPX (Windows Hypervisor Platform) detection.

omnidroid's default_accel() returns "whpx,kernel-irqchip=off" on Windows and
QEMU simply dies at launch if the feature is off, so the app has to say so
BEFORE a boot rather than surface an opaque failure.

Detection asks QEMU, not Windows. The obvious route -- DISM /Get-FeatureInfo
or Get-WindowsOptionalFeature parsed for State=Enabled -- REQUIRES ELEVATION:
from a normal user session it raises COMException (verified on the Windows
test box), which would report "WHPX disabled" on a machine where WHPX
demonstrably works and nag the user into running an admin command they do not
need. QEMU is both the authority and the consumer here: if it can initialize
the accelerator, the feature is on.
"""
import sys
from pathlib import Path

import pytest

import bootstrap


@pytest.fixture(autouse=True)
def _no_cached_verdict():
    # The probe result is cached process-wide on purpose (WHPX cannot change
    # without a reboot), so each test has to start from a clean slate.
    bootstrap._whpx_cache.clear()
    yield
    bootstrap._whpx_cache.clear()


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    return monkeypatch


def test_non_windows_is_a_no_op(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    s = bootstrap.windows_accel_status()
    assert s == {"os": "mac", "whpx_ok": True, "hint": None}


def test_whpx_available_reports_ok(win, monkeypatch):
    monkeypatch.setattr(bootstrap, "find_qemu", lambda rt: Path("C:/q"))
    s = bootstrap.windows_accel_status(probe=lambda qemu: True)
    assert s["os"] == "win"
    assert s["whpx_ok"] is True
    assert s["hint"] is None


def test_whpx_missing_names_the_feature_and_the_fix(win, monkeypatch):
    monkeypatch.setattr(bootstrap, "find_qemu", lambda rt: Path("C:/q"))
    s = bootstrap.windows_accel_status(probe=lambda qemu: False)
    assert s["whpx_ok"] is False
    # The hint no longer tells the user to go run DISM themselves -- the app
    # does it for them (enable_whpx), so it explains the two things they will
    # actually experience: one admin prompt, then a restart.
    assert "administrator" in s["hint"].lower()
    assert "restart" in s["hint"].lower()


def test_no_qemu_yet_is_unknown_not_disabled(win, monkeypatch):
    # First boot: QEMU has not been installed yet, so WHPX is unprobeable.
    # Reporting False here would show a scary "virtualization is off" panel to
    # a user whose machine is fine. Unknown must be distinguishable.
    monkeypatch.setattr(bootstrap, "find_qemu", lambda rt: None)
    s = bootstrap.windows_accel_status()
    assert s["whpx_ok"] is None
    assert s["hint"]


def test_a_probe_that_explodes_is_unknown_not_disabled(win, monkeypatch):
    def boom(qemu):
        raise OSError("access denied")

    monkeypatch.setattr(bootstrap, "find_qemu", lambda rt: Path("C:/q"))
    s = bootstrap.windows_accel_status(probe=boom)
    assert s["whpx_ok"] is None


# ------------------------------------------------------------- the probe itself

class _Proc:
    """Stand-in for the QEMU process the probe spawns."""

    def __init__(self, alive, returncode=0, stderr=""):
        self._alive = alive
        self.returncode = returncode
        self._stderr = stderr
        self.killed = False

    def communicate(self, timeout=None):
        if self._alive:
            raise bootstrap.subprocess.TimeoutExpired("qemu", timeout)
        return ("", self._stderr)

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


def test_a_qemu_that_keeps_running_means_whpx_initialized(win, monkeypatch):
    # QEMU started with -S sits paused once the accelerator is up, so
    # "still alive" is the success signal.
    proc = _Proc(alive=True)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **k: proc)
    assert bootstrap._whpx_probe("C:/q/qemu.exe") is True
    assert proc.killed is True          # never leave a probe VM behind


def test_a_qemu_that_dies_complaining_about_whpx_means_disabled(win,
                                                                monkeypatch):
    proc = _Proc(alive=False, returncode=1,
                 stderr="qemu: failed to initialize whpx: Insufficient "
                        "resources exist to complete the requested service")
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **k: proc)
    assert bootstrap._whpx_probe("C:/q/qemu.exe") is False


def test_a_qemu_that_dies_for_an_unrelated_reason_is_unknown(win, monkeypatch):
    proc = _Proc(alive=False, returncode=1,
                 stderr="qemu: could not load PC BIOS 'bios-256k.bin'")
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **k: proc)
    assert bootstrap._whpx_probe("C:/q/qemu.exe") is None


# --------------------------------------------- the fast path, added 2026-08-21
#
# Success used to be measured by WAITING OUT THE WHOLE BUDGET: the only signal
# was "the process is still alive when the timeout expires", so a machine where
# WHPX works perfectly paid a flat six seconds, every process, with the setup
# screen blocked behind it. QEMU will say so in a tenth of a second if asked --
# its QMP greeting is written after machine init, which is where accelerator
# init happens.


class _GreetingProc(_Proc):
    """A QEMU that comes up and writes its QMP greeting."""

    class _Stream:
        def __init__(self, line):
            self._line = line

        def readline(self):
            return self._line

    def __init__(self, line='{"QMP": {"version": {}}}\n'):
        super().__init__(alive=True)
        self.stdout = self._Stream(line)


def test_a_qmp_greeting_proves_whpx_without_waiting_out_the_budget(win,
                                                                   monkeypatch):
    proc = _GreetingProc()
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **k: proc)
    # A timeout of zero would fail the old liveness path outright; the greeting
    # has to be what answers.
    assert bootstrap._whpx_probe("C:/q/qemu.exe", timeout=0.01) is True
    assert proc.killed is True


def test_a_probe_without_a_readable_stdout_still_falls_back(win, monkeypatch):
    """The old signal is kept, not replaced: a QEMU build or a stub whose
    stdout we cannot read must still get an answer."""
    proc = _Proc(alive=True)             # no .stdout at all
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **k: proc)
    assert bootstrap._whpx_probe("C:/q/qemu.exe") is True


def test_garbage_on_stdout_is_not_a_greeting(win, monkeypatch):
    proc = _GreetingProc(line="something else entirely\n")
    proc._alive = False
    proc._stderr = "qemu: failed to initialize whpx"
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **k: proc)
    assert bootstrap._whpx_probe("C:/q/qemu.exe") is False


def test_the_probe_asks_for_a_qmp_monitor(win, monkeypatch):
    seen = {}

    def fake_popen(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Proc(alive=True)

    monkeypatch.setattr(bootstrap.subprocess, "Popen", fake_popen)
    bootstrap._whpx_probe("C:/q/qemu.exe")
    assert "-qmp" in seen["cmd"] and "stdio" in seen["cmd"]
    # ...and still nothing that could touch a disk or a screen.
    assert "-S" in seen["cmd"] and "none" in seen["cmd"]


def test_the_hint_covers_the_half_windows_cannot_fix(win, monkeypatch):
    """DISM turns the FEATURE on. It cannot turn on VT-x in the firmware, and a
    user whose BIOS switch is off would otherwise loop forever: enable, reboot,
    still off, enable again, with the message insisting it is about Windows."""
    monkeypatch.setattr(bootstrap, "find_qemu", lambda rt: Path("C:/q"))
    hint = bootstrap.windows_accel_status(probe=lambda qemu: False)["hint"]
    assert "BIOS" in hint or "UEFI" in hint
    assert "VT-x" in hint


def test_the_hint_says_it_still_runs(win, monkeypatch):
    """It does now -- the engine falls back to software emulation rather than
    refusing to boot. Telling the user their PC cannot run this would be
    false, and would send them away from a product that works."""
    monkeypatch.setattr(bootstrap, "find_qemu", lambda rt: Path("C:/q"))
    hint = bootstrap.windows_accel_status(probe=lambda qemu: False)["hint"]
    assert "slower" in hint.lower()
    assert "cannot start" not in hint.lower()
