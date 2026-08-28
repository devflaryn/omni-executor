"""Client half of the captcha solver: eyes and hands, no brain.

This module PLAYS the Arkose puzzle in the user's own browser. It does not ask
for a token, because no third-party service can get one: Arkose refuses to
issue a challenge at all to a solver's IP (the card hangs on "Verifying
browser..." forever), while it serves one normally to the person actually
signing up. So the only place the puzzle can be solved is right here.

Division of labour, deliberately lopsided:

  * THIS FILE knows geometry — where the card is, which pixel to click, how to
    cycle the carousel. That has to live client-side because only the client
    can measure its own window, DPI and the card's floating position.
  * THE SERVER knows the answer. It is shown the card as it currently looks
    and replies with what a person would do next - submit, next, or unsure.
    New puzzle variants ship by restarting the server, not by rebuilding this,
    because the instruction the model follows is inside the picture.

The card is located by finding its green action button by COLOUR rather than by
fixed coordinates: it is inside a cross-origin iframe (no DOM access) and it
does not sit in the same place twice.
"""
import base64
import json
import random
import time
import urllib.error
import urllib.request

# Offsets from the green button's centre, measured on the live Roblox card at
# 1x device pixel ratio, so the geometry travels with the card when it moves.
# The capture is the WHOLE card, instruction included. An earlier version cropped the target
# and each candidate separately, which meant hardcoding the dice game's layout
# and broke on every other Arkose variant. Sending the card as the user sees it
# lets the model read the instruction itself, so new games need no client change.
OFF_CARD = (-205, -385, 205, 80)         # x1, y1, x2, y2 from the green button
OFF_RIGHT_ARROW = (151, -59)
# BATCH mode crops the target and each candidate separately, so it needs the
# carousel's layout - which is why it only fits the dice-style games, while the
# per-step loop works on any variant by letting the model read the instruction.
OFF_TARGET = (-173, -283, -42, -86)
OFF_CHOICE = (-25, -283, 173, -86)

# EXACTLY one full cycle. It was 8 "for slack", but the carousel wraps at 6, so
# options 7 and 8 just re-photographed options 1 and 2 and paid to be told the
# same thing twice. If a full pass finds no match, the answer was misread, and
# seeing the same frames again does not fix that - RETRY_SCALE does.
MAX_OPTIONS = 6
MAX_ROUNDS = 8               # the card says "1 of 5"; the extra is slack
SHOT_SCALE = 2               # 2x: the dice numerals are small and it matters
# The card goes to the model as JPEG, not PNG. A 2x PNG of this card is ~327 kB;
# the same frame at q82 is a fraction of that, which cuts both the upload and
# the image-token count the model pays to read. Quality stays high enough that
# 12-vs-2 and 11-vs-1 remain distinguishable - the whole puzzle turns on that.
CARD_FORMAT = "jpeg"
CARD_QUALITY = 82
# A second pass at higher magnification when a whole cycle found nothing. The
# dice carry two-digit numbers and 12-vs-2 and 11-vs-1 are one blurred pixel
# apart; a miss usually means one die was miscounted, not that the puzzle was
# unsolvable. Observed: "top faces sum to about 33 or 42" - the model saying so.
RETRY_SCALE = 3
GREEN_MIN_PIXELS = 40
SETTLE_TIMEOUT = 75
# Each carousel step fetches a NEW photo over the network - and through a
# residential proxy that is not instant. Trimmed to 0.7s for speed, this handed
# the model half-loaded images: a whole round read 33/30/31/30/"11"/32 against a
# target of 34, twice, because the answer was never fully drawn when photographed.
CLICK_SETTLE_S = 1.3
POST_SUBMIT_S = 6.0          # a fresh round has to fetch new images
# The model tells us when it is looking at a half-drawn card. Treating that as
# "I give up" throws away a puzzle that is merely still loading, so those words
# buy a short wait and another look instead.
LOADING_WORDS = ("loading", "spinner", "blank", "not loaded", "still load")
LOADING_RETRIES = 4
LOADING_WAIT_S = 4.0

# Find the largest green region in a screenshot. Runs in the page because the
# screenshot arrives as a same-origin data: URL, so getImageData is allowed —
# no Pillow, which will not build on this project's Python.
FIND_GREEN_JS = r"""
const dataUrl = arguments[0];
return new Promise(resolve => {
  const img = new Image();
  img.onload = () => {
    const c = document.createElement('canvas');
    c.width = img.width; c.height = img.height;
    const x = c.getContext('2d'); x.drawImage(img, 0, 0);
    const d = x.getImageData(0, 0, c.width, c.height).data;
    let n=0, sx=0, sy=0, minx=1e9, miny=1e9, maxx=-1, maxy=-1;
    for (let y=0; y<c.height; y+=2) {
      for (let px=0; px<c.width; px+=2) {
        const i=(y*c.width+px)*4, r=d[i], g=d[i+1], b=d[i+2];
        if (g>130 && g<210 && r>50 && r<130 && b>50 && b<130 && g-r>45 && g-b>45) {
          n++; sx+=px; sy+=y;
          if(px<minx)minx=px; if(px>maxx)maxx=px;
          if(y<miny)miny=y; if(y>maxy)maxy=y;
        }
      }
    }
    resolve(JSON.stringify(n < 40 ? {found:false, n:n}
      : {found:true, n:n, cx:Math.round(sx/n), cy:Math.round(sy/n),
         box:[minx,miny,maxx,maxy]}));
  };
  img.onerror = () => resolve(JSON.stringify({found:false}));
  img.src = dataUrl;
});
"""


class VisionCaptchaError(Exception):
    pass


# ---------------------------------------------------------------------------
# browser primitives
# ---------------------------------------------------------------------------

def screenshot(drv, clip=None, fmt="png", quality=None, scale=1):
    """CDP screenshot, optionally cropped. Returns base64 (no data: prefix)."""
    args = {"format": fmt, "captureBeyondViewport": False}
    if quality is not None:
        args["quality"] = quality
    if clip:
        x1, y1, x2, y2 = clip
        args["clip"] = {"x": x1, "y": y1, "width": x2-x1, "height": y2-y1,
                        "scale": scale}
    return drv.execute_cdp_cmd("Page.captureScreenshot", args)["data"]


def click_at(drv, x, y):
    """A real mouse click at viewport coordinates.

    Dispatched through CDP rather than Selenium's element click, because the
    target is inside a cross-origin iframe that Selenium cannot address. The
    move-before-press matters: Arkose watches pointer movement, and a press
    with no preceding motion is a bot signature."""
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        drv.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": kind, "x": float(x), "y": float(y), "button": "left",
            "clickCount": 0 if kind == "mouseMoved" else 1})
        time.sleep(0.06)


def find_green_button(drv, tries=35, delay=2.0):
    """The card's action button ("Start Puzzle", then "Submit"), by colour."""
    for _ in range(tries):
        url = "data:image/jpeg;base64," + screenshot(drv, fmt="jpeg", quality=70)
        try:
            r = json.loads(drv.execute_script(
                "return (async()=>{" + FIND_GREEN_JS + "})()", url))
        except Exception:  # noqa: BLE001 - a mid-render frame; try again
            r = {"found": False}
        if r.get("found"):
            return r
        time.sleep(delay)
    return None


def wait_until_settled(drv, timeout=SETTLE_TIMEOUT, settle=3, poll=1.5):
    """Wait for the puzzle to finish painting.

    The card shows "Verifying browser..." for many seconds before the puzzle
    appears, and a capture taken then is an unreadable spinner — which is
    exactly what got sent to a solver on the first attempt at this. Frame-size
    stability is the signal: a spinner animates, a painted puzzle does not."""
    min_wait = 6 if timeout > 20 else 1.5
    prev, stable, t0 = None, 0, time.monotonic()
    while time.monotonic() - t0 < timeout:
        size = len(base64.b64decode(screenshot(drv, fmt="jpeg", quality=50)))
        if prev is not None and abs(size - prev) < 900:
            stable += 1
            if stable >= settle and time.monotonic() - t0 > min_wait:
                return True
        else:
            stable = 0
        prev = size
        time.sleep(poll)
    return False


def box_from(anchor, off):
    cx, cy = anchor
    return (cx + off[0], cy + off[1], cx + off[2], cy + off[3])


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------

class SolverClient:
    """Talks to the captcha service. Knows nothing about models."""

    def __init__(self, base_url, token="", timeout=200.0, opener=None):
        self.base = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self._open = opener or urllib.request.urlopen

    def _post(self, path, payload):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(payload).encode("utf-8"),
            method="POST", headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                raise VisionCaptchaError(f"solver HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001
            raise VisionCaptchaError(f"could not reach the solver: {e}") from e

    def step(self, screenshot_b64, round_no=1, seen=1):
        """Show the card as it looks now; get back what a person would do."""
        return self._post("/v1/step", {"screenshot": screenshot_b64,
                                       "round": round_no, "seen": seen})

    def solve(self, target_b64, candidates_b64, round_no=1):
        """Show the target and every candidate at once; get back an index."""
        return self._post("/v1/solve", {"target": target_b64,
                                        "candidates": list(candidates_b64),
                                        "round": round_no})

    def feedback(self, solve_id, advanced):
        if not solve_id:
            return {"ok": False}
        try:
            return self._post("/v1/feedback",
                              {"solve_id": solve_id, "advanced": bool(advanced)})
        except VisionCaptchaError:
            return {"ok": False}      # telemetry must never break a signup


# ---------------------------------------------------------------------------
# playing the puzzle
# ---------------------------------------------------------------------------

def _human_pause(base, jitter=0.35):
    """Sleep like a person looking at something, not like a loop.

    Arkose scores pointer and timing behaviour, and perfectly uniform gaps
    between clicks are one of the cheapest bot signals there is."""
    time.sleep(max(0.15, base + random.uniform(-jitter, jitter)))


def card_shot(drv, anchor, scale=SHOT_SCALE):
    """The card exactly as the user sees it."""
    return screenshot(drv, clip=box_from(anchor, OFF_CARD), scale=scale,
                      fmt=CARD_FORMAT, quality=CARD_QUALITY)


def _play_round_batch(drv, client, anchor, arrow, rnd, on_status, scale):
    """Photograph the target and all six candidates, then ask once.

    One request per round instead of one per candidate - about four times
    faster. Ends on candidate 1 again, so the returned index counts from a
    known position."""
    target = screenshot(drv, clip=box_from(anchor, OFF_TARGET), scale=scale,
                        fmt=CARD_FORMAT, quality=CARD_QUALITY)
    shots = []
    for i in range(MAX_OPTIONS):
        shots.append(screenshot(drv, clip=box_from(anchor, OFF_CHOICE),
                                scale=scale, fmt=CARD_FORMAT,
                                quality=CARD_QUALITY))
        click_at(drv, *arrow)
        time.sleep(CLICK_SETTLE_S)
    res = client.solve(target, shots, rnd)
    choice = res.get("choice")
    on_status(f"[vision] round {rnd}: candidate {choice} (sums {res.get('sums')})")
    if not choice:
        return None, res.get("reason") or "solver was unsure"
    for _ in range(choice - 1):      # the carousel wrapped back to candidate 1
        click_at(drv, *arrow)
        time.sleep(CLICK_SETTLE_S)
    return res, None


def play_challenge(drv, client, on_status=lambda m: None,
                   stop_check=lambda: False, is_present=None, mode="step"):
    """Solve the visible Arkose challenge by playing it. Returns a dict:

        {"ok": bool, "rounds": int, "reason": str|None}

    The loop is deliberately the one a person runs: look at what is on screen,
    ask "is this it?", and either submit or advance. It stops at the first
    match instead of surveying all six options, which is both cheaper and less
    machine-like than the batch version this replaced.

    `is_present(drv)` reports whether the challenge is still up; injected so
    this module does not import accountcreator (which imports it)."""
    if is_present is None:
        import accountcreator
        is_present = accountcreator.captcha_present

    start = find_green_button(drv)
    if not start:
        return {"ok": False, "rounds": 0, "reason": "no action button on the card"}
    on_status("[vision] starting the puzzle")
    click_at(drv, start["cx"], start["cy"])
    time.sleep(6)

    rounds = 0
    for rnd in range(1, MAX_ROUNDS + 1):
        if stop_check():
            return {"ok": False, "rounds": rounds, "reason": "cancelled"}
        if not is_present(drv):
            return {"ok": True, "rounds": rounds, "reason": None}

        # Only the first round waits out the "Verifying browser..." screen.
        # Later rounds repaint in place, so the full settle just added ~10s per
        # round to a puzzle that already takes minutes.
        if rnd == 1:
            wait_until_settled(drv)
        else:
            # Not as tight as it once was: trimmed too far, this handed the
            # model an unpainted round and it said so - "Loading spinners are
            # displayed instead of dice."
            wait_until_settled(drv, timeout=30, settle=3, poll=1.0)
        g = find_green_button(drv, tries=20, delay=1.5)
        if not g:
            return {"ok": False, "rounds": rounds,
                    "reason": "the Submit button vanished"}
        anchor = (g["cx"], g["cy"])
        arrow = (anchor[0] + OFF_RIGHT_ARROW[0], anchor[1] + OFF_RIGHT_ARROW[1])

        if mode == "batch":
            res, why = _play_round_batch(drv, client, anchor, arrow, rnd,
                                         on_status, SHOT_SCALE)
            if not res:
                return {"ok": False, "rounds": rounds, "reason": why}
            _human_pause(0.6)
            g2 = find_green_button(drv, tries=6, delay=1.0) or g
            click_at(drv, g2["cx"], g2["cy"])
            client.feedback(res.get("solve_id"), True)
            rounds += 1
            time.sleep(POST_SUBMIT_S)
            continue

        submitted = False
        loading_waits = 0
        # Two passes at most: the second re-reads the same options larger.
        for attempt, scale in enumerate((SHOT_SCALE, RETRY_SCALE)):
            seen = 0
            while seen < MAX_OPTIONS:
                seen += 1
                if stop_check():
                    return {"ok": False, "rounds": rounds, "reason": "cancelled"}
                try:
                    res = client.step(card_shot(drv, anchor, scale), rnd, seen)
                except VisionCaptchaError as e:
                    return {"ok": False, "rounds": rounds, "reason": str(e)}
                action = res.get("action")
                on_status(f"[vision] round {rnd} option {seen}"
                          f"{' (recheck)' if attempt else ''}: {action}"
                          f" ({res.get('reading') or ''})")

                if action == "submit":
                    g2 = find_green_button(drv, tries=6, delay=1.0) or g
                    _human_pause(0.6)
                    click_at(drv, g2["cx"], g2["cy"])
                    client.feedback(res.get("solve_id"), True)
                    submitted = True
                    break
                if action == "next":
                    client.feedback(res.get("solve_id"), False)
                    click_at(drv, *arrow)
                    _human_pause(1.0)
                    continue
                # "unsure" for a card that has not finished drawing is not a
                # verdict - wait and look again rather than abandoning it.
                reading = str(res.get("reading") or "").lower()
                if any(w in reading for w in LOADING_WORDS) and loading_waits < LOADING_RETRIES:
                    loading_waits += 1
                    on_status(f"[vision] card still loading; waiting "
                              f"({loading_waits}/{LOADING_RETRIES})")
                    time.sleep(LOADING_WAIT_S)
                    seen -= 1        # same option, another look
                    continue
                # A real "I cannot judge this": a wrong submit spends one of
                # the few attempts Arkose allows, so the human finishes it.
                return {"ok": False, "rounds": rounds,
                        "reason": res.get("reason") or "solver was unsure"}
            if submitted:
                break
            if attempt == 0:
                on_status(f"[vision] round {rnd}: no match in a full pass — "
                          f"re-reading at {RETRY_SCALE}x")

        if not submitted:
            return {"ok": False, "rounds": rounds,
                    "reason": "no option matched the instruction"}
        rounds += 1
        time.sleep(POST_SUBMIT_S)

    return {"ok": not is_present(drv), "rounds": rounds,
            "reason": None if not is_present(drv) else "ran out of rounds"}
