"""Solve the Arkose captcha inside the Roblox Android app's WebView, over CDP.

The desktop path drives Chrome through Selenium's execute_cdp_cmd. The Android
app renders the same Arkose challenge in a debuggable WebView (our APK patch
turns on setWebContentsDebuggingEnabled via the `omni.webview.debug` system
property). This module attaches to that WebView's DevTools endpoint over an
adb-forwarded port and exposes a tiny `drv` shim with exactly the two methods
visioncaptcha needs -- execute_cdp_cmd and execute_script -- so the existing
solver (visioncaptcha.play_challenge) runs unchanged against mobile.
"""
import json
import time
import urllib.request

import websocket  # websocket-client

import visioncaptcha as vc


class WebViewDrv:
    """A Selenium-shaped shim over a WebView's raw CDP WebSocket."""

    def __init__(self, ws_url, timeout=30):
        self.ws = websocket.create_connection(ws_url, max_size=None,
                                              timeout=timeout,
                                              suppress_origin=True)
        self._id = 0
        # Screenshots and input need these domains enabled on some builds.
        for dom in ("Page", "Runtime", "DOM"):
            try:
                self._send(f"{dom}.enable", {})
            except Exception:
                pass

    def _send(self, method, params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            # ignore events (msg with "method" and no matching id)

    # --- the two methods visioncaptcha calls -----------------------------
    def execute_cdp_cmd(self, method, params=None):
        return self._send(method, params or {})

    def execute_script(self, script, *args):
        # Selenium runs `function(){ <script> }.apply(null, args)` with
        # arguments[0..] bound. The scripts here `return ...` and may be async.
        expr = ("(async function(){ %s }).apply(null, %s)"
                % (script, json.dumps(list(args))))
        r = self._send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("text", "eval error"))
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def find_captcha_page(host_port, tries=20, delay=1.0):
    """Return the webSocketDebuggerUrl of the Roblox challenge WebView page."""
    url = f"http://{host_port}/json"
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                pages = json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(delay); continue
        for p in pages:
            u = (p.get("url") or "")
            if "challenge" in u or "arkose" in u or "funcaptcha" in u:
                ws = p.get("webSocketDebuggerUrl")
                if ws:
                    return ws, u
        time.sleep(delay)
    return None, None


def captcha_present_via_cdp(drv):
    """Is the Arkose challenge still up? True while the page URL still names a
    challenge; False once the app closes the WebView / navigates away."""
    try:
        href = drv.execute_script("return location.href")
        return bool(href) and ("challenge" in href or "arkose" in href
                               or "funcaptcha" in href)
    except Exception:
        return False   # page gone -> solved / closed


def solve(host_port, solver_base, token="", on_status=print,
          mode="step", card_off=None):
    """Attach to the WebView captcha at host_port and solve it.

    card_off optionally overrides visioncaptcha.OFF_CARD for the mobile layout
    (calibrated relative to the green button, in WebView CSS pixels)."""
    ws, u = find_captcha_page(host_port)
    if not ws:
        return {"ok": False, "reason": "no challenge WebView page found"}
    on_status(f"[mobile] attached to {u[:80]}")
    if card_off is not None:
        vc.OFF_CARD = card_off
    drv = WebViewDrv(ws)
    client = vc.SolverClient(solver_base, token=token)
    try:
        return vc.play_challenge(drv, client, on_status=on_status,
                                 is_present=captcha_present_via_cdp, mode=mode)
    finally:
        drv.close()
