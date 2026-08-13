"""The account layer: identity on disk, and the local<->cloud merge rules.

The merge is the part worth testing hard. Getting it wrong is not a visible
crash — it is a stale cookie that fails minutes later inside a VM, or two
stores that push the same value at each other forever.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import accountsync  # noqa: E402
import cloud  # noqa: E402


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """Point cloud.py's per-user config dir at a temp dir on every platform."""
    home = tmp_path / "cfg"
    home.mkdir()
    monkeypatch.setenv("APPDATA", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_device_identity_is_stable_and_survives_sign_out(config_home):
    first = cloud.device()
    assert first["deviceId"] and first["deviceName"]
    assert cloud.device()["deviceId"] == first["deviceId"]

    cloud._save_auth({"token": "t", "email": "a@b.c"})
    assert cloud.signed_in()
    cloud.sign_out()
    assert not cloud.signed_in()
    # A device id that changed on sign-out would orphan every running lease
    # this machine holds — the server would stop recognising them as ours.
    assert cloud.device()["deviceId"] == first["deviceId"]


def test_device_name_is_renameable(config_home):
    cloud.set_device_name("Mac mini")
    assert cloud.device()["deviceName"] == "Mac mini"
    # Blank falls back to the machine's own name rather than storing "".
    cloud.set_device_name("   ")
    assert cloud.device()["deviceName"]


def test_auth_headers_carry_the_device_and_a_user_agent(config_home):
    cloud._save_auth({"token": "tok-123"})
    h = cloud._headers()
    assert h["Authorization"] == "Bearer tok-123"
    assert h["X-Omni-Device-Id"] == cloud.device()["deviceId"]
    # Arcjet rejects a request with no user-agent outright, and urllib sends
    # none by default — this header is load-bearing, not cosmetic.
    assert h["User-Agent"].startswith("OmniExecutor")


def test_api_base_prefers_env_then_settings(config_home, monkeypatch):
    monkeypatch.delenv("OMNI_API_BASE", raising=False)
    assert cloud.api_base() == cloud.DEFAULT_API_BASE
    assert cloud.api_base({"apiBase": "http://192.168.0.9:5500/"}) == "http://192.168.0.9:5500"
    monkeypatch.setenv("OMNI_API_BASE", "http://envwins")
    assert cloud.api_base({"apiBase": "http://settings"}) == "http://envwins"


# ---------------------------------------------------------------- the merge

class FakeCloud:
    """Stand-in for the server side of accountsync, holding plaintext cookies
    and the sha256 the real backend publishes."""

    def __init__(self, rows=None, user_id="user-1"):
        self.rows = rows or {}          # username -> {cookie, ts}
        self.pushed = []
        self.user_id = user_id

    def auth(self):
        """accountsync asks the cloud module who is signed in, to decide which
        local accounts this user may push."""
        return {"token": "t", "userId": self.user_id}

    def _hash(self, cookie):
        import hashlib
        return hashlib.sha256(cookie.encode()).hexdigest()

    def list_accounts(self):
        return [
            {"username": n, "hasCookie": bool(r["cookie"]),
             "cookieHash": self._hash(r["cookie"]) if r["cookie"] else None,
             "cookieUpdatedAt": r["ts"], "userId": r.get("userId"),
             "presence": {"state": "stopped", "label": "Stopped"}}
            for n, r in self.rows.items()
        ]

    def get_cookie(self, username):
        return self.rows[username]["cookie"]

    def push_accounts(self, records):
        self.pushed.extend(records)
        for rec in records:
            self.rows[rec["username"]] = {"cookie": rec["cookie"],
                                          "ts": "2030-01-01T00:00:00.000Z",
                                          "userId": rec.get("userId")}
        return self.list_accounts()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real omnidroid account store in a temp dir."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    return tmp_path


def _write_local(name, cookie, user_id=1):
    accountsync.write_local(name, cookie, user_id=user_id)


def _local_cookies():
    return {n: r["cookie"] for n, r in accountsync.read_local().items()}


def test_local_only_accounts_are_uploaded(store, monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(accountsync, "cloud", fake)
    _write_local("farm1", "cookie-1")

    res = accountsync.sync()

    assert res["pushed"] == ["farm1"]
    assert fake.rows["farm1"]["cookie"] == "cookie-1"


def test_cloud_only_accounts_are_downloaded(store, monkeypatch):
    fake = FakeCloud({"farm2": {"cookie": "cookie-2", "ts": "2029-01-01T00:00:00.000Z"}})
    monkeypatch.setattr(accountsync, "cloud", fake)

    res = accountsync.sync()

    assert res["pulled"] == ["farm2"]
    assert _local_cookies()["farm2"] == "cookie-2"


def test_sync_converges_and_does_not_ping_pong(store, monkeypatch):
    """The regression this exists for: the sealed blob differs on every write,
    so a naive comparison re-uploads and re-downloads the same cookie forever.
    Two syncs in a row must move nothing the second time."""
    fake = FakeCloud({"farm3": {"cookie": "cookie-3", "ts": "2029-01-01T00:00:00.000Z"}})
    monkeypatch.setattr(accountsync, "cloud", fake)

    first = accountsync.sync()
    assert first["pulled"] == ["farm3"]

    second = accountsync.sync()
    assert second["pushed"] == []
    assert second["pulled"] == []


def test_a_newer_local_cookie_wins_over_an_older_cloud_one(store, monkeypatch):
    fake = FakeCloud({"farm4": {"cookie": "old-cookie", "ts": "2000-01-01T00:00:00.000Z"}})
    monkeypatch.setattr(accountsync, "cloud", fake)
    _write_local("farm4", "fresh-cookie")     # save_account stamps `saved` = now

    res = accountsync.sync()

    assert res["pushed"] == ["farm4"]
    assert fake.rows["farm4"]["cookie"] == "fresh-cookie"
    assert _local_cookies()["farm4"] == "fresh-cookie"


def test_sync_never_deletes(store, monkeypatch):
    """An account missing from the other side is copied, never removed —
    otherwise signing in on a fresh machine would wipe the cloud."""
    fake = FakeCloud({"only-remote": {"cookie": "r", "ts": "2029-01-01T00:00:00.000Z"}})
    monkeypatch.setattr(accountsync, "cloud", fake)
    _write_local("only-local", "l")

    accountsync.sync()

    assert set(_local_cookies()) == {"only-local", "only-remote"}
    assert set(fake.rows) == {"only-local", "only-remote"}


def test_cookies_are_never_written_to_the_store_listing(store, monkeypatch):
    """read_local() is allowed to see cookies; omnidroid's own public listing
    is not. Guards against a refactor that starts leaking them."""
    _write_local("farm5", "secret-cookie")
    mod = accountsync._omnidroid_accounts()
    listing = mod.list_accounts(str(store))
    assert listing and all("cookie" not in row for row in listing)
    assert json.dumps(listing).find("secret-cookie") == -1


# ------------------------------------------------- one machine, two Omni users

def _as_user(monkeypatch, user_id):
    """Pretend a particular Omni user is signed in on this machine."""
    accountsync.cloud.user_id = user_id


def test_a_second_user_does_not_inherit_the_first_users_accounts(store, monkeypatch):
    """THE LEAK THIS EXISTS TO STOP.

    Measured during the multi-device acceptance run: user A signed in on the
    Mac and pulled their accounts down; user B then signed in on the SAME
    machine and their first sync uploaded every one of A's cookies into B's
    cloud account. The local store is per-machine; ownership is per-user.
    """
    fake_a = FakeCloud({"a-farm": {"cookie": "cookie-a", "ts": "2029-01-01T00:00:00.000Z"}})
    monkeypatch.setattr(accountsync, "cloud", fake_a)
    _as_user(monkeypatch, "user-A")
    accountsync.sync()
    assert "a-farm" in _local_cookies()          # A pulled it down

    fake_b = FakeCloud()                          # B's cloud store is empty
    monkeypatch.setattr(accountsync, "cloud", fake_b)
    _as_user(monkeypatch, "user-B")
    res = accountsync.sync()

    assert res["pushed"] == [], "B must not upload A's accounts"
    assert res["foreign"] == ["a-farm"]
    assert fake_b.rows == {}, "A's cookie must not appear in B's cloud store"


def test_an_unclaimed_local_account_belongs_to_whoever_syncs_it(store, monkeypatch):
    """An account added on this machine and never synced has no owner yet —
    the person sitting at the machine adds it, so it is theirs."""
    fake = FakeCloud()
    monkeypatch.setattr(accountsync, "cloud", fake)
    _as_user(monkeypatch, "user-A")
    _write_local("fresh", "cookie-fresh")

    res = accountsync.sync()

    assert res["pushed"] == ["fresh"]
    assert accountsync.owned_here("fresh", "user-A") is True
    assert accountsync.owned_here("fresh", "user-B") is False


def test_signing_out_drops_the_accounts_that_user_pulled_down(store, monkeypatch):
    fake = FakeCloud({"a-farm": {"cookie": "cookie-a", "ts": "2029-01-01T00:00:00.000Z"}})
    monkeypatch.setattr(accountsync, "cloud", fake)
    _as_user(monkeypatch, "user-A")
    _write_local("local-only", "cookie-local")     # never claimed by anyone
    accountsync.sync()
    assert {"a-farm", "local-only"} <= set(_local_cookies())

    removed = accountsync.forget_user("user-A")

    # Everything A synced is gone from this machine; it is still in the cloud
    # and comes back on their next sign-in.
    assert "a-farm" in removed
    assert "a-farm" not in _local_cookies()
    assert fake.rows["a-farm"]["cookie"] == "cookie-a"
    # `local-only` was pushed during that sync, so it is A's too.
    assert "local-only" in removed


def test_forgetting_one_user_leaves_another_users_accounts_alone(store, monkeypatch):
    fake_a = FakeCloud({"a-farm": {"cookie": "ca", "ts": "2029-01-01T00:00:00.000Z"}})
    monkeypatch.setattr(accountsync, "cloud", fake_a)
    _as_user(monkeypatch, "user-A")
    accountsync.sync()

    fake_b = FakeCloud({"b-farm": {"cookie": "cb", "ts": "2029-01-01T00:00:00.000Z"}})
    monkeypatch.setattr(accountsync, "cloud", fake_b)
    _as_user(monkeypatch, "user-B")
    accountsync.sync()

    accountsync.forget_user("user-B")

    assert "a-farm" in _local_cookies()
    assert "b-farm" not in _local_cookies()
