"""The PyInstaller specs must not drift apart.

PyInstaller's analysis walks bytecode, so it *does* find a plain
`import accountsync` inside a function — that was checked with a control build,
and those modules land in the PYZ with or without a hiddenimports entry. What it
cannot resolve on its own is a dependency that is imported conditionally AND
ships native binaries or package data: selenium (whose Selenium Manager is an
executable), tkinter and PIL (the built-in VNC viewer). Each of those has
already caused a "works from source, broken when frozen" bug here.

The failure mode these tests exist for is subtler than a missing entry: a fix
gets applied to the spec of whichever platform it broke on, and never mirrored.
That is exactly what had happened — the Windows spec carried the selenium and
PIL fixes and the macOS spec did not, so the same bugs were sitting unfixed in
every Mac build.
"""

import re
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
WIN_SPEC = PROJECT / "OmniExecutor-win.spec"
MAC_SPEC = PROJECT / "OmniExecutor.spec"
LINUX_SPEC = PROJECT / "OmniExecutor-linux.spec"
SPECS = [WIN_SPEC, MAC_SPEC, LINUX_SPEC]

# Imported conditionally and carrying native binaries or package data, so
# PyInstaller cannot pull them in unaided. Every one of these is a bug that
# already happened.
RUNTIME_ONLY = ("selenium", "tkinter", "PIL", "omnidroid")


def _declared(spec_path):
    """Every name a spec names — hiddenimports literals, collect_submodules()
    and collect_data_files() alike."""
    text = spec_path.read_text(encoding="utf-8")
    names = set()
    for chunk in re.findall(r"hiddenimports\s*\+?=\s*\[([^\]]*)\]", text):
        names |= set(re.findall(r"[\"']([\w.]+)[\"']", chunk))
    for call in ("collect_submodules", "collect_data_files"):
        names |= set(re.findall(rf"{call}\(\s*[\"']([\w.]+)[\"']", text))
    # PIL.Image / PIL.ImageTk should satisfy a check for "PIL"
    return names | {n.split(".")[0] for n in names}


@pytest.mark.parametrize("spec", SPECS, ids=lambda p: p.name)
def test_runtime_only_dependencies_are_declared(spec):
    missing = [n for n in RUNTIME_ONLY if n not in _declared(spec)]
    assert not missing, (
        f"{spec.name} does not declare {missing}. These are imported inside "
        f"functions and ship native binaries or data, so PyInstaller cannot "
        f"collect them on its own — the frozen app fails at runtime, not at "
        f"build time."
    )


def test_every_spec_declares_the_same_things():
    """A dependency added to one spec and not the others means the fix shipped
    on one platform only — which is how a Mac-only crash gets written. With a
    third platform the odds of that go up, not down, so Linux is held to the
    same bar rather than being allowed to lag."""
    declared = {s.name: _declared(s) for s in SPECS}
    union = set().union(*declared.values())
    gaps = {name: sorted(union - names)
            for name, names in declared.items() if names != union}
    assert not gaps, f"specs disagree — missing per spec: {gaps}"


def test_selenium_manager_binary_is_collected():
    """collect_submodules('selenium') gets the Python; the chromedriver
    resolver also needs Selenium Manager, which is a native executable shipped
    as package data."""
    for spec in SPECS:
        text = spec.read_text(encoding="utf-8")
        assert re.search(r"collect_data_files\(\s*[\"']selenium[\"']", text), (
            f"{spec.name} must collect selenium's data files, or "
            f"_resolve_chromedriver() cannot download a matching driver"
        )
