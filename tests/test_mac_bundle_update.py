"""A macOS build depends on symlinks: `_internal/Python` is a link to the Python
library. Lose one and the downloaded helper can't even start, so no update ever
installs. These need real symlinks, so they run on macOS and Linux."""
import os
import stat
import sys
import zipfile

import pytest

import bootstrap

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")


def _add_link(zf, name, pointee):
    info = zipfile.ZipInfo(name)
    info.external_attr = (stat.S_IFLNK | 0o755) << 16
    zf.writestr(info, pointee)


def _zip(path, files=(), links=()):
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            zf.writestr(info, body)
        for name, pointee in links:
            _add_link(zf, name, pointee)
    return path


def test_links_come_back_as_links(tmp_path):
    z = _zip(tmp_path / "a.zip",
             files=[("App/lib/Versions/3.13/Python", "library")],
             links=[("App/lib/Python", "Versions/Current/Python"), ("App/lib/Versions/Current", "3.13")])
    dest = tmp_path / "out"
    dest.mkdir()
    with zipfile.ZipFile(z) as zf:
        bootstrap._safe_extract_zip(zf, dest)
    python = dest / "App/lib/Python"
    assert python.is_symlink() and python.read_text() == "library"
    assert os.access(dest / "App/lib/Versions/3.13/Python", os.X_OK)


@pytest.mark.parametrize("pointee", ["../../outside", "/etc/passwd"])
def test_a_link_out_of_the_archive_is_refused(tmp_path, pointee):
    z = _zip(tmp_path / "a.zip", links=[("App/evil", pointee)])
    dest = tmp_path / "out"
    dest.mkdir()
    with zipfile.ZipFile(z) as zf, pytest.raises(bootstrap.BootstrapError):
        bootstrap._safe_extract_zip(zf, dest)
    assert not (dest / "App/evil").is_symlink()


def _tree(root):
    (root / "lib/Versions/3.13").mkdir(parents=True)
    (root / "lib/Versions/3.13/Python").write_text("library " + root.name)
    os.symlink("3.13", root / "lib/Versions/Current")
    os.symlink("Versions/Current/Python", root / "lib/Python")
    return root


def test_replacing_a_build_keeps_its_links(tmp_path):
    source, target = _tree(tmp_path / "new"), _tree(tmp_path / "old")
    bootstrap.replace_tree(source, target)
    assert (target / "lib/Versions/Current").is_symlink()
    assert (target / "lib/Python").is_symlink()
    assert (target / "lib/Python").read_text() == "library new"


def test_replacing_links_with_real_files_leaves_no_stale_link(tmp_path):
    """What a published zip does to an installed .app: real files where links were."""
    target = _tree(tmp_path / "old")
    source = tmp_path / "new"
    (source / "lib/Versions/3.13").mkdir(parents=True)
    (source / "lib/Versions/3.13/Python").write_text("library new")
    (source / "lib/Python").write_text("library new")
    bootstrap.replace_tree(source, target)
    assert not (target / "lib/Python").is_symlink()
    assert (target / "lib/Python").read_text() == "library new"
    assert not (target / "lib/Versions/Current").exists()


def test_a_real_folder_can_become_a_link(tmp_path):
    source = _tree(tmp_path / "new")
    target = tmp_path / "old"
    (target / "lib/Versions/Current").mkdir(parents=True)
    (target / "lib/Versions/Current/Python").write_text("old")
    bootstrap.replace_tree(source, target)
    assert (target / "lib/Versions/Current").is_symlink()
    assert (target / "lib/Python").read_text() == "library new"
