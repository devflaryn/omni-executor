"""On-demand XMRig download + install.

Miners trip Windows Defender, so the binary is NOT in the installer. It is
fetched only after the user enrolls, from the `tools` dist channel, and
recorded as kind:"tool" so it never gates first-boot readiness. Reuses
bootstrap's sha256-verified, resumable download and atomic swap so a running
miner is never overwritten half-way.
"""
import shutil
import sys
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
    base = bootstrap.dist_base()
    manifest = bootstrap.read_manifest(base, channel=MINER_CHANNEL)
    art = _find_artifact(manifest)

    staging = miner_dir() / f"_part_{(art.get('sha256') or 'nohash')[:12]}"
    bootstrap.download_blob(base, art, staging, progress=progress)

    # Atomic-ish swap: write to <exe>.new, then replace, so a running miner is
    # not clobbered mid-write (mirrors bootstrap._install_qemu_portable).
    exe = binary_path()
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
