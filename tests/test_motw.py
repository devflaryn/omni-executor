"""Mark-of-the-Web: the reason a downloaded build would not start at all.

Windows puts a `Zone.Identifier` alternate data stream on every file extracted
from a downloaded .zip.

HISTORY. Under pywebview the .NET Framework assembly loader refused to load
Python.Runtime.dll out of the Internet zone, so clr_loader could not resolve
its entry point and the WinForms backend died on import:

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
    ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

Reported from a fresh PC, and reproduced by putting that one stream on that one
DLL in a working venv — the error was identical, and removing the stream cured
it. The Tauri shell has no CLR and no pythonnet, so that specific failure is
gone; the sweep stays because the marking has not, and these tests hold it to
the behaviour that matters: cheap when clean, thorough when not, and never
fatal. It is structurally invisible in development: a build made locally was
never downloaded, so it is never marked.
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
    """An installed build, in miniature: the Tauri shell, the backend beside
    it, and the backend's one-dir tree. Returns the sweep's probe file."""
    internal = root / "_internal"
    internal.mkdir(parents=True)
    (internal / "base_library.zip").write_bytes(b"PK")
    for n in ("python311.dll", "libssl-3.dll", "select.pyd"):
        (internal / n).write_bytes(b"MZ")
    (root / "omni-exec.exe").write_bytes(b"MZ")        # the Tauri shell
    (root / "omni-exec-py.exe").write_bytes(b"MZ")     # the backend
    return internal / "base_library.zip"


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    root = tmp_path / "omni-exec"
    root.mkdir()
    probe = _fake_install(root)
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    # The sweep runs in the BACKEND, so sys.executable is omni-exec-py.exe.
    monkeypatch.setattr(main.sys, "executable", str(root / "omni-exec-py.exe"))
    monkeypatch.setattr(main.sys, "platform", "win32")
    return root, probe


def test_a_marked_install_is_unblocked(frozen):
    root, probe = frozen
    for p in root.rglob("*"):
        if p.is_file():
            _mark(p)
    assert main._has_motw(probe)

    cleared = main._unblock_app_files()

    assert cleared >= 5
    assert not main._has_motw(probe)
    # THE WHOLE TREE, and that includes the Tauri shell sitting beside the
    # backend: a marked extraction marks every file, and the sweep is run by
    # the one process that is already loaded.
    for p in root.rglob("*"):
        if p.is_file():
            assert not main._has_motw(p), f"{p.name} is still marked"
    assert not main._has_motw(root / "omni-exec.exe")


def test_an_unmarked_install_is_left_alone(frozen):
    """The normal case — an installer-placed build — must cost one stat, not
    a walk of 1,200 files on every launch."""
    root, probe = frozen
    assert main._unblock_app_files() == 0


def test_a_mark_inside_the_tree_still_triggers_the_sweep(frozen):
    """The probe is a file INSIDE _internal, not the exe: a user who
    right-clicks the executable and unblocks just that has fixed one file out
    of a thousand, and the sweep still has to notice."""
    root, probe = frozen
    _mark(probe)
    assert main._unblock_app_files() == 1
    assert not main._has_motw(probe)


def test_running_from_source_is_a_no_op(tmp_path, monkeypatch):
    """There is no install tree to sweep, and sys.executable is the
    interpreter — walking its directory would be both pointless and rude."""
    monkeypatch.setattr(main.sys, "frozen", False, raising=False)
    assert main._unblock_app_files() == 0


def test_an_unremovable_mark_does_not_crash_the_launch(frozen, monkeypatch):
    """Best-effort by design: a read-only install cannot be unmarked, and
    failing to start is far worse than starting and possibly failing later."""
    root, probe = frozen
    _mark(probe)

    def boom(path):
        raise PermissionError("read-only")

    monkeypatch.setattr(main.os, "remove", boom)
    assert main._unblock_app_files() == 0     # no exception
