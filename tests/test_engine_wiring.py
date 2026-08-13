import os, sys, json, types
from pathlib import Path
import pytest
import main, bootstrap


def test_engine_prefix_frozen_dispatch(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Omni Executor.app/Contents/MacOS/OmniExecutor", raising=False)
    monkeypatch.delenv("OMNIDROID_ENGINE", raising=False)
    assert main.engine_prefix() == [sys.executable, "--omnidroid"]


def test_engine_prefix_source_fallback_is_module(monkeypatch, tmp_path):
    # no env, not frozen, no adjacent binary, sibling omnidroid checkout present
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("OMNIDROID_ENGINE", raising=False)
    sib = tmp_path / "omnidroid" / "omnidroid"
    sib.mkdir(parents=True); (sib / "__main__.py").write_text("")
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path / "omni-executor")
    (tmp_path / "omni-executor").mkdir()
    prefix = main.engine_prefix()
    assert prefix[0] == sys.executable and "omnidroid" in prefix  # -m omnidroid form


def test_configure_engine_on_launch_sets_env_on_every_launch(tmp_path, monkeypatch):
    """Fix round 1: _configure_engine_on_launch() must set the same env
    configure_engine() sets, called unconditionally from main() on EVERY
    launch -- not just first-boot (see the gap this closes: bootstrap_start
    is only invoked once, on first install; a relaunch of an already
    -installed app never re-runs it, so without this helper being called
    from main() on every launch, later engine subprocesses would inherit no
    OMNIDROID_CONFIG_PATH/OMNI_DATA_DIR/OMNI_IMAGES_DIR at all)."""
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    for var in ("OMNIDROID_CONFIG_PATH", "OMNI_DATA_DIR", "OMNI_IMAGES_DIR"):
        monkeypatch.delenv(var, raising=False)

    main._configure_engine_on_launch()

    assert os.environ["OMNIDROID_CONFIG_PATH"] == str(tmp_path / "paths.json")
    assert os.environ["OMNI_DATA_DIR"] == str(tmp_path)
    assert os.environ["OMNI_IMAGES_DIR"] == str(tmp_path / "images")
    assert (tmp_path / "paths.json").exists()


def test_configure_engine_on_launch_no_images_does_not_raise(tmp_path, monkeypatch):
    """Genuinely fresh first boot: runtime dir exists but nothing has been
    downloaded yet (no images/arm at all). configure_engine() must not raise
    -- it just writes a base-less paths.json; bootstrap_start() corrects it
    once the download completes."""
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))

    main._configure_engine_on_launch()  # must not raise

    cfg = json.loads((tmp_path / "paths.json").read_text())
    assert cfg["bases"] == {}
    assert cfg["current_base"] is None


def test_configure_engine_sets_env_and_writes_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "images/arm").mkdir(parents=True)
    (tmp_path / "images/arm/base_arm_system_rooted.qcow2").write_bytes(b"x")
    (tmp_path / "images/arm/base_arm_data_offset_arceusremote.qcow2").write_bytes(b"x")
    info = bootstrap.configure_engine(tmp_path)
    assert os.environ["OMNI_DATA_DIR"] == str(tmp_path)
    assert os.environ["OMNI_IMAGES_DIR"] == str(tmp_path / "images")
    assert os.environ["OMNIDROID_CONFIG_PATH"] == str(tmp_path / "paths.json")
    cfg = json.loads((tmp_path / "paths.json").read_text())
    assert "qemu" in cfg and "images_dir" in cfg
    assert isinstance(info["qemu_ok"], bool)


# ------------------------------------------------------------------------
# Integration: does the paths.json configure_engine writes actually pass the
# REAL omnidroid loader's validation (bases.py base_missing_files, engine.py
# load_config/effective_base_tag)? Guarded — mirrors tests/test_engine_
# capabilities.py's skip pattern — because it needs the sibling omnidroid
# checkout importable.
#
# The fixture from test_configure_engine_sets_env_and_writes_paths above is
# deliberately partial (a system file + an offset file, no pristine /data),
# which is not something the real loader can ever validate as a bootable
# base (base_missing_files requires "data" to exist) — that gap is inherent
# to the fixture, not a bug in configure_engine, so it is not exercised
# against the real loader here. This test uses its own COMPLETE fixture
# (system + data + one offset, mirroring what ensure_runtime's manifest
# actually places under images/arm/ per tests/test_bootstrap.py) instead of
# invoking `python -m omnidroid bases --json` as a real subprocess: the
# loader's CONFIG_PATH is a fixed REPO/configs/paths.json with no env-var
# override (confirmed by reading omnidroid/config.py), so a real subprocess
# using the sibling checkout would read/write the DEVELOPER'S OWN real,
# daily-used configs/paths.json (its live arm base + offset registrations)
# instead of our fake rt/paths.json. Monkeypatching CONFIG_PATH in-process
# (in all three modules that bind it) exercises the identical validation
# logic without any risk to that file.
def test_configure_engine_config_matches_real_loader(tmp_path, monkeypatch):
    try:
        from omnidroid import config as omni_config
        from omnidroid import engine as omni_engine
        from omnidroid import bases as omni_bases
    except Exception:
        pytest.skip("omnidroid not importable in this environment")

    rt = tmp_path
    arm_dir = rt / "images" / "arm"
    arm_dir.mkdir(parents=True)
    (arm_dir / "base_arm_system_rooted.qcow2").write_bytes(b"sys")
    (arm_dir / "base_arm_data_rooted.qcow2").write_bytes(b"data")
    (arm_dir / "base_arm_data_offset_arceusremote.qcow2").write_bytes(b"off")

    info = bootstrap.configure_engine(rt)
    assert info["images_dir"] == str(rt / "images")
    assert os.environ["OMNIDROID_CONFIG_PATH"] == str(rt / "paths.json")

    fake_config_path = rt / "paths.json"
    # Redirect the loader's fixed config location at the file
    # configure_engine just wrote — never the real sibling checkout's own
    # configs/paths.json (see note above).
    monkeypatch.setattr(omni_config, "CONFIG_PATH", fake_config_path, raising=False)
    monkeypatch.setattr(omni_engine, "CONFIG_PATH", fake_config_path, raising=False)
    monkeypatch.setattr(omni_bases, "CONFIG_PATH", fake_config_path, raising=False)
    monkeypatch.setenv("OMNI_DATA_DIR", str(rt))
    monkeypatch.setenv("OMNI_IMAGES_DIR", str(rt / "images"))

    cfg = omni_engine.load_config()
    assert "arm" in cfg["bases"]
    assert cfg["_effective_base"] == "arm"
    images = Path(cfg["images_dir"])
    assert omni_bases.base_missing_files(images, cfg["bases"]["arm"]) == []
    assert "arceusremote" in (cfg["bases"]["arm"].get("offsets") or {})
