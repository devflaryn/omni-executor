"""The name on the account, across two computers, and using the app as a guest."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cloud  # noqa: E402
import main  # noqa: E402


class FakeServer:
    """Just enough of the Omni API, holding one account."""

    def __init__(self):
        self.user = {"_id": "u1", "email": "a@example.com", "username": None}
        self.profile_works = True
        self.saves = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, {"success": True, "data": {"user": outer.user, "subscription": {}}})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._send(200, {"success": True, "data": {"token": "t", "user": outer.user}})

            def do_PATCH(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if not outer.profile_works:
                    return self._send(404, {"success": False, "error": "Not found"})
                outer.saves += 1
                outer.user.update(body)
                self._send(200, {"success": True, "data": {"user": outer.user}})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setenv("OMNI_API_BASE", s.url)
    yield s
    s.httpd.shutdown()


class Computer:
    """One computer: its own config folder and settings file."""

    def __init__(self, folder, monkeypatch):
        self.folder, self.monkeypatch = Path(folder), monkeypatch
        self.folder.mkdir(parents=True, exist_ok=True)
        self.use()
        self.api = main.Api.__new__(main.Api)
        self.api._known_running = set()
        self.api._push = lambda *a, **k: None
        self.api._after_sign_in = lambda: self.api.save_settings({"guest": False})
        self.api._release_all_leases = lambda: None

    def use(self):
        self.monkeypatch.setenv("APPDATA", str(self.folder))
        self.monkeypatch.setenv("XDG_CONFIG_HOME", str(self.folder))
        self.monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.folder))
        self.monkeypatch.setattr(main, "SETTINGS_FILE", self.folder / "settings.json")
        return self.api if hasattr(self, "api") else None

    def login(self):
        self.use()
        assert self.api.auth_login("a@example.com", "pw")["ok"]


@pytest.fixture
def computers(tmp_path, monkeypatch):
    return lambda name: Computer(tmp_path / name, monkeypatch)


def test_a_name_changed_on_one_computer_shows_on_another(server, computers):
    a, b = computers("a"), computers("b")
    a.login()
    b.login()
    a.use()
    assert a.api.profile_save(" Berat ", "Farming")["synced"] is True
    b.use()
    status = b.api.auth_status()
    assert status["profile"] == {"name": "Berat", "tag": "Farming"}
    assert b.api.get_settings()["profile"]["name"] == "Berat"


def test_a_computer_already_signed_in_picks_up_the_change(server, computers):
    a, b = computers("a"), computers("b")
    a.login()
    b.login()
    b.use()
    b.api.auth_status()
    a.use()
    a.api.profile_save("New", "")
    b.use()
    assert b.api.auth_status()["profile"]["name"] == "New"


def test_a_name_set_before_this_fix_goes_up_once(server, computers):
    a = computers("a")
    a.login()
    a.api.save_settings({"profile": {"name": "devflaryn", "tag": "Hi"}})
    assert a.api.auth_status()["profile"] == {"name": "devflaryn", "tag": "Hi"}
    assert server.user["displayName"] == "devflaryn"
    assert server.saves == 1
    a.api.auth_status()
    assert server.saves == 1


def test_the_account_beats_an_old_name_on_another_computer(server, computers):
    a, b = computers("a"), computers("b")
    a.login()
    a.api.profile_save("Account name", "")
    b.login()
    b.api.save_settings({"profile": {"name": "Old local", "tag": ""}})
    assert b.api.auth_status()["profile"]["name"] == "Account name"
    assert server.user["displayName"] == "Account name"


def test_clearing_a_name_stays_cleared_everywhere(server, computers):
    a, b = computers("a"), computers("b")
    a.login()
    a.api.profile_save("Name", "")
    b.login()
    b.api.auth_status()
    a.use()
    a.api.profile_save("", "")
    b.use()
    assert b.api.auth_status()["profile"]["name"] == ""
    assert server.user["displayName"] == ""


def test_an_edit_the_server_missed_goes_up_later(server, computers):
    a = computers("a")
    a.login()
    a.api.profile_save("First", "")
    server.profile_works = False
    res = a.api.profile_save("Offline edit", "")
    assert res["ok"] is True and res["synced"] is False
    server.profile_works = True
    assert a.api.auth_status()["profile"]["name"] == "Offline edit"
    assert server.user["displayName"] == "Offline edit"
    assert a.api.get_settings()["profilePending"] is False


def test_an_older_server_leaves_the_local_name_working(server, computers):
    a = computers("a")
    a.login()
    server.profile_works = False
    a.api.save_settings({"profile": {"name": "Mine", "tag": ""}})
    assert a.api.auth_status()["profile"]["name"] == "Mine"


def test_the_old_default_name_is_never_uploaded(server, computers):
    a = computers("a")
    a.login()
    a.api.save_settings({"profile": {"name": "Guest", "tag": ""}})
    assert a.api.auth_status()["profile"]["name"] == ""
    assert server.saves == 0


def test_signing_out_does_not_leave_the_name_for_the_next_person(server, computers, monkeypatch):
    a = computers("a")
    a.login()
    a.api.profile_save("Mine", "")
    monkeypatch.setattr(main, "accountsync", lambda: type("S", (), {"forget_user": lambda self, u: []})())
    a.api.auth_logout()
    assert a.api.get_settings()["profile"] == {"name": "", "tag": ""}


def test_a_guest_needs_no_account_or_server(computers, monkeypatch):
    monkeypatch.setenv("OMNI_API_BASE", "http://127.0.0.1:9")
    a = computers("a")
    assert a.api.auth_status()["guest"] is False
    a.api.guest_enter()
    status = a.api.auth_status()
    assert status["guest"] is True and status["signedIn"] is False
    res = a.api.profile_save("Local", "")
    assert res["ok"] is True and res["synced"] is False


def test_a_guest_can_go_back_to_sign_in(computers):
    a = computers("a")
    a.api.guest_enter()
    a.api.guest_exit()
    assert a.api.auth_status()["guest"] is False


def test_signing_in_ends_guest_mode(server, computers):
    a = computers("a")
    a.api.guest_enter()
    a.login()
    status = a.api.auth_status()
    assert status["guest"] is False and status["signedIn"] is True
    assert a.api.get_settings()["guest"] is False


def test_a_guest_cannot_use_what_needs_the_server(computers):
    a = computers("a")
    a.api.guest_enter()
    assert cloud.signed_in() is False
    assert a.api.cloud_sync()["error"] == "signed_out"
    assert a.api.keys_redeem("OMNI-AAAA")["error"] == "signed_out"
