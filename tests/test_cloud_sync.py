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


# ------------------------------------------------- free sign-up and redeeming
#
# These pin the WIRE SHAPE against omni-backend: the paths, the field names and
# what comes back out of auth.json. They record the calls rather than standing a
# server up, so what is asserted is what cloud.py sends — if the backend's
# contract moves, these are the tests that have to be changed with it.
#
#   POST /api/v1/auth/sign-up  {email, username, password}  -> {data:{token,user,subscription}}
#   POST /api/v1/keys/redeem   {code}                       -> {data:{subscription}}

def _record_requests(monkeypatch, response):
    """Replace cloud.request with a recorder that answers `response`."""
    calls = []

    def fake_request(method, path, payload=None, **kwargs):
        calls.append({"method": method, "path": path, "payload": payload, **kwargs})
        return response

    monkeypatch.setattr(cloud, "request", fake_request)
    return calls


FREE_SIGNUP_RESPONSE = {
    "data": {
        "token": "tok-free",
        "user": {"_id": "u1", "email": "a@b.c", "username": "berat"},
        "subscription": {"plan": None, "planLabel": None, "expiresAt": None,
                         "active": False, "tier": "free", "daysRemaining": None},
    }
}


def test_register_sends_a_username_and_no_license_key(config_home, monkeypatch):
    calls = _record_requests(monkeypatch, FREE_SIGNUP_RESPONSE)

    out = cloud.register("A@B.c ", "berat", "hunter22")

    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/api/v1/auth/sign-up"
    # The key field is gone entirely, not sent empty: the server would reject a
    # payload it does not expect far less clearly than one it does.
    assert calls[0]["payload"] == {"email": "A@B.c ", "username": "berat",
                                   "password": "hunter22"}
    assert "key" not in calls[0]["payload"]
    # Sign-up is unauthenticated — there is no token yet to send.
    assert calls[0]["with_auth"] is False
    assert out["username"] == "berat"
    assert out["subscription"]["tier"] == "free"
    # The token is saved but never handed back to the UI layer.
    assert "token" not in out
    assert cloud.auth()["token"] == "tok-free"
    assert cloud.auth()["username"] == "berat"


def test_redeem_upgrades_the_saved_subscription_in_place(config_home, monkeypatch):
    cloud._save_auth({"token": "tok-free", "email": "a@b.c", "username": "berat",
                      "subscription": {"tier": "free", "plan": None}})
    premium = {"plan": "30_day", "planLabel": "30 days", "active": True,
               "tier": "premium", "daysRemaining": 30}
    calls = _record_requests(monkeypatch, {"data": {"subscription": premium}})

    out = cloud.redeem("  omni-aaaa-bbbb-cccc  ")

    assert calls[0]["path"] == "/api/v1/keys/redeem"
    # Keys are printed in upper case; typing one in lower case must still work.
    assert calls[0]["payload"] == {"code": "OMNI-AAAA-BBBB-CCCC"}
    assert out["tier"] == "premium"
    # Written through to auth.json, so a launch that cannot reach the server
    # still shows Premium rather than dropping back to Free.
    assert cloud.auth()["subscription"]["tier"] == "premium"
    assert cloud.auth()["username"] == "berat"


def test_me_keeps_a_cached_username_when_the_account_predates_usernames(
        config_home, monkeypatch):
    cloud._save_auth({"token": "t", "email": "a@b.c", "username": "berat"})
    _record_requests(monkeypatch, {"data": {"user": {"email": "a@b.c"},
                                            "subscription": {"tier": "free"}}})

    out = cloud.me()

    assert out["username"] == "berat"
    assert cloud.auth()["username"] == "berat"


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
