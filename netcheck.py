"""Reachability and latency for the two networks Omni Executor depends on.

The app is useless in two different ways when the network is wrong, and the
two look identical from the inside:

  * Roblox unreachable — logins hang, joins fail, and the guest reports a bare
    "Error 277" that says nothing about whose fault it is. On a filtered
    network the failure is DNS, not routing, so a probe has to actually try
    the name rather than ping an address.
  * The Omni server unreachable — sign-in, the account store and presence all
    stop, and `auth_status` deliberately keeps working from its last good
    answer, which means an offline machine LOOKS signed in and healthy.

So this module answers one question per target: did it answer, and how long
did it take. It is deliberately a full HTTP round trip and not an ICMP ping —
ICMP is the thing a filtered network lets through, and the thing that breaks
is the HTTP path.

Thresholds live here so the UI's legend and the verdicts can never disagree.
"""

import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# A probe is a foreground action the user is watching, so it fails fast: six
# seconds is already long enough that "down" is the honest word for it.
TIMEOUT = 6.0

# Everything at or under this is "ok". It is generous on purpose: the number
# is a whole HTTP request — DNS (on the first probe of a session), TCP, the
# TLS handshake and the response — against servers that are usually a
# continent away, so its floor sits well above a ping's.
OK_MS = 700

USER_AGENT = "OmniExecutor/1.0"

# The smallest public endpoint Roblox keeps up: user 1 is Roblox's own account
# and the payload is a few hundred bytes. No auth, no cookie, nothing cached.
ROBLOX_URL = "https://users.roblox.com/v1/users/1"

# Unauthenticated and mounted BEFORE the Arcjet middleware in omni-backend's
# server.js, so a probe cannot spend the user's bot-detection budget or trip a
# rate limit. See backend/src/omni-exec/distApi.js.
OMNI_PATH = "/omni/dist/health"

# Anything the server itself answers proves the network works, so only a 5xx
# — the server admitting it is broken — counts against it.
SERVER_ERROR = 500


def classify(ok, ms):
    """ok | slow | down. Down means "no answer", never "a slow answer"."""
    if not ok:
        return "down"
    return "ok" if ms <= OK_MS else "slow"


def _elapsed_ms(t0):
    return round((time.perf_counter() - t0) * 1000, 1)


def _reason(exc, timeout=TIMEOUT):
    """The part of a urllib failure worth putting in front of a user."""
    inner = getattr(exc, "reason", exc)
    if isinstance(exc, socket.timeout) or isinstance(inner, socket.timeout):
        return "No answer in {:.0f}s".format(timeout)
    if isinstance(inner, ssl.SSLError):
        return "TLS failed: {}".format(getattr(inner, "reason", None) or inner)
    if isinstance(inner, socket.gaierror):
        # The signature of a DNS-filtered network, which is exactly the case
        # this probe exists to name. Say so rather than print errno 11001.
        return "Name not resolved (DNS)"
    return str(inner) or type(exc).__name__


def http_probe(url, proxy_url=None, timeout=TIMEOUT):
    """One HTTP round trip. Returns (ok, ms, http_status, detail)."""
    # An explicit ProxyHandler either way: the empty one DISABLES urllib's
    # environment scan, so a stray HTTPS_PROXY in the shell cannot silently
    # reroute a probe whose whole job is to report the path we actually use.
    handler = urllib.request.ProxyHandler(
        {"http": proxy_url, "https": proxy_url} if proxy_url else {})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")

    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(2048)  # the first bytes only; the body is not the point
            return True, _elapsed_ms(t0), int(resp.status), ""
    except urllib.error.HTTPError as exc:
        ms = _elapsed_ms(t0)
        try:
            exc.read(512)
        except Exception:  # noqa: BLE001 - draining a body must not fail a probe
            pass
        if exc.code >= SERVER_ERROR:
            return False, ms, int(exc.code), "Server error {}".format(exc.code)
        # A 403/429 from Roblox still proves the route is open, and that is the
        # question here. Carry the code so a rate limit is not read as health.
        return True, ms, int(exc.code), "HTTP {}".format(exc.code)
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
        return False, _elapsed_ms(t0), None, _reason(exc, timeout)


def tcp_probe(host, port, timeout=TIMEOUT):
    """Connect and hang up. Returns (ok, ms, None, detail)."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, _elapsed_ms(t0), None, ""
    except (socket.timeout, OSError) as exc:
        return False, _elapsed_ms(t0), None, _reason(exc, timeout)


def proxy_url(parsed):
    """A parse_proxy() dict as a URL urllib can use. Credentials are
    percent-encoded: residential passwords routinely contain ':' and '@'."""
    auth = ""
    if parsed.get("login"):
        auth = "{}:{}@".format(
            urllib.parse.quote(str(parsed["login"]), safe=""),
            urllib.parse.quote(str(parsed.get("password") or ""), safe=""))
    return "{}://{}{}:{}".format(parsed["type"], auth, parsed["address"], parsed["port"])


def _target(id_, label, url, note=""):
    return {"id": id_, "label": label, "url": url, "note": note}


def probe_roblox(timeout=TIMEOUT):
    ok, ms, status, detail = http_probe(ROBLOX_URL, timeout=timeout)
    return {**_target("roblox", "Roblox", ROBLOX_URL,
                      "Logins and joins go through this host."),
            "status": classify(ok, ms), "ms": ms, "httpStatus": status,
            "detail": detail}


def probe_omni(api_base, timeout=TIMEOUT):
    url = "{}{}".format(str(api_base or "").rstrip("/"), OMNI_PATH)
    ok, ms, status, detail = http_probe(url, timeout=timeout)
    return {**_target("omni", "Omni server", url,
                      "Sign-in, your account list and presence."),
            "status": classify(ok, ms), "ms": ms, "httpStatus": status,
            "detail": detail}


def probe_proxy(proxy_text, timeout=TIMEOUT):
    """The configured outbound proxy, measured the way account creation will
    actually use it: a real request to Roblox THROUGH it.

    Returns None when no proxy is set — a row for something switched off is
    noise, not information.
    """
    import accountcreator  # lazy: keeps this module importable without selenium

    parsed, error = accountcreator.parse_proxy(proxy_text)
    if error:
        return {**_target("proxy", "Proxy", str(proxy_text or "").strip(),
                          "Outbound proxy for account creation."),
                "status": "down", "ms": None, "httpStatus": None, "detail": error}
    if not parsed:
        return None

    # host:port only — the credentials never leave Python, not even into a
    # label the user could screenshot.
    shown = "{}://{}:{}".format(parsed["type"], parsed["address"], parsed["port"])
    if parsed["type"] in ("http", "https"):
        ok, ms, status, detail = http_probe(ROBLOX_URL, proxy_url=proxy_url(parsed),
                                            timeout=timeout)
        note = "Reaching Roblox through the proxy."
    else:
        # urllib speaks no SOCKS and the app ships no SOCKS library, so a
        # connect is the largest claim available. The note says as much rather
        # than letting a green lamp imply an end-to-end check.
        ok, ms, status, detail = tcp_probe(parsed["address"], parsed["port"], timeout=timeout)
        note = "SOCKS: the proxy accepts connections (not an end-to-end check)."
    return {**_target("proxy", "Proxy", shown, note),
            "status": classify(ok, ms), "ms": ms, "httpStatus": status, "detail": detail}


def probe_all(api_base, proxy=None, timeout=TIMEOUT):
    """Every target at once. Parallel, so a check costs the SLOWEST probe
    rather than their sum — at 6s each, serial probing would make a fully-down
    network take longer than anyone waits before clicking again."""
    jobs = [lambda: probe_roblox(timeout), lambda: probe_omni(api_base, timeout)]
    if str(proxy or "").strip():
        jobs.append(lambda: probe_proxy(proxy, timeout))

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(job) for job in jobs]
        results = [f.result() for f in futures]

    return {"ok": True, "checkedAt": int(time.time() * 1000),
            "okMs": OK_MS, "timeoutMs": int(timeout * 1000),
            "targets": [r for r in results if r]}
