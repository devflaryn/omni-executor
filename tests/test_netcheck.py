"""The Network tab's probe.

What is worth pinning down here is not "does urllib work" but the judgement
calls netcheck makes on top of it, because every one of them is a claim the UI
then shows a user as a coloured lamp:

  * a slow answer is not a dead link, and a 429 is not a dead link either;
  * a proxy that is merely CONFIGURED is not a proxy that works, and its
    password must not travel back to the frontend to be read off a screenshot;
  * three probes behind a six-second timeout must cost six seconds, not
    eighteen.
"""

import socket
import time
import urllib.error

import pytest

import main
import netcheck


# --------------------------------------------------------------- classify

def test_a_slow_answer_is_slow_not_down():
    """Down means "no answer". A link that answers in four seconds is bad, but
    reporting it as down would send the user hunting for an outage that isn't
    there."""
    assert netcheck.classify(True, netcheck.OK_MS) == "ok"
    assert netcheck.classify(True, netcheck.OK_MS + 0.1) == "slow"
    assert netcheck.classify(True, 4000) == "slow"
    assert netcheck.classify(False, 12.0) == "down"


# ------------------------------------------------------------- http_probe

class _FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self, n=None):
        return self._body[:n] if n else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, outcome):
        self._outcome = outcome
        self.opened = []

    def open(self, req, timeout=None):
        self.opened.append((req.full_url, timeout))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


@pytest.fixture
def opener(monkeypatch):
    """Install a fake opener and hand back the ProxyHandler it was built with,
    so the proxy wiring is observable without a network."""
    box = {}

    def install(outcome):
        fake = _FakeOpener(outcome)

        def fake_build_opener(*handlers):
            box["handlers"] = handlers
            return fake

        monkeypatch.setattr(netcheck.urllib.request, "build_opener", fake_build_opener)
        box["opener"] = fake
        return box

    return install


def test_a_rate_limit_still_proves_the_route_is_open(opener):
    """Roblox answers 429 to a machine that has been signing up all afternoon.
    The network is fine and saying "down" would point the user at the wrong
    problem entirely — but the code has to survive into the UI, or a throttled
    host reads as a healthy one."""
    opener(urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None))
    ok, ms, status, detail = netcheck.http_probe("https://example.invalid/")
    assert ok is True
    assert status == 429
    assert "429" in detail
    assert ms >= 0


def test_the_server_admitting_it_is_broken_counts_against_it(opener):
    opener(urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None))
    ok, _ms, status, detail = netcheck.http_probe("https://example.invalid/")
    assert ok is False
    assert status == 503
    assert "503" in detail


def test_a_filtered_name_is_reported_as_dns_not_an_errno(opener):
    """The one failure this app actually hits: on a network that blocks Roblox
    by DNS, urllib raises gaierror and its str() is "[Errno 11001] getaddrinfo
    failed", which tells a user nothing they can act on."""
    opener(urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")))
    ok, _ms, status, detail = netcheck.http_probe("https://example.invalid/")
    assert ok is False
    assert status is None
    assert detail == "Name not resolved (DNS)"


def test_a_timeout_names_the_deadline_it_actually_used(opener):
    opener(urllib.error.URLError(socket.timeout()))
    _ok, _ms, _status, detail = netcheck.http_probe("https://example.invalid/", timeout=2)
    assert detail == "No answer in 2s"


def test_an_unproxied_probe_disables_urllibs_environment_scan(opener):
    """A stray HTTPS_PROXY in the environment would silently reroute the one
    request whose job is to report the path the app really takes."""
    box = opener(_FakeResponse())
    netcheck.http_probe("https://example.invalid/")
    handler = box["handlers"][0]
    assert handler.proxies == {}


def test_a_proxied_probe_dials_through_the_proxy(opener):
    box = opener(_FakeResponse())
    netcheck.http_probe("https://example.invalid/", proxy_url="http://p:1")
    assert box["handlers"][0].proxies == {"http": "http://p:1", "https": "http://p:1"}


# ------------------------------------------------------------------ proxy

def test_proxy_credentials_are_percent_encoded():
    """Residential passwords routinely contain ':' and '@', which are exactly
    the characters that decide where a proxy URL splits."""
    url = netcheck.proxy_url({"type": "http", "address": "gate.io", "port": 7000,
                              "login": "user", "password": "p@:ss"})
    assert url == "http://user:p%40%3Ass@gate.io:7000"


def test_no_proxy_means_no_row():
    """A row for something switched off is noise. `None` is the signal
    probe_all filters on."""
    assert netcheck.probe_proxy("") is None
    assert netcheck.probe_proxy(None) is None
    assert netcheck.probe_proxy("   ") is None


def test_an_unparseable_proxy_is_down_with_the_validators_own_words():
    row = netcheck.probe_proxy("nonsense")
    assert row["status"] == "down"
    assert row["ms"] is None
    assert "host and a port" in row["detail"]


def test_the_proxy_row_never_carries_the_password(monkeypatch):
    """This dict crosses into the frontend and lands in a label a user can
    screenshot. host:port is all the identification a row needs."""
    monkeypatch.setattr(netcheck, "http_probe",
                        lambda *a, **k: (True, 120.0, 200, ""))
    row = netcheck.probe_proxy("gate.io:7000:someuser:hunter2")
    assert "hunter2" not in repr(row)
    assert "someuser" not in repr(row)
    assert row["url"] == "http://gate.io:7000"
    assert row["status"] == "ok"


def test_a_socks_proxy_is_measured_by_connect_and_says_so(monkeypatch):
    """urllib speaks no SOCKS and the app ships no SOCKS library, so the check
    is smaller than the HTTP one. The note has to admit that, or a green lamp
    claims an end-to-end test that never ran."""
    called = {}

    def fake_tcp(host, port, timeout=netcheck.TIMEOUT):
        called["at"] = (host, port)
        return True, 42.0, None, ""

    monkeypatch.setattr(netcheck, "tcp_probe", fake_tcp)
    monkeypatch.setattr(netcheck, "http_probe",
                        lambda *a, **k: pytest.fail("SOCKS cannot go through urllib"))
    row = netcheck.probe_proxy("socks5://gate.io:1080")
    assert called["at"] == ("gate.io", 1080)
    assert row["status"] == "ok"
    assert "not an end-to-end check" in row["note"]


# -------------------------------------------------------------- probe_all

def test_probe_all_omits_the_proxy_until_one_is_configured(monkeypatch):
    monkeypatch.setattr(netcheck, "http_probe", lambda *a, **k: (True, 100.0, 200, ""))
    ids = [t["id"] for t in netcheck.probe_all("http://api")["targets"]]
    assert ids == ["roblox", "omni"]

    ids = [t["id"] for t in netcheck.probe_all("http://api", proxy="gate.io:7000")["targets"]]
    assert ids == ["roblox", "omni", "proxy"]


def test_probe_all_costs_the_slowest_probe_not_their_sum(monkeypatch):
    """Serial probing would make a fully-down network take one timeout PER
    target — 18 seconds with a proxy configured, which is far longer than
    anyone waits before clicking Check again."""
    def slow(*_a, **_k):
        time.sleep(0.4)
        return True, 400.0, 200, ""

    monkeypatch.setattr(netcheck, "http_probe", slow)
    t0 = time.perf_counter()
    res = netcheck.probe_all("http://api", proxy="gate.io:7000")
    elapsed = time.perf_counter() - t0

    assert len(res["targets"]) == 3
    assert elapsed < 1.0, f"three 0.4s probes took {elapsed:.2f}s — they ran serially"


def test_probe_all_reports_the_thresholds_it_judged_by(monkeypatch):
    """The tab's legend is rendered from these. Hardcoding them in the
    frontend is how a UI ends up describing a rule the backend stopped
    using."""
    monkeypatch.setattr(netcheck, "http_probe", lambda *a, **k: (True, 100.0, 200, ""))
    res = netcheck.probe_all("http://api", timeout=3)
    assert res["okMs"] == netcheck.OK_MS
    assert res["timeoutMs"] == 3000
    assert res["checkedAt"] > 0


def test_the_omni_target_asks_the_unauthenticated_health_route(monkeypatch):
    """It is mounted before omni-backend's Arcjet middleware, so a probe every
    20 seconds cannot spend the user's bot-detection budget or trip a rate
    limit on the routes that matter."""
    seen = {}

    def capture(url, proxy_url=None, timeout=netcheck.TIMEOUT):
        seen["url"] = url
        return True, 100.0, 200, ""

    monkeypatch.setattr(netcheck, "http_probe", capture)
    netcheck.probe_omni("http://72.62.59.232/")
    assert seen["url"] == "http://72.62.59.232/omni/dist/health"


# ------------------------------------------------- the Api method behind it

def test_net_probe_dials_the_configured_proxy_and_api_base(monkeypatch):
    """Read from settings on every call, never cached: the proxy field sits
    on the same page as the check, so editing it and pressing Check has to
    test what was just typed."""
    seen = {}

    def fake_probe_all(api_base, proxy=None, **_kw):
        seen["base"], seen["proxy"] = api_base, proxy
        return {"ok": True, "targets": []}

    monkeypatch.setattr(netcheck, "probe_all", fake_probe_all)
    api = main.Api()
    monkeypatch.setattr(api, "get_settings",
                        lambda: {"captcha": {"proxy": "gate.io:7000:u:p"}})
    monkeypatch.setattr(api, "_api_base", lambda: "http://server")

    assert api.net_probe()["ok"] is True
    assert seen == {"base": "http://server", "proxy": "gate.io:7000:u:p"}


def test_net_probe_survives_a_probe_that_raises(monkeypatch):
    """The tab polls this every 20 seconds. An exception crossing the
    pywebview bridge is a dead tab, not a failed reading."""
    def boom(*_a, **_k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(netcheck, "probe_all", boom)
    api = main.Api()
    monkeypatch.setattr(api, "get_settings", lambda: {})
    monkeypatch.setattr(api, "_api_base", lambda: "http://server")

    res = api.net_probe()
    assert res["ok"] is False
    assert res["targets"] == []
    assert "resolver exploded" in res["message"]


def test_net_probe_treats_an_unreadable_settings_file_as_no_proxy(monkeypatch):
    seen = {}
    monkeypatch.setattr(netcheck, "probe_all",
                        lambda base, proxy=None, **_k: seen.update(proxy=proxy) or {"ok": True})
    api = main.Api()

    def broken():
        raise OSError("settings.json is a directory")

    monkeypatch.setattr(api, "get_settings", broken)
    monkeypatch.setattr(api, "_api_base", lambda: "http://server")

    assert api.net_probe()["ok"] is True
    assert seen["proxy"] == ""
