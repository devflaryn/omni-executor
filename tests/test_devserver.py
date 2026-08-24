"""Dev mode redirects OUR server and nothing else, and is not in a release build.

Two failure modes are worth a test each, and they point in opposite directions:

  * Under-reaching — one of the three bases (api / dist / exec) does not follow
    the switch, so half the app talks to the local backend and half talks to
    production. That is worse than no dev mode: the app signs in against
    localhost and then polls the real exec bridge for a session it never made.
  * Over-reaching — the redirect swallows a URL that was never ours. Google's
    platform-tools zip is fetched by absolute URL from the same module as the
    dist blobs, and a dev machine that cannot install adb would look like a
    bootstrap bug, not a dev-mode bug.

Plus the one that costs a customer: `devserver` reaching a shipped bundle. The
specs are the only enforcement (every call site falls back silently), so the
exclusion is asserted against the spec text.
"""

import io
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402
import cloud  # noqa: E402
import devserver  # noqa: E402
import main  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
SPECS = [PROJECT / "OmniExecutor-win.spec", PROJECT / "OmniExecutor.spec"]


@pytest.fixture
def dev_off(monkeypatch, tmp_path):
    """No env switch and no dev.json — the production path."""
    monkeypatch.delenv(devserver.DEV_ENV, raising=False)
    monkeypatch.setattr(devserver, "_config_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def dev_on(dev_off, monkeypatch):
    monkeypatch.setenv(devserver.DEV_ENV, "1")
    return dev_off


# ------------------------------------------------------------ the switch

def test_off_by_default(dev_off):
    assert devserver.dev_server() is None
    assert devserver.enabled() is False


def test_bare_flag_means_the_local_backend(dev_on):
    """The common case is one env var, so `1` has to resolve to omni-backend's
    dev port without anyone typing a URL."""
    assert devserver.dev_server() == "http://127.0.0.1:5500"


@pytest.mark.parametrize("value,expected", [
    ("http://127.0.0.1:5500", "http://127.0.0.1:5500"),
    ("127.0.0.1:5500", "http://127.0.0.1:5500"),   # scheme implied
    ("http://10.0.0.4:5500/", "http://10.0.0.4:5500"),  # trailing slash dropped
    ("https://dev.example:8443", "https://dev.example:8443"),
])
def test_explicit_targets(dev_off, monkeypatch, value, expected):
    monkeypatch.setenv(devserver.DEV_ENV, value)
    assert devserver.dev_server() == expected


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "  "])
def test_falsey_env_is_off(dev_off, monkeypatch, value):
    monkeypatch.setenv(devserver.DEV_ENV, value)
    assert devserver.dev_server() is None


def test_dev_json_works_without_an_env_var(dev_off):
    (dev_off / devserver.DEV_FILE).write_text(
        '{"devServer": "http://127.0.0.1:5500"}', encoding="utf-8")
    assert devserver.dev_server() == "http://127.0.0.1:5500"


def test_env_off_beats_the_file(dev_off, monkeypatch):
    """Turning dev mode off for one run must not mean deleting a config file."""
    (dev_off / devserver.DEV_FILE).write_text(
        '{"devServer": "http://127.0.0.1:5500"}', encoding="utf-8")
    monkeypatch.setenv(devserver.DEV_ENV, "0")
    assert devserver.dev_server() is None


def test_dev_mode_false_in_the_file_is_off(dev_off):
    (dev_off / devserver.DEV_FILE).write_text(
        '{"devMode": false, "devServer": "http://127.0.0.1:5500"}',
        encoding="utf-8")
    assert devserver.dev_server() is None


def test_unreadable_dev_json_is_off_not_a_crash(dev_off):
    (dev_off / devserver.DEV_FILE).write_text("{not json", encoding="utf-8")
    assert devserver.dev_server() is None


# ------------------------------------------------------------- the rewrite

def test_rewrites_our_origin_and_keeps_the_path(dev_on):
    assert devserver.redirect("http://72.62.59.232") == "http://127.0.0.1:5500"
    assert devserver.redirect("http://72.62.59.232/omni/dist/manifest?os=win") \
        == "http://127.0.0.1:5500/omni/dist/manifest?os=win"


@pytest.mark.parametrize("url", [
    "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "https://users.roblox.com/v1/users/authenticated",
    "http://192.168.0.9:5500/api/v1/auth/me",
])
def test_leaves_every_other_host_alone(dev_on, url):
    assert devserver.redirect(url) == url


def test_matches_on_host_not_on_a_string_prefix(dev_on):
    """https and a port are the same server. A `startswith` check would miss
    both and leave those calls pointed at production."""
    assert devserver.redirect("https://72.62.59.232/omni/exec/claim") \
        == "http://127.0.0.1:5500/omni/exec/claim"
    assert devserver.redirect("http://72.62.59.232:80/gist") \
        == "http://127.0.0.1:5500/gist"


def test_off_is_a_passthrough(dev_off):
    assert devserver.redirect("http://72.62.59.232") == "http://72.62.59.232"


# --------------------------------------------------- every base follows it

def test_api_base_follows(dev_on):
    assert cloud.api_base() == "http://127.0.0.1:5500"


def test_dist_base_follows(dev_on):
    assert bootstrap.dist_base() == "http://127.0.0.1:5500"
    # The blob URLs are built from it, so they come along for free — asserted
    # because that is the update path the whole feature exists to exercise.
    assert bootstrap.qemu_win_url().startswith("http://127.0.0.1:5500/")


def test_exec_base_follows(dev_on, monkeypatch, tmp_path):
    api = main.Api.__new__(main.Api)
    monkeypatch.setattr(main.Api, "get_settings", lambda self: {})
    assert api._exec_base() == "http://127.0.0.1:5500"


def test_an_explicit_production_override_is_still_redirected(dev_on, monkeypatch):
    """OMNI_API_BASE naming the production server is a request for THAT server,
    which in dev mode is the local one. The alternative — an env var that
    silently defeats dev mode — is a debugging trap."""
    monkeypatch.setenv("OMNI_API_BASE", "http://72.62.59.232")
    assert cloud.api_base() == "http://127.0.0.1:5500"


def test_a_third_party_override_is_not_redirected(dev_on, monkeypatch):
    monkeypatch.setenv("OMNI_API_BASE", "http://192.168.0.9:5500")
    assert cloud.api_base() == "http://192.168.0.9:5500"


def test_production_bases_are_unchanged_when_dev_is_off(dev_off, monkeypatch):
    monkeypatch.delenv("OMNI_API_BASE", raising=False)
    monkeypatch.delenv("OMNI_EXEC_BASE", raising=False)
    assert cloud.api_base() == "http://72.62.59.232"
    assert bootstrap.dist_base() == "http://72.62.59.232"


# ------------------------------------------------- and the engine follows

def _engine_env(monkeypatch, dev_dir):
    """Run one engine call and hand back the env its subprocess was given."""
    seen = {}

    class FakeProc:
        def __init__(self, *a, **kw):
            seen["env"] = kw.get("env")
            self.stdout, self.stderr = io.StringIO('{"ok": true}'), io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

        returncode = 0

    monkeypatch.setattr(main, "engine_prefix", lambda: ["omnidroid.exe"])
    monkeypatch.setattr(main.subprocess, "Popen", FakeProc)
    main.run_engine(["list"])
    return seen["env"]


def test_dev_json_is_exported_to_the_engine(dev_off, monkeypatch):
    """The guest's redirect is omnidroid's job and omnidroid reads the ENV. A
    file-only dev mode would otherwise leave the app on localhost while its VMs
    kept calling production — the one half-applied state to rule out."""
    (dev_off / devserver.DEV_FILE).write_text(
        '{"devServer": "http://127.0.0.1:5500"}', encoding="utf-8")
    env = _engine_env(monkeypatch, dev_off)
    assert env is not None and env[devserver.DEV_ENV] == "http://127.0.0.1:5500"


def test_nothing_is_exported_when_dev_is_off(dev_off, monkeypatch):
    """Production must not gain an env var, and must keep inheriting os.environ
    rather than being handed a copy."""
    assert _engine_env(monkeypatch, dev_off) is None


# ------------------------------------------- and it never reaches a customer

@pytest.mark.parametrize("spec", SPECS, ids=lambda p: p.name)
def test_specs_exclude_dev_mode_by_default(spec):
    """The import guards make a missing module invisible, so the bundle is the
    only place this can be enforced — and a spec that quietly stopped excluding
    it would ship a customer an app that a dropped-in dev.json can redirect."""
    text = spec.read_text(encoding="utf-8")
    assert 'DEV_MODULES = ["devserver", "omnidroid.devserver"]' in text, (
        f"{spec.name} no longer declares the dev modules")
    assert "excludes=dev_excludes" in text, (
        f"{spec.name} does not pass the dev-mode exclusion to Analysis()")
    assert re.search(r'OMNI_DEV_BUILD', text), (
        f"{spec.name} lost the opt-in that builds a dev bundle")
    # The filter matters as much as the exclude: collect_submodules("omnidroid")
    # names omnidroid.devserver as a hiddenimport, and a name that is both
    # hidden-imported and excluded is PyInstaller's to arbitrate.
    assert "h not in DEV_MODULES" in text, (
        f"{spec.name} excludes the dev modules but still hidden-imports them")


def test_the_setup_stub_excludes_dev_mode_unconditionally():
    """The stub is the file on the download page. It has no dev use and gets no
    OMNI_DEV_BUILD escape."""
    text = (PROJECT / "OmniExecutorSetup.spec").read_text(encoding="utf-8")
    excludes = re.search(r"excludes=\[(.*?)\]", text, re.S).group(1)
    assert '"devserver"' in excludes
    # A literal list, not a computed one: no env var can empty it. (The name
    # appears in the surrounding comment explaining why — that is prose, so the
    # assertion is on the mechanism.)
    assert 'os.environ.get("OMNI_DEV_BUILD"' not in text
    assert "dev_excludes" not in text


def test_call_sites_survive_the_module_being_absent(monkeypatch):
    """What a customer's app actually runs: no devserver module at all."""
    monkeypatch.setenv(devserver.DEV_ENV, "1")   # on, and still ignored
    monkeypatch.delenv("OMNI_API_BASE", raising=False)
    monkeypatch.delenv("OMNI_EXEC_BASE", raising=False)
    monkeypatch.setattr(cloud, "devserver", None)
    monkeypatch.setattr(bootstrap, "_devserver", None)
    monkeypatch.setattr(main, "_devserver", None)
    assert cloud.api_base() == "http://72.62.59.232"
    assert bootstrap.dist_base() == "http://72.62.59.232"
    api = main.Api.__new__(main.Api)
    monkeypatch.setattr(main.Api, "get_settings", lambda self: {})
    assert api._exec_base() == "http://72.62.59.232"
