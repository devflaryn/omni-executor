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
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "C:/q/qemu.exe")
    s = bootstrap.windows_accel_status(probe=lambda qemu: True)
    assert s["os"] == "win"
    assert s["whpx_ok"] is True
    assert s["hint"] is None


def test_whpx_missing_names_the_feature_and_the_fix(win, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "C:/q/qemu.exe")
    s = bootstrap.windows_accel_status(probe=lambda qemu: False)
    assert s["whpx_ok"] is False
    assert "HypervisorPlatform" in s["hint"]
    assert "reboot" in s["hint"].lower()


def test_no_qemu_yet_is_unknown_not_disabled(win, monkeypatch):
    # First boot: QEMU has not been installed yet, so WHPX is unprobeable.
    # Reporting False here would show a scary "virtualization is off" panel to
    # a user whose machine is fine. Unknown must be distinguishable.
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: None)
    s = bootstrap.windows_accel_status()
    assert s["whpx_ok"] is None
    assert s["hint"]


def test_a_probe_that_explodes_is_unknown_not_disabled(win, monkeypatch):
    def boom(qemu):
        raise OSError("access denied")

    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "C:/q/qemu.exe")
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
