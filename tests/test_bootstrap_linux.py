"""Linux/x86 half of the first-boot installer.

NOTE ON FIXTURE ORDER: `tmp_path` is requested BEFORE the `linux` fixture in
every test that needs both. Patching sys.platform to "linux" is global, and
pytest's own tmp_path setup then takes its POSIX branch and calls os.getuid(),
which does not exist on the Windows box these tests also run on.

Linux runs the SAME x86 Bliss base as Windows -- same qcow2, same kernel and
initrd, same arceus offset -- because both are x86_64 hosts. What differs is
the tooling policy: Windows downloads a portable QEMU (bootstrap installs it),
while Linux uses SYSTEM QEMU from apt and must therefore never be handed a
`qemu.download_url`, the way omnidroid's own build-linux.sh describes.

The bug these tests exist to prevent is silent and expensive: `current_os()`
used to answer "win" or "mac" with no third case, so a Linux host fell into
the mac branch and asked the dist API for the MAC manifest -- a 3.9 GB arm64
base and an arm offset, neither of which an x86_64 Linux box can boot. It
would download the better part of four gigabytes before failing.
"""
import pytest

import bootstrap


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    return monkeypatch


def _seed_x86(rt):
    x = rt / "images" / "x86"
    x.mkdir(parents=True, exist_ok=True)
    for n in ["base_x86.qcow2", "base_x86.kernel", "base_x86.initrd.img",
              "data-template-8g.qcow2",
              "base_x86_data_offset_arceusremote.qcow2"]:
        (x / n).write_bytes(b"x")
    return x


# ---------------------------------------------------------------- os identity

def test_current_os_is_linux_on_linux(linux):
    assert bootstrap.current_os() == "linux"


def test_linux_does_not_masquerade_as_mac(linux):
    """The whole point: a Linux host must not be served the arm64 mac base."""
    assert bootstrap.current_os() != "mac"


# ------------------------------------------------------------------ tooling

def test_linux_wants_the_x86_emulator(linux):
    """x86_64, like Windows -- not the aarch64 one macOS needs."""
    assert bootstrap._qemu_system_name() == "qemu-system-x86_64"


def test_linux_qemu_hint_names_apt(linux):
    hint = bootstrap._qemu_hint()
    assert "apt" in hint.lower()


# ------------------------------------------------- the engine's paths.json

def test_configure_engine_registers_the_x86_base_on_linux(tmp_path, linux,
                                                          monkeypatch):
    monkeypatch.setattr(bootstrap, "find_qemu", lambda rt: None)
    monkeypatch.setattr(bootstrap, "_apply_tool_env", lambda rt: None)
    _seed_x86(tmp_path)

    bootstrap.configure_engine(tmp_path)

    import json
    cfg = json.loads((tmp_path / "paths.json").read_text())
    assert cfg["current_base"] == "x86"
    base = cfg["bases"]["x86"]
    assert base["type"] == "x86-bliss"
    assert base["disk"] == "x86/base_x86.qcow2"
    # The offset must be found and, being the only one, become the default.
    assert base["default_offset"] == "arceusremote"
    # Every recorded name carries its arch subfolder, or the qcow2 backing
    # reference cannot resolve beside the /data template it overlays.
    assert cfg["data_template"] == "x86/data-template-8g.qcow2"


def test_linux_never_gets_a_qemu_download_url(tmp_path, linux, monkeypatch):
    """SYSTEM QEMU is the Linux policy. A download_url here would make
    omnidroid's ensure_qemu() fetch the WINDOWS portable build on Linux."""
    monkeypatch.setattr(bootstrap, "find_qemu", lambda rt: None)
    monkeypatch.setattr(bootstrap, "_apply_tool_env", lambda rt: None)
    _seed_x86(tmp_path)

    bootstrap.configure_engine(tmp_path)

    import json
    cfg = json.loads((tmp_path / "paths.json").read_text())
    assert "download_url" not in cfg["qemu"]
