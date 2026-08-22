r"""Replacing an install whose folder cannot be renamed or deleted.

REGRESSION, reported from a real machine 2026-08-18. A leftover
OmniExecutorSetup.exe from a previous failed run held
%LOCALAPPDATA%\Programs\OmniExecutor open. The installer's
`shutil.rmtree(target, ignore_errors=True)` then silently did nothing, and the
next line -- `shutil.copytree(source, target)` with no `dirs_exist_ok` -- raised

    [WinError 183] Cannot create a file when that file already exists:
    '...\Programs\OmniExecutor'

naming a directory the user can plainly see exists, giving them nothing to act
on, and failing every reinstall attempt identically.

The fakes below patch `os.rename` / `os.unlink` rather than the pathlib methods
that call them: patching `Path.rename` on the class corrupts pathlib's own
internals on 3.14 (AttributeError on `_str_normcase_cached`), which is a test
artefact and not the behaviour under test.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import installer                                            # noqa: E402

BUSY = 183


def _busy(*_a, **_k):
    raise OSError(BUSY, "used by another process")


def test_clear_target_renames_aside_when_it_can(tmp_path):
    """The happy path still leaves a rollback copy."""
    target = tmp_path / "OmniExecutor"
    target.mkdir()
    (target / "stale.txt").write_text("old build")

    old = installer._clear_target(target)

    assert old is not None and old.exists()
    assert (old / "stale.txt").exists(), "the rollback copy keeps the contents"


def test_empties_a_directory_it_can_neither_move_nor_delete(tmp_path, monkeypatch):
    """The reported case.

    Rename and delete of the DIRECTORY both fail, which is what a held handle
    does. The contents must still go -- a stale file that survives is exactly
    the one that loads instead of its replacement -- and the empty directory
    itself is allowed to remain.
    """
    target = tmp_path / "OmniExecutor"
    target.mkdir()
    (target / "stale.txt").write_text("old build")
    (target / "sub").mkdir()
    (target / "sub" / "deep.dll").write_text("older build")

    real_rmtree = shutil.rmtree

    def rmtree(path, *a, **k):
        if Path(path) == target:
            if k.get("ignore_errors"):
                return None
            _busy()
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(os, "rename", _busy)
    monkeypatch.setattr(installer.shutil, "rmtree", rmtree)

    old = installer._clear_target(target)

    assert old is None
    assert target.exists(), "the locked directory itself may remain"
    assert list(target.iterdir()) == [], "but nothing stale may survive inside"


def test_an_undeletable_file_is_reported_not_swallowed(tmp_path, monkeypatch):
    """A leftover that cannot be removed must STOP the install and say why.

    Carrying on is what produced the opaque WinError 183, and it would leave a
    stale binary that loads instead of its replacement.
    """
    target = tmp_path / "OmniExecutor"
    target.mkdir()
    (target / "locked.dll").write_text("cannot go")

    def rmtree(path, *a, **k):
        if k.get("ignore_errors"):
            return None
        _busy()

    monkeypatch.setattr(os, "rename", _busy)
    monkeypatch.setattr(os, "unlink", _busy)
    monkeypatch.setattr(installer.shutil, "rmtree", rmtree)

    with pytest.raises(installer.InstallError) as excinfo:
        installer._clear_target(target)

    message = str(excinfo.value)
    assert "locked.dll" in message, "name the file that is in the way"
    assert "reboot" in message.lower(), "give the user something to do"


def test_copytree_call_tolerates_an_existing_directory():
    """The other half of the fix.

    Read off the source rather than exercised end-to-end, because the install
    path downloads a build. Without `dirs_exist_ok` a target that
    `_clear_target` could only EMPTY still raises WinError 183 -- which is the
    exact reported failure.
    """
    source = Path(installer.__file__).read_text(encoding="utf-8")
    assert "shutil.copytree(source, target, dirs_exist_ok=True)" in source
