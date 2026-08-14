"""Mark-of-the-Web: the reason a downloaded build would not start at all.

Windows puts a `Zone.Identifier` alternate data stream on every file extracted
from a downloaded .zip. The .NET Framework assembly loader then refuses to
load Python.Runtime.dll out of the Internet zone, so clr_loader cannot resolve
its entry point and pywebview's WinForms backend dies on import:

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
    ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

Reported from a fresh PC, and reproduced here by putting that one stream on
that one DLL in a working venv — the error was identical, and removing the
stream cured it. It is structurally invisible in development: a build made
locally was never downloaded, so it is never marked.
"""
import os
import sys
from pathlib import Path

import pytest

import main

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="alternate data streams are NTFS-only")


def _mark(path: Path):
    """Do to a file exactly what Explorer does when it extracts a download."""
    with open(f"{path}:Zone.Identifier", "w") as f:
        f.write("[ZoneTransfer]\nZoneId=3\n")


def _fake_install(root: Path) -> Path:
    """A one-dir PyInstaller layout, in miniature."""
    runtime = root / "_internal" / "pythonnet" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "Python.Runtime.dll").write_bytes(b"MZ")
    # The facades Python.Runtime.dll pulls in. Each is refused on the same
    # grounds, which is why clearing only the one file is not enough.
    for n in ("netstandard.dll", "System.Runtime.dll", "System.Linq.dll"):
        (runtime / n).write_bytes(b"MZ")
    (root / "omni-exec.exe").write_bytes(b"MZ")
    (root / "_internal" / "base_library.zip").write_bytes(b"PK")
    return runtime / "Python.Runtime.dll"


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    root = tmp_path / "omni-exec"
    root.mkdir()
    dll = _fake_install(root)
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main.sys, "executable", str(root / "omni-exec.exe"))
    monkeypatch.setattr(main.sys, "platform", "win32")
    return root, dll


def test_a_marked_install_is_unblocked(frozen):
    root, dll = frozen
    for p in root.rglob("*"):
        if p.is_file():
            _mark(p)
    assert main._has_motw(dll)

    cleared = main._unblock_app_files()

    assert cleared >= 5
    assert not main._has_motw(dll)
    # Every file, not just the DLL that trips first: Python.Runtime.dll loads
    # ~100 netstandard facades beside it and each would be refused in turn, so
    # clearing one only moves the error along.
    for p in root.rglob("*"):
        if p.is_file():
            assert not main._has_motw(p), f"{p.name} is still marked"


def test_an_unmarked_install_is_left_alone(frozen):
    """The normal case — an installer-placed build — must cost one stat, not
    a walk of 1,200 files on every launch."""
    root, dll = frozen
    assert main._unblock_app_files() == 0


def test_only_the_dll_marked_still_triggers_the_sweep(frozen):
    """The probe is Python.Runtime.dll, not the exe: a user who right-clicks
    the EXE and unblocks it has fixed nothing, because the assembly loader
    cares about the assembly."""
    root, dll = frozen
    _mark(dll)
    assert main._unblock_app_files() == 1
    assert not main._has_motw(dll)


def test_running_from_source_is_a_no_op(tmp_path, monkeypatch):
    """There is no install tree to sweep, and sys.executable is the
    interpreter — walking its directory would be both pointless and rude."""
    monkeypatch.setattr(main.sys, "frozen", False, raising=False)
    assert main._unblock_app_files() == 0


def test_an_unremovable_mark_does_not_crash_the_launch(frozen, monkeypatch):
    """Best-effort by design: a read-only install cannot be unmarked, and
    failing to start is far worse than starting and possibly failing later."""
    root, dll = frozen
    _mark(dll)

    def boom(path):
        raise PermissionError("read-only")

    monkeypatch.setattr(main.os, "remove", boom)
    assert main._unblock_app_files() == 0     # no exception
