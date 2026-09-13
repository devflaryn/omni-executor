import os
import io
import hashlib
import zipfile
from pathlib import Path
import pytest
import miner


def test_miner_dir_under_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    d = miner.miner_dir()
    assert d == Path(tmp_path) / "miner"


def test_install_verifies_and_places(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    payload = b"fake-xmrig-binary"
    sha = hashlib.sha256(payload).hexdigest()
    artifact = {"name": "xmrig-win", "url": "/omni/dist/blob/xmrig", "bytes": len(payload), "sha256": sha, "kind": "tool", "dest": "miner", "exe": "xmrig.exe"}

    monkeypatch.setattr(miner.bootstrap, "read_manifest",
                        lambda *a, **k: {"ok": True, "artifacts": [artifact]})
    monkeypatch.setattr(miner.bootstrap, "dist_base", lambda: "http://x")

    def fake_download(base, art, tmp, progress=None):
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(payload)
        if progress:
            progress({"phase": "download", "artifact": art["name"], "received": len(payload), "total": len(payload), "percent": 100})
    monkeypatch.setattr(miner.bootstrap, "download_blob", fake_download)

    seen = []
    res = miner.install(progress=lambda p: seen.append(p))
    assert res["ok"] is True
    assert miner.is_installed() is True
    assert miner.binary_path().read_bytes() == payload
    assert any(p.get("percent") == 100 for p in seen)


def test_install_extracts_a_zip_bundle(tmp_path, monkeypatch):
    """The NVIDIA artifact is a zip (xmrig.exe + xmrig-cuda.dll + CUDA dlls);
    install() must extract the whole bundle, not place a single file."""
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xmrig.exe", b"fake-xmrig")
        zf.writestr("xmrig-cuda.dll", b"fake-cuda-plugin")
        zf.writestr("cudart64_12.dll", b"fake-cuda-runtime")
    blob = buf.getvalue()
    sha = hashlib.sha256(blob).hexdigest()
    artifact = {"name": "xmrig-win", "url": "/omni/dist/blob/xmrig", "bytes": len(blob), "sha256": sha, "kind": "tool"}
    monkeypatch.setattr(miner.bootstrap, "read_manifest",
                        lambda *a, **k: {"ok": True, "artifacts": [artifact]})
    monkeypatch.setattr(miner.bootstrap, "dist_base", lambda: "http://x")

    def fake_download(base, art, tmp, progress=None):
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(blob)
    monkeypatch.setattr(miner.bootstrap, "download_blob", fake_download)

    res = miner.install()
    assert res["ok"] is True and res.get("bundle") is True
    assert miner.is_installed() is True
    assert miner.binary_path().read_bytes() == b"fake-xmrig"
    assert (miner.miner_dir() / "xmrig-cuda.dll").read_bytes() == b"fake-cuda-plugin"
    assert (miner.miner_dir() / "cudart64_12.dll").exists()


def test_install_is_idempotent_when_already_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    miner.binary_path().parent.mkdir(parents=True, exist_ok=True)
    miner.binary_path().write_bytes(b"already-here")

    def boom(*a, **k):
        raise AssertionError("install() re-downloaded an already-installed binary")
    monkeypatch.setattr(miner.bootstrap, "read_manifest", boom)
    monkeypatch.setattr(miner.bootstrap, "download_blob", boom)

    res = miner.install()
    assert res == {"ok": True, "path": str(miner.binary_path()), "cached": True}
    assert miner.binary_path().read_bytes() == b"already-here"
