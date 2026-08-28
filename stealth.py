"""Anti-detection Chrome for account creation.

WHY THIS EXISTS. Roblox scores every signup POST server-side, and Arkose
mints the captcha token with a risk score computed from the browser
environment. A vanilla Selenium driver announces itself four ways —
``navigator.webdriver === true``, the ``--enable-automation`` switch (the
"Chrome is being controlled" banner), the automation extension, and the
``cdc_*`` variables the chromedriver binary leaks into every page — so even a
CORRECTLY solved captcha arrives inside a high-risk token and Roblox answers
403 Forbidden ("An unknown error occurred, please try again later"), then asks
for the captcha all over again. A normal browser on the same machine and the
same VPN sails through, which is exactly what was observed.

The fix is not to solve harder; it is to stop announcing the automation:

  1. If ``undetected_chromedriver`` is installed, use it. It BINARY-PATCHES
     the chromedriver executable to strip the ``cdc_*`` variables — the one
     signal no JavaScript patching can hide — and handles the rest.
  2. Otherwise fall back to hardened vanilla Selenium: drop the automation
     switch and extension, disable the AutomationControlled blink feature,
     and inject a stealth script (webdriver, window.chrome, plugins,
     languages) into every document before any page script runs.

The injected signals are kept CONSISTENT with what the rest of the flow
asserts (force_english sets an en-US Accept-Language header, so the script
reports ``['en-US', 'en']``). WebGL vendor/renderer and hardwareConcurrency
are deliberately LEFT ALONE: the real values are self-consistent, and a fake
GPU string beside a real canvas hash is a bigger flag than a common one.
"""

import base64
import select
import socket
import ssl
import sys
import threading
import urllib.parse


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

if (!window.chrome) {
  window.chrome = {
    runtime: {},
    loadTimes: function() { return {}; },
    csi: function() { return {}; },
    app: {
      isInstalled: false,
      InstallState: {DISABLED: 'disabled', INSTALLED: 'installed',
                     NOT_INSTALLED: 'not_installed'},
      RunningState: {CANNOT_RUN: 'cannot_run', RUNNING: 'running',
                     READY_TO_RUN: 'ready_to_run'}
    }
  };
}

Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

Object.defineProperty(navigator, 'plugins', {get: () => {
  const plugin = (name, desc) => ({
    name: name, filename: 'internal-pdf-viewer', description: desc, length: 1,
    0: {type: 'application/pdf', suffixes: 'pdf'},
    item: (i) => (i === 0 ? this[0] : null),
    namedItem: (n) => (n === 'application/pdf' ? this[0] : null),
  });
  return [
    plugin('Chrome PDF Viewer', 'Portable Document Format'),
    plugin('Chromium PDF Viewer', 'Portable Document Format'),
    plugin('Microsoft Edge PDF Viewer', 'Portable Document Format'),
    plugin('WebKit built-in PDF', 'Portable Document Format'),
  ];
}});

const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _origQuery(p));
}
"""


def _resolve_paths():
    """(driver_path, browser_path) via omnidroid's Selenium-Manager plumbing,
    or (None, None) when the engine is not importable — the vanilla fallback
    then lets Selenium resolve on its own."""
    try:
        import accountsync
        accounts = accountsync._omnidroid_accounts()
        return accounts._resolve_chromedriver()
    except Exception:  # noqa: BLE001 - any loader failure falls back
        return None, None


# ---------------------------------------------------------------------------
# the proxy — Chrome's --proxy-server speaks ONLY scheme://host:port
# ---------------------------------------------------------------------------

class _AuthRelay:
    """A local HTTP proxy that forwards to an AUTHENTICATED upstream.

    Chrome's --proxy-server accepts no credentials, and a residential proxy
    pasted as host:port:user:pass therefore killed the browser with
    ERR_NO_SUPPORTED_PROXIES. The relay takes Chrome's UNauthenticated traffic
    on 127.0.0.1 and adds the Proxy-Authorization header on its way upstream.

    CONNECT (every https request) and absolute-URI http are both handled; the
    upstream may itself be http(s) (this is what the 2captcha dashboard hands
    out) or socks5, whose username/password handshake this client speaks."""

    def __init__(self, scheme, host, port, user, password):
        self._scheme = scheme
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._srv = None
        self._thread = None
        self.port = None
        self._target_host = None      # set per-connection, read by socks5
        self._target_port = None

    def start(self):
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name=f"proxy-relay-{self.port}")
        self._thread.start()
        return self

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return          # socket closed: the process is shutting down
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    # -- upstream -----------------------------------------------------------

    def _connect_upstream(self):
        """A socket to the upstream proxy, authenticated and ready."""
        sock = socket.create_connection((self._host, self._port), timeout=20)
        sock.settimeout(20)
        try:
            if self._scheme == "socks5":
                self._socks5_handshake(sock)
            elif self._scheme == "https":
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self._host)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        sock.settimeout(None)    # piping is governed by the peers, not us
        return sock

    def _socks5_handshake(self, sock):
        """RFC 1928: methods, optional username/password auth (RFC 1929),
        then the CONNECT request — this module never BINDs or UDPs."""
        auth = 0x02 if self._user else 0x00
        sock.sendall(b"\x05\x01" + bytes([auth]))
        resp = sock.recv(2)
        if len(resp) < 2 or resp[0] != 5 or resp[1] not in (0x00, auth):
            raise OSError(f"socks5 handshake refused (method {resp[1:]})")
        if resp[1] == 0x02:
            u = self._user.encode()
            p = self._password.encode()
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if sock.recv(2)[1] != 0:
                raise OSError("socks5 credentials rejected")
        host = self._target_host.encode()   # set by the caller first
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host
                     + self._target_port.to_bytes(2, "big"))
        if sock.recv(2)[1] != 0:
            raise OSError("socks5 CONNECT refused")

    # -- per client connection ----------------------------------------------

    def _handle(self, conn):
        try:
            head = self._read_head(conn)
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method = line.split(" ", 1)[0].upper()
            if method == "CONNECT":
                self._do_connect(conn, line, head)
            else:
                self._do_http(conn, head)
        except Exception as e:  # noqa: BLE001 - one dead client, never the relay
            print(f"[stealth] relay: {e}", flush=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_head(conn, cap=32768):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                break
            head += chunk
            if len(head) > cap:
                raise OSError("client request head too large")
        return head

    def _do_connect(self, conn, line, _head):
        # "CONNECT host:port HTTP/1.1" — host may be a bracketed IPv6.
        target = line.split(" ", 2)[1]
        if target.startswith("["):
            host, _, tail = target.rpartition("]:")
            self._target_host, self._target_port = host.lstrip("["), int(tail)
        else:
            host, _, port_s = target.rpartition(":")
            self._target_host, self._target_port = host, int(port_s or 443)

        up = self._connect_upstream()
        if self._scheme in ("http", "https"):
            up.sendall(line.encode("latin-1") + b"\r\n"
                       + self._auth_header() + b"\r\n")
            status = b""
            while b"\r\n" not in status:
                b_ = up.recv(1)
                if not b_:
                    raise OSError("upstream closed during CONNECT")
                status += b_
            while b"\r\n\r\n" not in status:
                status += up.recv(4096)
            ok = status.split(b"\r\n", 1)[0].split(b" ")[1].startswith(b"2")
            if not ok:
                up.close()
                raise OSError(f"upstream CONNECT refused: {status[:60]!r}")
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        # socks5: _connect_upstream already tunnelled; just confirm to Chrome
        elif self._scheme == "socks5":
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        _pipe(conn, up)

    def _do_http(self, conn, head):
        """Plain-http request with an absolute URI: add the auth header and
        forward, relaying the body back until the upstream closes."""
        if self._scheme == "socks5":
            # The request line carries the absolute URI the proxy must dial.
            url = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[1]
            sp = urllib.parse.urlsplit(url)
            self._target_host, self._target_port = sp.hostname, sp.port or 80
            up = self._connect_upstream()
            up.sendall(head)
        else:
            up = self._connect_upstream()
            lines = [l for l in head.split(b"\r\n")
                     if not l.lower().startswith(b"proxy-authorization:")
                     and not l.lower().startswith(b"proxy-connection:")
                     and not l.lower().startswith(b"connection:")]
            lines.insert(1, self._auth_header().rstrip(b"\r\n"))
            lines.insert(2, b"Connection: close")
            up.sendall(b"\r\n".join(lines))
        try:
            _pipe(conn, up)
        finally:
            try:
                up.close()
            except OSError:
                pass

    def _auth_header(self):
        tok = base64.b64encode(
            f"{self._user}:{self._password}".encode()).decode()
        return f"Proxy-Authorization: Basic {tok}\r\n".encode("latin-1")


def _pipe(a, b):
    """Shuttle bytes both ways until either side dies, then kill both."""
    socks = [a, b]
    try:
        while socks:
            r, _, _ = select.select(socks, [], [], 60)
            if not r:
                continue
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    return
                (b if s is a else a).sendall(data)
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _parse_proxy(text):
    """(scheme, host, port, user, password) for any layout the residential
    dashboards hand out. accountcreator.parse_proxy is the tested authority —
    it is imported lazily, and only if that somehow fails is the minimal
    host:port:user:pass grammar parsed here."""
    try:
        import accountcreator
        d, err = accountcreator.parse_proxy(text)
        if err or not d:
            return None
        return (d["type"], d["address"], d["port"],
                d.get("login") or "", d.get("password") or "")
    except ImportError:
        raw = str(text).strip()
        scheme = "http"
        if "://" in raw:
            scheme, _, raw = raw.partition("://")
            scheme = scheme.lower()
        if "@" in raw:
            creds, _, raw = raw.rpartition("@")
            user, _, password = creds.partition(":")
        else:
            user = password = ""
        parts = raw.split(":")
        if len(parts) >= 4:
            host, port = parts[0], parts[1]
            user, password = parts[2], ":".join(parts[3:])
        else:
            host, port = parts[0], parts[1]
        return scheme, host, int(port), user, password


def _proxy_args(proxy, on_status=lambda m: None):
    """The Chrome arguments for a configured proxy, or [] for none/direct.

    Credential-less proxies go straight to --proxy-server. Proxies WITH
    credentials (the common residential layout) cannot: Chrome refuses the
    flag with ERR_NO_SUPPORTED_PROXIES, so they are routed through a local
    _AuthRelay that adds the authentication upstream."""
    if not proxy or not str(proxy).strip():
        return []
    parsed = _parse_proxy(proxy)
    if parsed is None:
        on_status(f"[stealth] proxy {str(proxy).strip()!r} is malformed — "
                  "browser going DIRECT")
        return []
    scheme, host, port, user, password = parsed
    if not user:
        return [f"--proxy-server={scheme}://{host}:{port}"]
    if scheme == "socks4":
        # socks4 carries no username/password subnegotiation worth speaking.
        on_status(f"[stealth] socks4 proxies cannot authenticate — using "
                  f"socks4://{host}:{port} WITHOUT credentials")
        return [f"--proxy-server=socks4://{host}:{port}"]
    try:
        relay = _AuthRelay(scheme, host, port, user, password).start()
    except Exception as e:  # noqa: BLE001 - direct beats a dead session
        on_status(f"[stealth] could not start the proxy relay ({e}) — "
                  "browser going DIRECT")
        return []
    on_status(f"[stealth] authenticated proxy {host}:{port} relayed via "
              f"127.0.0.1:{relay.port}")
    return [f"--proxy-server=http://127.0.0.1:{relay.port}"]


def _uc_driver(headless, proxy, on_status):
    """undetected_chromedriver, if the package is present. Returns a driver
    or None (so the caller can fall back) — never raises for a MISSING
    package, only a genuine startup failure propagates."""
    try:
        import undetected_chromedriver as uc
    except ImportError:
        return None
    opts = uc.ChromeOptions()
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    if headless:
        opts.add_argument("--headless=new")
    for arg in _proxy_args(proxy, on_status):
        opts.add_argument(arg)
    driver_path, browser_path = _resolve_paths()
    kwargs = {"options": opts}
    if driver_path:
        kwargs["driver_executable_path"] = driver_path
    if browser_path:
        kwargs["browser_executable_path"] = browser_path
    try:
        drv = uc.Chrome(**kwargs)
    except Exception as e:  # noqa: BLE001 - a broken uc must not kill creation
        on_status(f"[stealth] undetected_chromedriver failed ({e}); "
                  "falling back to stealth patches")
        return None
    try:
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                            {"source": STEALTH_JS})
    except Exception:  # noqa: BLE001 - uc already patches the essentials
        pass
    on_status("[stealth] driving through undetected_chromedriver")
    return drv


def _vanilla_stealth_driver(headless, proxy, on_status):
    """Hardened vanilla Selenium: hides everything a CDP script can hide.

    What this path CANNOT hide is the chromedriver binary's cdc_* variables
    (only a patched binary — the uc path above — removes those), which is why
    installing undetected_chromedriver is the recommended configuration."""
    from selenium import webdriver
    opts = webdriver.ChromeOptions()
    # AutomationControlled is the blink feature behind navigator.webdriver.
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    if headless:
        opts.add_argument("--headless=new")
    for arg in _proxy_args(proxy, on_status):
        opts.add_argument(arg)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option(
        "prefs", {"credentials_enable_service": False,
                  "profile.password_manager_enabled": False})

    driver_path, browser_path = _resolve_paths()
    if browser_path:
        opts.binary_location = browser_path
    if driver_path:
        from selenium.webdriver.chrome.service import Service
        drv = webdriver.Chrome(service=Service(executable_path=driver_path),
                               options=opts)
    else:
        drv = webdriver.Chrome(options=opts)
    try:
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                            {"source": STEALTH_JS})
    except Exception as e:  # noqa: BLE001 - no CDP, or an odd build
        on_status(f"[stealth] could not inject the stealth script ({e})")
    on_status("[stealth] driving through hardened Selenium "
              "(install undetected-chromedriver for full masking)")
    return drv


def make_driver(headless=False, proxy=None, on_status=lambda m: None):
    """A Chrome that does not look automated. Headful by default — the signup
    flow may need a human to see the window."""
    drv = _uc_driver(headless, proxy, on_status)
    if drv is not None:
        return drv
    return _vanilla_stealth_driver(headless, proxy, on_status)


if __name__ == "__main__":
    # A five-second smoke check: start a driver, read back the two signals
    # Roblox's page scripts read first, print them, quit.
    d = make_driver()
    try:
        d.get("data:text/html,<title>t</title>")
        print("navigator.webdriver =", d.execute_script("return navigator.webdriver"))
        print("window.chrome       =", bool(d.execute_script("return window.chrome")))
        print("languages           =", d.execute_script("return navigator.languages"))
    finally:
        d.quit()
    sys.exit(0)
