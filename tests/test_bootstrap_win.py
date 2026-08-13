"""Windows/x86 half of the first-boot installer.

The client must ask the dist API for the base that matches the machine it is
running on -- Windows gets the x86 Bliss base and an x86 arceus offset, not
the arm64 pair -- and then hand omnidroid a paths.json describing an
x86-bliss base, in the exact schema its loader (bases.py) reads.

The x86 entry is NOT the arm entry with different filenames. An x86 base
boots a system disk directly with -kernel/-initrd and no UEFI, so it carries
`disk`/`kernel`/`initrd` (never `system`/`data`/`efivars`) plus a `src`
kernel argument, and its per-instance /data is seeded from a shared template
recorded at the top level as `data_template`. Verified against the real
engine on a Windows host: qemu_proc.qemu_command() reads base['src'] when it
builds the x86 command line, so an entry without it cannot boot.
"""
import json
import sys

import pytest

import bootstrap


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    return monkeypatch


def _seed_x86(rt, *, offsets=(), rooted_initrd=False):
    x = rt / "images" / "x86"
    x.mkdir(parents=True, exist_ok=True)
    names = ["base_x86.qcow2", "base_x86.kernel", "base_x86.initrd.img",
             "data-template-8g.qcow2"]
    if rooted_initrd:
        names.append("base_x86_rooted.initrd.img")
    names += [f"base_x86_data_offset_{n}.qcow2" for n in offsets]
    for n in names:
        (x / n).write_bytes(b"x")
    return x


# ---------------------------------------------------------------- os + paths

def test_current_os_is_win_on_windows(win):
    assert bootstrap.current_os() == "win"


def test_current_os_is_mac_elsewhere(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    assert bootstrap.current_os() == "mac"


def test_runtime_dir_uses_localappdata(win, tmp_path):
    win.delenv("OMNIEXEC_RUNTIME_DIR", raising=False)
    win.setenv("LOCALAPPDATA", str(tmp_path))
    p = bootstrap.runtime_dir()
    assert p == tmp_path / "OmniExec"
    assert p.exists()


def test_runtime_dir_falls_back_to_appdata(win, tmp_path):
    win.delenv("OMNIEXEC_RUNTIME_DIR", raising=False)
    win.delenv("LOCALAPPDATA", raising=False)
    win.setenv("APPDATA", str(tmp_path))
    assert bootstrap.runtime_dir() == tmp_path / "OmniExec"


def test_the_override_still_wins(win, tmp_path):
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path / "custom"))
    win.setenv("LOCALAPPDATA", str(tmp_path / "ignored"))
    assert bootstrap.runtime_dir() == tmp_path / "custom"


# ------------------------------------------------------- arch-aware manifest

def test_the_manifest_url_carries_this_os(win, monkeypatch):
    # Windows must never silently fetch the mac manifest and download an
    # arm64 base it cannot boot.
    seen = {}

    class FakeResp:
        status = 200

        def read(self):
            return b'{"ok": true, "artifacts": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=30):
        seen["url"] = url
        return FakeResp()

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fake_urlopen)
    bootstrap.read_manifest("http://example.invalid")

    assert "os=win" in seen["url"]


def test_ensure_runtime_asks_for_this_os(win, tmp_path, monkeypatch):
    seen = {}

    def fake_read(base_url, os_name=None, channel="stable"):
        seen["os"] = os_name or bootstrap.current_os()
        return {"ok": True, "artifacts": [], "app": {"version": "1"}}

    monkeypatch.setattr(bootstrap, "read_manifest", fake_read)
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    bootstrap.ensure_runtime(base_url="http://example.invalid")

    assert seen["os"] == "win"


# --------------------------------------------------------- configure_engine

def test_configure_engine_registers_an_x86_base(win, tmp_path, monkeypatch):
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    _seed_x86(tmp_path, offsets=["arceusremote"])
    monkeypatch.setattr(bootstrap.shutil, "which",
                        lambda n: "C:/q/qemu-system-x86_64.exe"
                        if "x86_64" in n else None)

    eng = bootstrap.configure_engine(tmp_path)
    cfg = json.loads((tmp_path / "paths.json").read_text())

    assert cfg["current_base"] == "x86"
    b = cfg["bases"]["x86"]
    assert b["type"] == "x86-bliss"
    assert b["disk"] == "x86/base_x86.qcow2"
    assert b["kernel"] == "x86/base_x86.kernel"
    assert b["initrd"] == "x86/base_x86.initrd.img"
    # An x86 base has no arm-shaped keys at all.
    assert "system" not in b and "data" not in b and "efivars" not in b
    # qemu_command() indexes base['src'] directly -- without it, boot raises.
    assert b["src"]
    assert cfg["data_template"] == "x86/data-template-8g.qcow2"
    assert eng["qemu_ok"] is True


def test_it_registers_the_x86_offsets_it_finds(win, tmp_path, monkeypatch):
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    _seed_x86(tmp_path, offsets=["arceusremote"])
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "q")

    bootstrap.configure_engine(tmp_path)
    b = json.loads((tmp_path / "paths.json").read_text())["bases"]["x86"]

    assert b["offsets"]["arceusremote"]["data"] == \
        "x86/base_x86_data_offset_arceusremote.qcow2"
    # Exactly one baked version is unambiguously the default.
    assert b["default_offset"] == "arceusremote"


def test_two_offsets_leave_the_default_unset(win, tmp_path, monkeypatch):
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    _seed_x86(tmp_path, offsets=["arceusremote", "2.740.101"])
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "q")

    bootstrap.configure_engine(tmp_path)
    b = json.loads((tmp_path / "paths.json").read_text())["bases"]["x86"]

    assert set(b["offsets"]) == {"arceusremote", "2.740.101"}
    assert "default_offset" not in b


def test_it_prefers_the_rooted_initrd(win, tmp_path, monkeypatch):
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    _seed_x86(tmp_path, rooted_initrd=True)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "q")

    bootstrap.configure_engine(tmp_path)
    b = json.loads((tmp_path / "paths.json").read_text())["bases"]["x86"]

    assert b["initrd"] == "x86/base_x86_rooted.initrd.img"


def test_qemu_download_url_is_wired_for_ensure_qemu(win, tmp_path,
                                                    monkeypatch):
    # omnidroid's ensure_qemu() silently no-ops unless qemu.download_url is
    # set, so on Windows -- where QEMU is not a brew install away -- the
    # client must supply one or the engine can never self-install it.
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    _seed_x86(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: None)

    bootstrap.configure_engine(tmp_path)
    cfg = json.loads((tmp_path / "paths.json").read_text())

    assert cfg["qemu"]["download_url"]


def test_the_qemu_url_is_overridable(win, tmp_path, monkeypatch):
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    win.setenv("OMNI_QEMU_WIN_URL", "http://example.invalid/qemu.exe")
    _seed_x86(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: None)

    bootstrap.configure_engine(tmp_path)
    cfg = json.loads((tmp_path / "paths.json").read_text())

    assert cfg["qemu"]["download_url"] == "http://example.invalid/qemu.exe"


def test_no_images_yet_is_not_an_error(win, tmp_path, monkeypatch):
    # Genuinely fresh first boot: the runtime dir exists, nothing downloaded.
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: None)

    bootstrap.configure_engine(tmp_path)  # must not raise
    cfg = json.loads((tmp_path / "paths.json").read_text())

    assert cfg["bases"] == {}
    assert cfg["current_base"] is None


# ------------------------------------------------------------- engine_ready

def test_engine_ready_looks_for_the_x86_emulator(win, tmp_path, monkeypatch):
    asked = []

    def which(n):
        asked.append(n)
        return "C:/q/qemu-system-x86_64.exe" if "x86_64" in n else None

    monkeypatch.setattr(bootstrap.shutil, "which", which)
    r = bootstrap.engine_ready(tmp_path)

    assert r["qemu_ok"] is True
    assert any("x86_64" in n for n in asked)
    assert not any("aarch64" in n for n in asked)


def test_the_windows_hint_is_not_brew(win, tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: None)
    r = bootstrap.engine_ready(tmp_path)

    assert r["qemu_ok"] is False
    assert "brew" not in (r["qemu_hint"] or "").lower()


# ------------------------------------------------- pointer (redirect) entries

def test_a_sha_less_artifact_is_not_downloaded(win):
    # REGRESSION: `qemu-win` is a 302 POINTER, not a stored blob, so the
    # manifest reports sha256:null for it. It used to be planned like any
    # other artifact, and download_blob compared the downloaded bytes against
    # None -- which can never match -- so a first boot died with
    # "qemu-win: sha256 mismatch after 3 attempts" after pulling 197 MB.
    manifest = {"artifacts": [
        {"name": "base-x86", "sha256": "abc", "bytes": 10},
        {"name": "qemu-win", "sha256": None, "bytes": None},
    ]}
    plan = bootstrap.plan_downloads(manifest, {"artifacts": {}})
    assert [a["name"] for a in plan] == ["base-x86"]


def test_a_sha_less_artifact_never_looks_stale(win):
    # It must not reappear in the plan on every launch either.
    manifest = {"artifacts": [{"name": "qemu-win", "sha256": None}]}
    assert bootstrap.plan_downloads(manifest, {"artifacts": {}}) == []


def test_download_blob_refuses_an_unverifiable_artifact(win, tmp_path):
    # Belt and braces: if one is ever passed directly, say WHY rather than
    # reporting a hash mismatch against nothing.
    with pytest.raises(bootstrap.BootstrapError) as e:
        bootstrap.download_blob("http://example.invalid",
                                {"name": "qemu-win", "sha256": None,
                                 "url": "/omni/dist/blob/qemu-win"},
                                tmp_path / "x.part")
    assert "sha256" in str(e.value).lower()
    assert "qemu-win" in str(e.value)


def test_qemu_download_url_points_at_the_dist_api(win, tmp_path, monkeypatch):
    # Served through the dist API's redirect rather than baked at the
    # upstream URL, so an expired installer is a server-side fix instead of
    # a client release. ensure_qemu() uses urlopen, which follows the 302.
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    win.delenv("OMNI_QEMU_WIN_URL", raising=False)
    win.setenv("OMNI_EXEC_BASE", "http://dist.example.invalid")
    _seed_x86(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: None)

    bootstrap.configure_engine(tmp_path)
    cfg = json.loads((tmp_path / "paths.json").read_text())

    assert cfg["qemu"]["download_url"] == \
        "http://dist.example.invalid/omni/dist/blob/qemu-win"


def test_progress_is_recorded_after_each_artifact(win, tmp_path, monkeypatch):
    # REGRESSION: installed.json used to be written only after the WHOLE plan
    # succeeded, so a failure on the last artifact discarded the receipt for
    # gigabytes already on disk and the next launch re-downloaded all of it.
    win.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    manifest = {"ok": True, "app": {"version": "9"}, "artifacts": [
        {"name": "big", "sha256": "aa", "bytes": 1, "url": "/b/big",
         "dest": "images/x86", "version": "1"},
        {"name": "boom", "sha256": "bb", "bytes": 1, "url": "/b/boom",
         "dest": "images/x86", "version": "1"},
    ]}
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(bootstrap, "_precheck_space", lambda *a, **k: None)

    def fake_download(base_url, artifact, tmp, progress=None):
        if artifact["name"] == "boom":
            raise bootstrap.BootstrapError("sha256 mismatch")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(b"x")

    monkeypatch.setattr(bootstrap, "download_blob", fake_download)

    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.ensure_runtime(base_url="http://example.invalid")

    state = json.loads((tmp_path / "installed.json").read_text())
    assert "big" in state["artifacts"], "the completed artifact must survive"
    assert "boom" not in state["artifacts"]
