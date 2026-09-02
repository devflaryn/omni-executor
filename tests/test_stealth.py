"""stealth: proxy normalization and the authenticated relay.

The parser is the guard that keeps Chrome from ever seeing a --proxy-server
value it will reject (ERR_NO_SUPPORTED_PROXIES); the relay tests prove the
Proxy-Authorization header actually reaches the upstream and that CONNECT
tunnels byte-for-byte."""

import socket
import sys
import threading

import pytest

import stealth


# ---------------------------------------------------------------- the parser

def test_blank_proxy_is_direct():
    assert stealth._proxy_args("") == []
    assert stealth._proxy_args(None) == []
    assert stealth._proxy_args("   ") == []


def test_plain_hostport_keeps_chrome_native():
    args = stealth._proxy_args("1.2.3.4:8080")
    assert args == ["--proxy-server=http://1.2.3.4:8080"]


def test_schemed_credless_passes_through():
    assert stealth._proxy_args("socks5://1.2.3.4:1080") == \
        ["--proxy-server=socks5://1.2.3.4:1080"]


def test_embedded_credentials_never_reach_chrome():
    """The bug this module exists for: host:port:user:pass must not appear
    in a --proxy-server flag."""
    args = stealth._proxy_args("ap.proxy.example.com:2334:u7:se:cret")
    assert len(args) == 1
    arg = args[0]
    assert arg.startswith("--proxy-server=http://127.0.0.1:")
    assert "u7" not in arg and "ap.proxy.example.com" not in arg


def test_userpass_at_layout_relayed():
    args = stealth._proxy_args("u7:p%40ss@1.2.3.4:8080")
    assert args and args[0].startswith("--proxy-server=http://127.0.0.1:")


def test_malformed_proxy_falls_back_to_direct():
    notes = []
    args = stealth._proxy_args("no-port-here", on_status=notes.append)
    assert args == []
    assert notes and "malformed" in notes[0]


def test_socks4_cannot_authenticate_says_so():
    notes = []
    args = stealth._proxy_args("socks4://u:p@1.2.3.4:1080",
                               on_status=notes.append)
    assert args == ["--proxy-server=socks4://1.2.3.4:1080"]
    assert notes and "cannot authenticate" in notes[0]


# --------------------------------------------------------------- the relay

class _FakeUpstream:
    """An HTTP proxy that records its headers and, on CONNECT, echoes bytes
    back through the tunnel. Listens on 127.0.0.1 only."""

    def __init__(self, expect_auth=True, always_407=False):
        self.expect_auth = expect_auth
        self.always_407 = always_407
        self.auth_header = None
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._run = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while self._run:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,),
                             daemon=True).start()

    def _one(self, conn):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                return
            head += chunk
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"proxy-authorization:"):
                self.auth_header = line
        if head.startswith(b"CONNECT"):
            if self.always_407 or (self.expect_auth and self.auth_header is None):
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required"
                             b"\r\n\r\n")
                conn.close()
                return
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            try:                      # echo server inside the tunnel
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    conn.sendall(data)
            except OSError:
                pass
        else:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        conn.close()

    def stop(self):
        self._run = False
        self._srv.close()


def _relay_for(proxy):
    parsed = stealth._parse_proxy(proxy)
    return stealth._AuthRelay(*parsed).start()


def test_relay_adds_proxy_authorization_on_connect():
    """The full chain: Chrome -> relay -> fake upstream. The upstream must
    see a Basic Proxy-Authorization header and the tunnel must carry bytes."""
    up = _FakeUpstream()
    try:
        relay = _relay_for("me:pw@127.0.0.1:%d" % up.port)
        c = socket.create_connection(("127.0.0.1", relay.port), timeout=10)
        c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443"
                  b"\r\n\r\n")
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += c.recv(4096)
        assert b"200 Connection established" in resp
        assert up.auth_header and b"bWU6cHc=" in up.auth_header  # me:pw
        c.sendall(b"ping")
        assert c.recv(65536) == b"ping"
        c.close()
    finally:
        up.stop()


def test_relay_handles_password_containing_at():
    up = _FakeUpstream()
    try:
        relay = _relay_for("user:p@ss@127.0.0.1:%d" % up.port)
        c = socket.create_connection(("127.0.0.1", relay.port), timeout=10)
        c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += c.recv(4096)
        assert b"200 Connection established" in resp
        assert up.auth_header and b"dXNlcjpwQHNz" in up.auth_header  # user:p@ss
        c.close()
    finally:
        up.stop()


def test_relay_upstream_407_surfaces_as_tunnel_failure():
    """An upstream that refuses the credentials must NEVER surface as a
    successful tunnel — the client connection is closed instead."""
    up = _FakeUpstream(always_407=True)
    try:
        relay = _relay_for("u:pw@127.0.0.1:%d" % up.port)
        c = socket.create_connection(("127.0.0.1", relay.port), timeout=10)
        c.settimeout(10)
        c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        try:
            data = c.recv(4096)
            assert b"200" not in data
        except (ConnectionResetError, OSError):
            pass                  # closed without a reply is also acceptable
        c.close()
    finally:
        up.stop()


def test_relay_forwards_plain_http_with_auth():
    up = _FakeUpstream()
    try:
        relay = _relay_for("u:pw@127.0.0.1:%d" % up.port)
        c = socket.create_connection(("127.0.0.1", relay.port), timeout=10)
        c.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com"
                  b"\r\n\r\n")
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += c.recv(4096)
        assert b"200 OK" in resp
        assert up.auth_header and b"basic" in up.auth_header.lower()
        c.close()
    finally:
        up.stop()


@pytest.mark.parametrize("layout,expected", [
    ("1.2.3.4:8080", ("http", "1.2.3.4", 8080, "", "")),
    ("1.2.3.4:8080:u:p:wd", ("http", "1.2.3.4", 8080, "u", "p:wd")),
    ("u:p@1.2.3.4:8080", ("http", "1.2.3.4", 8080, "u", "p")),
    ("socks5://u:p@1.2.3.4:1080", ("socks5", "1.2.3.4", 1080, "u", "p")),
])
def test_parse_layouts(layout, expected):
    assert stealth._parse_proxy(layout) == expected


# ------------------------------------------------- the selenium-stealth layer
#
# What is worth pinning here is not that selenium-stealth works -- that is its
# own package's problem -- but the ARGUMENTS it is handed. Every one of them
# was chosen to stay consistent with something else (the en-US header
# force_english sends, the real machine's GPU and platform), and a default
# quietly creeping back in is a fingerprint mismatch that no test would
# otherwise catch: the browser still starts, still solves, and still gets 403.


class _FakeChrome:
    """Enough driver for the masking layer: answers the two fingerprint reads
    and records every CDP command."""

    def __init__(self, platform="Win32", webgl=("NVIDIA Corporation",
                                                "NVIDIA GeForce RTX 3060"),
                 script_error=None):
        self._platform, self._webgl = platform, webgl
        self._script_error = script_error
        self.cdp = []

    def execute_script(self, js):
        if self._script_error is not None:
            raise self._script_error
        if "navigator.platform" in js:
            return self._platform
        if "UNMASKED_VENDOR_WEBGL" in js:
            return list(self._webgl) if self._webgl else None
        raise AssertionError(f"unexpected script: {js[:60]!r}")

    def execute_cdp_cmd(self, cmd, params):
        self.cdp.append((cmd, params))
        return {}


@pytest.fixture
def recorded_stealth(monkeypatch):
    """selenium_stealth.stealth, replaced by a recorder. The real one type
    checks for a live selenium.webdriver.Chrome, which no unit test has."""
    calls = []
    monkeypatch.setattr("selenium_stealth.stealth",
                        lambda drv, **kw: calls.append((drv, kw)))
    return calls


def test_stealth_is_applied_with_consistent_values(recorded_stealth):
    drv = _FakeChrome(platform="MacIntel", webgl=("Apple", "Apple M2"))
    assert stealth._apply_stealth(drv, lambda m: None) is True

    (called_on, kw), = recorded_stealth
    assert called_on is drv
    # en-US here, en-US in accountcreator.force_english's Accept-Language.
    assert kw["languages"] == ["en-US", "en"]
    assert kw["vendor"] == "Google Inc."
    # NOT selenium-stealth's "Win32" default -- this browser says MacIntel,
    # and its user-agent string will say Macintosh right beside it.
    assert kw["platform"] == "MacIntel"
    # The real GPU, echoed back: the override becomes a no-op instead of
    # putting "Intel Iris OpenGL Engine" next to an Apple canvas hash.
    assert kw["webgl_vendor"] == "Apple"
    assert kw["renderer"] == "Apple M2"
    # Passing a user_agent pins the library's 2020-era Chrome/83 string;
    # omitting it makes selenium-stealth read the REAL one over CDP.
    assert "user_agent" not in kw


def test_unreadable_webgl_leaves_the_library_defaults(recorded_stealth):
    """No GL on the box (a bare VM, a server): there is no real value to be
    consistent with, so the library's common default is the better lie."""
    drv = _FakeChrome(webgl=None)
    stealth._apply_stealth(drv, lambda m: None)

    (_, kw), = recorded_stealth
    assert "webgl_vendor" not in kw and "renderer" not in kw
    assert kw["platform"] == "Win32"


def test_platform_is_never_none(recorded_stealth):
    """CDP's Network.setUserAgentOverride rejects a null platform, so a
    browser that will not answer the read still has to yield a string."""
    drv = _FakeChrome(script_error=RuntimeError("no such session"))
    stealth._apply_stealth(drv, lambda m: None)

    (_, kw), = recorded_stealth
    assert isinstance(kw["platform"], str) and kw["platform"]


def test_missing_package_falls_back_to_the_builtin_script(monkeypatch):
    """A machine without selenium-stealth must lose masking QUALITY, not
    masking: the built-in subset still goes in."""
    monkeypatch.setitem(sys.modules, "selenium_stealth", None)
    drv, notes = _FakeChrome(), []
    assert stealth._apply_stealth(drv, notes.append) is False

    assert drv.cdp == [("Page.addScriptToEvaluateOnNewDocument",
                        {"source": stealth._FALLBACK_JS})]
    assert "'webdriver'" in stealth._FALLBACK_JS
    assert any("selenium-stealth is not installed" in n for n in notes)


def test_a_failed_stealth_call_does_not_kill_the_browser(monkeypatch):
    """The driver is already up and the account is already half made; a
    masking failure is reported and the flow goes on."""
    def boom(drv, **kw):
        raise RuntimeError("no chrome devtools")
    monkeypatch.setattr("selenium_stealth.stealth", boom)

    notes = []
    assert stealth._apply_stealth(_FakeChrome(), notes.append) is False
    assert any("could not be applied" in n for n in notes)
