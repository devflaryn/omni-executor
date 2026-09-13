"""On-demand XMRig download + install.

Miners trip Windows Defender, so the binary is NOT in the installer. It is
fetched only after the user enrolls, from the `tools` dist channel, and
recorded as kind:"tool" so it never gates first-boot readiness. Reuses
bootstrap's sha256-verified, resumable download and atomic swap so a running
miner is never overwritten half-way.
"""
import shutil
import sys
import zipfile
from pathlib import Path

import bootstrap

MINER_CHANNEL = "tools"
_ARTIFACT_NAME = "xmrig-win" if sys.platform == "win32" else "xmrig-linux"


def miner_dir() -> Path:
    d = bootstrap.runtime_dir() / "miner"
    d.mkdir(parents=True, exist_ok=True)
    return d


def binary_path() -> Path:
    exe = "xmrig.exe" if sys.platform == "win32" else "xmrig"
    return miner_dir() / exe


def is_installed() -> bool:
    return binary_path().exists()


def _find_artifact(manifest: dict) -> dict:
    for art in manifest.get("artifacts", []):
        if art.get("name") == _ARTIFACT_NAME:
            return art
    raise bootstrap.BootstrapError(f"{_ARTIFACT_NAME} not in the {MINER_CHANNEL} manifest")


def install(progress=None) -> dict:
    """Download + place the miner. Idempotent: a good existing binary is kept."""
    if is_installed():
        return {"ok": True, "path": str(binary_path()), "cached": True}
    base = bootstrap.dist_base()
    manifest = bootstrap.read_manifest(base, channel=MINER_CHANNEL)
    art = _find_artifact(manifest)

    staging = miner_dir() / f"_part_{(art.get('sha256') or 'nohash')[:12]}"
    bootstrap.download_blob(base, art, staging, progress=progress)

    # The NVIDIA-capable artifact is a ZIP bundle (xmrig.exe + xmrig-cuda.dll +
    # CUDA runtime dlls) — a plain single exe can't mine KawPow on NVIDIA. If the
    # downloaded blob is a zip, extract the whole bundle into the miner dir;
    # otherwise treat it as a bare binary (back-compat). install() only fully
    # runs when nothing is installed (is_installed() early-returns above), so
    # nothing is running to clobber.
    exe = binary_path()
    if zipfile.is_zipfile(staging):
        with zipfile.ZipFile(staging) as zf:
            dest = miner_dir()
            for member in zf.namelist():
                # path-traversal guard (the artifact is ours, but be safe)
                target = (dest / member).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise bootstrap.BootstrapError(f"{art['name']}: unsafe path in bundle: {member}")
            zf.extractall(dest)
        staging.unlink()
        if not is_installed():
            raise bootstrap.BootstrapError(
                f"{art['name']}: bundle did not contain {exe.name}")
        if sys.platform != "win32":
            exe.chmod(0o755)
        return {"ok": True, "path": str(exe), "name": art["name"], "bundle": True}

    # Single-binary artifact: atomic-ish swap (write to <exe>.new, then replace).
    new = exe.with_suffix(exe.suffix + ".new")
    if new.exists():
        new.unlink()
    shutil.move(str(staging), str(new))
    if sys.platform != "win32":
        new.chmod(0o755)
    if exe.exists():
        old = exe.with_suffix(exe.suffix + ".old")
        if old.exists():
            old.unlink()
        exe.replace(old)
    new.replace(exe)
    return {"ok": True, "path": str(exe), "name": art["name"]}
