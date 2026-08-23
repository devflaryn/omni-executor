r"""Replacing an install whose FOLDER cannot be renamed or deleted.

TWO REGRESSIONS, both reported from real machines, both the same underlying
fact: Windows will not rename or delete a directory while any process holds a
handle inside it, and this app leaves such processes behind on purpose.

  the installer   `[WinError 183] Cannot create a file when that file already
                  exists: '...\Programs\OmniExecutor'` -- a directory the user
                  can plainly see exists, named at them with nothing to act on,
                  failing every reinstall attempt identically.
                  (2026-08-18, and again 2026-08-22.)

  the updater     `[WinError 32] The process cannot access the file because it
                  is being used by another process: '...\Programs\OmniExecutor'
                  -> '...\Programs\OmniExecutor.old'` -- as a PyInstaller crash
                  dialog, from a program the user never launched.
                  (2026-08-16, -18 and -22, all three in one update.log.)

The holder is not exotic. `update_app_restart` states outright that QEMU is
detached and the VMs outlive the app, every instance also runs a `_windowlock`
that IS omni-exec.exe out of the install directory, and all of them inherit
that directory as their working directory. A user with an instance running --
most of them, most of the time -- could never update.

bootstrap.replace_tree therefore never touches a DIRECTORY. Files are what it
moves, and moving a file whose image is loaded is allowed: it is how every
self-updating program on Windows works.

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

import bootstrap                                            # noqa: E402

BUSY = 32


def _busy(*_a, **_k):
    raise OSError(BUSY, "used by another process")


def _build(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _tree(root):
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_text(encoding="utf-8")
            for p in root.rglob("*") if p.is_file()}


# ----------------------------------------------------------- the happy path

def test_it_replaces_rather_than_merges(tmp_path):
    """A file the old build had and the new one does not must be GONE.

    The whole reason the installer never merged: a leftover from an older
    build is exactly the thing that loads instead of its replacement, and is
    impossible to diagnose afterwards.
    """
    source = _build(tmp_path / "new", {
        "omni-exec.exe": "v2",
        "_internal/base_library.zip": "v2 lib",
        "_internal/frontend/dist/index.html": "<html>v2",
    })
    target = _build(tmp_path / "OmniExecutor", {
        "omni-exec.exe": "v1",
        "_internal/base_library.zip": "v1 lib",
        "_internal/gone-in-v2.dll": "stale",
        "configs/old-only/paths.json": "{}",
    })

    report = bootstrap.replace_tree(source, target)

    assert _tree(target) == _tree(source)
    assert report["files"] == 3 and report["parked"] == 4


def test_it_installs_into_a_directory_that_is_not_there_yet(tmp_path):
    source = _build(tmp_path / "new", {"omni-exec.exe": "v2"})
    target = tmp_path / "Programs" / "OmniExecutor"

    bootstrap.replace_tree(source, target)

    assert (target / "omni-exec.exe").read_text(encoding="utf-8") == "v2"


# -------------------------------------------------- the reported failures

def test_a_folder_that_can_be_neither_renamed_nor_deleted_is_still_replaced(
        tmp_path, monkeypatch):
    """THE REPORTED CASE, for both the installer and the updater.

    Every directory operation fails, which is what a held handle does. The
    install must still be replaced: that is the entire claim.
    """
    source = _build(tmp_path / "new", {"omni-exec.exe": "v2",
                                       "_internal/python314.dll": "v2 dll"})
    target = _build(tmp_path / "OmniExecutor", {"omni-exec.exe": "v1",
                                                "_internal/python314.dll": "v1 dll"})

    real_rmtree = shutil.rmtree
    real_rename = os.rename

    def no_directory_moves(src, dst, *a, **k):
        if Path(src).is_dir():
            _busy()                       # ... 'OmniExecutor' -> '...old'
        return real_rename(src, dst, *a, **k)

    def no_directory_deletes(path, *a, **k):
        if k.get("ignore_errors"):
            return None                   # silently does nothing, as reported
        _busy()

    monkeypatch.setattr(os, "rename", no_directory_moves)
    monkeypatch.setattr(bootstrap.shutil, "rmtree", no_directory_deletes)

    bootstrap.replace_tree(source, target)

    monkeypatch.setattr(bootstrap.shutil, "rmtree", real_rmtree)
    installed = {k: v for k, v in _tree(target).items()
                 if not k.startswith(bootstrap._TRASH_PREFIX)}
    assert installed == _tree(source)


def test_the_previous_build_is_left_parked_when_it_cannot_be_deleted(
        tmp_path, monkeypatch):
    """A DLL still loaded can be renamed but not removed. Better to leave the
    old bytes parked than to fail an update that otherwise worked."""
    source = _build(tmp_path / "new", {"omni-exec.exe": "v2"})
    target = _build(tmp_path / "OmniExecutor", {"omni-exec.exe": "v1"})

    monkeypatch.setattr(bootstrap.shutil, "rmtree",
                        lambda *a, **k: None if k.get("ignore_errors") else _busy())
    report = bootstrap.replace_tree(source, target)

    assert report["leftover"], "the caller is told there is something to sweep"
    parked = Path(report["leftover"]) / "omni-exec.exe"
    assert parked.read_text(encoding="utf-8") == "v1"
    assert (target / "omni-exec.exe").read_text(encoding="utf-8") == "v2"


def test_the_leftovers_are_swept_on_a_later_launch(tmp_path):
    """The other half. By the next start, whatever held those files is gone."""
    target = tmp_path / "OmniExecutor"
    _build(target, {"omni-exec.exe": "v2",
                    f"{bootstrap._TRASH_PREFIX}1700000000/omni-exec.exe": "v1",
                    f"{bootstrap._TRASH_PREFIX}1700000001/_internal/py.dll": "v1"})

    cleared = bootstrap.sweep_replaced(target)

    assert cleared == 2
    assert _tree(target) == {"omni-exec.exe": "v2"}


def test_sweeping_leaves_the_install_alone_when_there_is_nothing_parked(tmp_path):
    target = _build(tmp_path / "OmniExecutor", {"omni-exec.exe": "v2",
                                                "_internal/py.dll": "v2"})
    assert bootstrap.sweep_replaced(target) == 0
    assert _tree(target) == {"omni-exec.exe": "v2", "_internal/py.dll": "v2"}


# ------------------------------------------------------------- all or nothing

def test_a_file_that_will_not_move_stops_the_install_and_says_which(
        tmp_path, monkeypatch):
    """A leftover that cannot be moved aside is a real problem -- it is the
    stale binary that would load instead of its replacement -- so it must stop
    the install and name itself. Carrying on is what produced WinError 183."""
    source = _build(tmp_path / "new", {"omni-exec.exe": "v2"})
    target = _build(tmp_path / "OmniExecutor", {"locked.dll": "cannot go"})

    monkeypatch.setattr(os, "rename", _busy)

    with pytest.raises(bootstrap.BootstrapError) as excinfo:
        bootstrap.replace_tree(source, target)

    message = str(excinfo.value)
    assert "locked.dll" in message, "name the file that is in the way"
    assert "reboot" in message.lower(), "give the user something to do"


def test_the_previous_build_is_restored_when_the_copy_fails(tmp_path, monkeypatch):
    """A half-copied application directory is the one outcome worse than not
    updating at all, so the old files go back exactly where they were."""
    source = _build(tmp_path / "new", {"omni-exec.exe": "v2",
                                       "_internal/a.dll": "v2 a",
                                       "_internal/b.dll": "v2 b"})
    target = _build(tmp_path / "OmniExecutor", {"omni-exec.exe": "v1",
                                                "_internal/a.dll": "v1 a",
                                                "_internal/b.dll": "v1 b"})
    before = _tree(target)

    calls = {"n": 0}
    real_copy2 = shutil.copy2

    def copy2(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:               # partway through, as a full disk would
            raise OSError(112, "there is not enough space on the disk")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(bootstrap.shutil, "copy2", copy2)

    with pytest.raises(OSError):
        bootstrap.replace_tree(source, target)

    assert _tree(target) == before, "every file back, and nothing of v2 left"


def test_a_rolled_back_target_keeps_no_parked_copy(tmp_path, monkeypatch):
    """Rollback must not leave the old build in two places: the sweep would
    then delete a directory the install still needs to be found in."""
    source = _build(tmp_path / "new", {"omni-exec.exe": "v2"})
    target = _build(tmp_path / "OmniExecutor", {"omni-exec.exe": "v1"})

    monkeypatch.setattr(bootstrap.shutil, "copy2", _busy)
    with pytest.raises(OSError):
        bootstrap.replace_tree(source, target)

    assert list(target.iterdir()) == [target / "omni-exec.exe"]


def test_nothing_to_install_from_is_refused_before_anything_is_moved(tmp_path):
    target = _build(tmp_path / "OmniExecutor", {"omni-exec.exe": "v1"})

    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.replace_tree(tmp_path / "does-not-exist", target)

    assert _tree(target) == {"omni-exec.exe": "v1"}


def test_a_previous_run_s_parked_files_are_never_reinstalled(tmp_path):
    """Parked leftovers are not part of the install and must not be treated as
    files to preserve -- otherwise every failed update doubles the folder."""
    source = _build(tmp_path / "new", {"omni-exec.exe": "v3"})
    target = _build(tmp_path / "OmniExecutor", {
        "omni-exec.exe": "v2",
        f"{bootstrap._TRASH_PREFIX}1700000000/omni-exec.exe": "v1",
    })

    report = bootstrap.replace_tree(source, target)

    assert report["parked"] == 1, "only the live file, not the parked one"
    assert _tree(target) == {"omni-exec.exe": "v3"}
