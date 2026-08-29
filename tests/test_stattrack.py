"""Stat Track's local half: the autoexec file that arms the tracker.

The server owns the collector and the paywall (omni-backend's stats tests cover
those). What this side owns is one file: writing it arms Stat Track, deleting
it disarms it, and the file's contents have to point at the server this app is
actually talking to. All three are checked here, because the failure mode of
getting the third one wrong is a toggle that reads "on" while reporting into
nowhere.
"""
import main


def _api(tmp_path, monkeypatch, base="http://example.test"):
    api = main.Api()
    monkeypatch.setattr(main.Api, "autoexec_dir", lambda self: str(tmp_path))
    monkeypatch.setattr(main.Api, "_exec_base", lambda self: base)
    return api


def test_off_by_default(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    status = api.stattrack_status()
    assert status["ok"] and status["enabled"] is False
    assert status["installed"] is False


def test_enabling_writes_a_loader_pointing_at_this_server(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, base="http://10.0.0.5:5500")
    res = api.stattrack_set(True)
    assert res["ok"] and res["enabled"] is True

    written = tmp_path / main.Api.STATTRACK_FILE
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    # The whole point of the indirection: the file is a pointer, the collector
    # lives on the server, and the URL is the one this app talks to.
    assert "loadstring" in body
    assert "http://10.0.0.5:5500/omni/exec/stattrack.lua" in body
    # It must be obvious to whoever opens the folder that they can delete it.
    assert "safe to delete" in body.lower()


def test_disabling_removes_the_file(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    api.stattrack_set(True)
    res = api.stattrack_set(False)
    assert res["ok"] and res["enabled"] is False
    assert not (tmp_path / main.Api.STATTRACK_FILE).exists()


def test_disabling_twice_is_not_an_error(tmp_path, monkeypatch):
    # The toggle can be driven from more than one place; a second "off" must
    # not report a failure just because the first one already did the work.
    api = _api(tmp_path, monkeypatch)
    assert api.stattrack_set(False)["ok"] is True
    assert api.stattrack_set(False)["ok"] is True


def test_a_file_left_by_an_older_server_reads_as_stale_not_on(tmp_path, monkeypatch):
    # An install that used to point at a different host would otherwise look
    # armed while reporting nowhere — the one failure the user cannot see.
    api = _api(tmp_path, monkeypatch, base="http://old.test")
    api.stattrack_set(True)

    moved = _api(tmp_path, monkeypatch, base="http://new.test")
    status = moved.stattrack_status()
    assert status["installed"] is True
    assert status["stale"] is True
    assert status["enabled"] is False

    # Re-arming repairs it in place rather than needing a delete first.
    assert moved.stattrack_set(True)["enabled"] is True
    assert moved.stattrack_status()["stale"] is False


def test_stats_without_a_session_says_signed_out(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(main.cloud, "signed_in", lambda: False)
    assert api.stattrack_stats()["error"] == "signed_out"


def test_a_free_account_gets_the_locked_state_not_an_error(tmp_path, monkeypatch):
    # 402 is the paywall's deliberate signal (see subscription.middleware.js);
    # turning it into a generic failure would show "something went wrong" to a
    # user whose only problem is that they have not redeemed a key.
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(main.cloud, "signed_in", lambda: True)

    def refuse():
        raise main.cloud.CloudError("Redeem a key to continue.", status=402,
                                    error="subscription_inactive")
    monkeypatch.setattr(main.cloud, "list_stats", refuse)

    res = api.stattrack_stats()
    assert res["ok"] is False
    assert res["error"] == "subscription_inactive"
    assert "key" in res["message"]


def test_stats_pass_the_server_rows_through(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(main.cloud, "signed_in", lambda: True)
    monkeypatch.setattr(main.cloud, "list_stats", lambda: {
        "accounts": [{"username": "farm1", "tracking": True, "metrics": []}],
        "summary": {"accounts": 1, "online": 1, "tracking": 1},
    })
    res = api.stattrack_stats()
    assert res["ok"] is True
    assert res["accounts"][0]["username"] == "farm1"
    assert res["summary"]["tracking"] == 1
