"""The update channels: who may follow what, what gets asked of the server, and
that moving between channels leaves the install in a true state."""
import json
import urllib.request
from types import SimpleNamespace

import pytest

import bootstrap
import cloud
import main
import updates


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SETTINGS_FILE", tmp_path / "settings.json")
    login = {}
    monkeypatch.setattr(cloud, "auth", lambda: login)
    a = main.Api()
    a.login = login
    a._push = lambda *_: None
    monkeypatch.setattr(a, "_update_tick", lambda *_, **__: None)
    monkeypatch.setattr(updates, "check", lambda *_, **__: {"ok": True})
    monkeypatch.setattr(updates, "staged_info", lambda: None)
    monkeypatch.setattr(updates, "discard_staged", lambda: None)
    yield a
    bootstrap.use_channel_source(lambda: bootstrap.NATIVE_CHANNEL, lambda: None)


def test_a_new_install_follows_prod(api):
    assert api._channel_state() == {"picked": "prod", "allowed": ["prod", "beta"], "active": "prod"}
    assert bootstrap.active_channel() == "native"


def test_anyone_can_choose_beta(api):
    report = api.update_set_channel("beta")
    assert report["channel"]["active"] == "beta"
    assert bootstrap.active_channel() == "beta"


def test_a_normal_account_cannot_choose_dev(api):
    report = api.update_set_channel("dev")
    assert report == {"ok": False, "error": "channel_forbidden",
                      "message": "This account can't use that channel."}
    assert api.get_settings().get("updateChannel") is None
    assert bootstrap.active_channel() == "native"


def test_a_dev_account_can_choose_dev(api):
    api.login["updateChannels"] = ["prod", "beta", "dev"]
    assert api.update_set_channel("dev")["channel"]["active"] == "dev"
    assert bootstrap.active_channel() == "dev"


def test_dev_falls_back_to_prod_without_a_dev_account_and_returns_with_one(api):
    api.save_settings({"updateChannel": "dev"})
    assert api._channel_state()["active"] == "prod"
    assert api._channel_state()["picked"] == "dev"
    api.login["updateChannels"] = ["prod", "beta", "dev"]
    assert api._channel_state()["active"] == "dev"


def test_switching_is_refused_mid_download(api):
    api._updating = True
    assert api.update_set_channel("beta")["error"] == "already_running"


def test_switching_throws_away_a_build_staged_for_the_old_channel(api, monkeypatch):
    gone = []
    monkeypatch.setattr(updates, "discard_staged", lambda: gone.append(True))
    api.update_set_channel("beta")
    assert gone == [True]


def test_every_report_carries_what_the_ui_reads(api, monkeypatch):
    monkeypatch.setattr(updates, "staged_info", lambda: {"version": "9.9.9"})
    report = api.update_check()
    assert report["staged"] == {"version": "9.9.9"}
    assert report["autoUpdate"] is True
    assert report["channel"]["active"] == "prod"


class FakeResponse:
    status = 200
    def __init__(self, body): self.body = body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *_): return False


def _capture_manifest(monkeypatch):
    seen = []
    def urlopen(req, timeout=None):
        seen.append(req)
        return FakeResponse(json.dumps({"ok": True}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_the_token_goes_only_to_dev_requests(monkeypatch):
    seen = _capture_manifest(monkeypatch)
    bootstrap.use_channel_source(lambda: "beta", lambda: "secret")
    try:
        bootstrap.read_manifest("http://x", "win")
        bootstrap.read_manifest("http://x", "win", channel="tools")
        assert "channel=beta" in seen[0].full_url and seen[0].get_header("Authorization") is None
        assert "channel=tools" in seen[1].full_url and seen[1].get_header("Authorization") is None
        bootstrap.read_manifest("http://x", "win", channel="dev")
        assert seen[2].get_header("Authorization") == "Bearer secret"
    finally:
        bootstrap.use_channel_source(lambda: bootstrap.NATIVE_CHANNEL, lambda: None)


def _fake_runtime(tmp_path, monkeypatch, manifest):
    monkeypatch.setattr(bootstrap, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *_, **__: manifest)
    monkeypatch.setattr(bootstrap, "_precheck_space", lambda *_: None)
    monkeypatch.setattr(bootstrap, "download_blob", lambda *_, **__: None)
    monkeypatch.setattr(bootstrap, "place_artifact", lambda *_, **__: None)


def _engine(name, sha):
    return {"name": name, "sha256": sha, "bytes": 1, "url": "/x", "dest": "omnidroid", "version": sha}


def test_switching_back_really_downloads_the_old_channels_engine(tmp_path, monkeypatch):
    """Beta's engine was written over prod's files. If prod's receipt survived,
    going back to prod would see "already installed" and leave beta's engine."""
    prod = {"artifacts": [_engine("omnidroid-win", "p")], "app": {}}
    beta = {"artifacts": [_engine("omnidroid-win-beta", "b")], "app": {}}
    _fake_runtime(tmp_path, monkeypatch, prod)
    assert bootstrap.ensure_runtime("http://x")["changed"] == ["omnidroid-win"]
    _fake_runtime(tmp_path, monkeypatch, beta)
    assert bootstrap.ensure_runtime("http://x")["changed"] == ["omnidroid-win-beta"]
    assert list(bootstrap.installed_state(tmp_path)["artifacts"]) == ["omnidroid-win-beta"]
    _fake_runtime(tmp_path, monkeypatch, prod)
    assert bootstrap.ensure_runtime("http://x")["changed"] == ["omnidroid-win"]


def test_an_unrelated_receipt_is_left_alone(tmp_path, monkeypatch):
    both = {"artifacts": [_engine("omnidroid-win", "p"), _engine("omniexec-apk-win", "a")], "app": {}}
    _fake_runtime(tmp_path, monkeypatch, both)
    bootstrap.ensure_runtime("http://x")
    assert sorted(bootstrap.installed_state(tmp_path)["artifacts"]) == ["omnidroid-win", "omniexec-apk-win"]


def _app_manifest(version):
    return {"artifacts": [{"name": "app-win-native", "kind": "app", "version": version, "bytes": 5}], "app": {}}


def test_an_older_build_on_the_channel_is_offered_never_taken(monkeypatch):
    monkeypatch.setattr(updates, "APP_VERSION", "1.3.0")
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *_, **__: _app_manifest("1.1.0"))
    monkeypatch.setattr(bootstrap, "installed_state", lambda *_: {"artifacts": {}})
    app = updates.check("http://x")["app"]
    assert app["update"] is False and app["downgrade"] is True
    with pytest.raises(updates.UpdateError):
        updates.stage_app("http://x")


def test_the_same_version_is_neither_update_nor_downgrade(monkeypatch):
    monkeypatch.setattr(updates, "APP_VERSION", "1.3.0")
    monkeypatch.setattr(bootstrap, "read_manifest", lambda *_, **__: _app_manifest("1.3.0"))
    monkeypatch.setattr(bootstrap, "installed_state", lambda *_: {"artifacts": {}})
    app = updates.check("http://x")["app"]
    assert app["update"] is False and app["downgrade"] is False


def test_a_chosen_downgrade_stays_staged_until_it_has_been_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "staging_root", lambda: tmp_path)
    build = tmp_path / "build"
    build.mkdir()
    (tmp_path / "staged.json").write_text(json.dumps(
        {"version": "1.1.0", "build": str(build), "downgrade": True}), encoding="utf-8")
    monkeypatch.setattr(updates, "APP_VERSION", "1.3.0")
    assert updates.staged_info()["version"] == "1.1.0"
    monkeypatch.setattr(updates, "APP_VERSION", "1.1.0")
    assert updates.staged_info() is None
