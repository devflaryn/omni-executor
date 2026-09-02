"""Captcha solving service — the brain the client is NOT allowed to have.

Why a server at all: Arkose rotates its puzzle games, so the "which candidate
is right?" logic changes far more often than the client does. Keeping it here
means a new variant ships by restarting this process, not by pushing a build to
every install. It also keeps the model key off client machines, and every
request lands in one place where it can be logged into a training set.

Contract (see visioncaptcha.py for the caller):

    POST /v1/step
      {"screenshot": "<b64 png of the whole card>", "round": 1, "seen": 2}
    -> {"action": "submit", "reading": "dice total 33 matches"}
    -> {"action": "next"}
    -> {"action": "unsure", "reason": "..."}      # client asks a human

    POST /v1/feedback
      {"solve_id": "...", "advanced": true}       # ground truth for training

One picture, one decision. The client shows the card as it currently looks and
is told what a person would do next; it never learns WHY. That keeps every
puzzle-specific rule here, so a new Arkose game ships by restarting this
process rather than rebuilding every client.

The client sends PICTURES and receives an INDEX. It never learns how the answer
was reached, and it never talks to the model.
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Qwen over Gemini: Gemini 3.7 refuses this outright every 5-10 calls
# ("Sorry, I cannot assist with solving or bypassing CAPTCHA challenges"),
# which strands a 5-round puzzle that needs ~15 consecutive answers. Qwen
# agreed with Gemini on 6/6 replayed frames with no refusals, and costs about
# a third as much.
DEFAULT_MODEL = "qwen/qwen3.8-flash"
# Gemini 3.7 spends reasoning tokens BEFORE emitting any answer, and they count
# against max_tokens. A budget sized only for the JSON reply comes back
# truncated mid-object, which reads like a model failure but is ours.
MAX_TOKENS = 2500
REQUEST_TIMEOUT = 180
# Upstream hiccups are common enough to matter: OpenRouter answers HTTP 200
# with {"error": {"code": 504, "message": "The operation was aborted"}} in the
# BODY, which is not an exception and used to surface as "the model is unsure"
# - abandoning a challenge that was going fine. Transient means retry.
MODEL_RETRIES = 4
# Retries for a reply that arrived fine but did not parse - a different problem
# from an HTTP failure, and worth its own budget.
CONTENT_RETRIES = 3
RETRY_BACKOFF_S = 2.0
# A /v1/step must ANSWER inside the client's patience.
# visioncaptcha.SolverClient waits 200s, while the retry budget above can
# reach CONTENT_RETRIES * MODEL_RETRIES * REQUEST_TIMEOUT across the
# fallback models too -- over half an hour. So the client hung up while
# this server was still retrying, and the puzzle died with it. That was
# watched happening three rounds into a challenge that was going fine,
# which costs the whole account: solved rounds cannot be replayed.
# Retries are therefore bounded by WALL CLOCK rather than by a count, and
# the deadline sits below the client timeout so an answer -- even an
# "unsure" one, which merely hands the puzzle to the human -- always beats
# the hang-up.
STEP_DEADLINE_S = 150
TRANSIENT_CODES = (408, 409, 429, 500, 502, 503, 504)
# A rate limit is not a blip - the upstream is telling us to slow down, and the
# 2s/4s backoff that clears a 504 just burns the retries. Observed live:
# "qwen3.8-flash is temporarily rate-limited upstream" from Alibaba.
RATE_LIMIT_BACKOFF_S = 12.0
# If one model stays unavailable, try a sibling rather than strand a puzzle
# that is already several rounds in. Same prompt, same contract.
# Measured on replayed frames: 3.8-flash 6.5s median and 5/5 correct;
# qwen3-vl-32b 2.1s but 4/5; 3.7-flash 29.6s and 4/5 with one unusable reply.
# So the fast 32b is the fallback, not the primary - a wrong SUBMIT spends one
# of the few attempts Arkose grants, while a slow answer only costs seconds.
FALLBACK_MODELS = ("qwen/qwen3-vl-32b-instruct",)

# No hard vendor pin. It was Google Vertex while the model was Gemini; pinning
# a provider that does not serve the current model would fail every request.
PROVIDER_PREFS = {"allow_fallbacks": True}

MAX_IMAGE_BYTES = 3 * 1024 * 1024

# Credits live in the Node backend, which is also the only thing that verifies
# user tokens. This service holds a shared secret and FORWARDS the user's token;
# it never parses one itself, so identity has a single authority.
CREDITS_BASE = os.environ.get("OMNI_CREDITS_BASE", "").rstrip("/")
SERVICE_TOKEN = os.environ.get("CAPTCHA_SERVICE_TOKEN", "")
CREDITS_TIMEOUT = 15
MICROS_PER_DOLLAR = 1_000_000


class SolveError(Exception):
    """Something the caller should see verbatim.

    `transient` marks failures worth retrying (upstream timeouts, rate limits)
    as opposed to ones that will fail identically forever (a bad key, a
    malformed request)."""

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


# ---------------------------------------------------------------------------
# prompts, one per puzzle variant
# ---------------------------------------------------------------------------

STEP_PROMPT = (
    "You are looking at an Arkose FunCaptcha exactly as a person sees it. "
    "Read the instruction text in the image, then look at the answer that "
    "is CURRENTLY displayed. Decide ONE action. "
    "submit: the option now on screen satisfies the instruction. "
    "next: it does not, so a person would click the arrow for another. "
    "unsure: you cannot read or judge it. "
    "Judge ONLY the option on display, never a remembered one. If the "
    "instruction is arithmetic (summing dice pips, or the numbers on the "
    "top faces), compute it and compare exactly. Prefer next over a guess: "
    "a wrong submit spends one of the few attempts allowed, next costs "
    "nothing. Reply with STRICT JSON only, no prose and no code fence: "
    '{"action":"submit"|"next"|"unsure",'
    '"reading":"<what you saw, 12 words max>"}'
)


# ---------------------------------------------------------------------------
# the model call
# ---------------------------------------------------------------------------

def _data_url(b64):
    return {"type": "image_url",
            "image_url": {"url": "data:image/png;base64," + b64}}


def _extract_json(text):
    """Pull the JSON object out of a reply.

    The model wraps its answer in ```json fences often enough that a plain
    json.loads fails on perfectly good answers, so match the outermost braces
    instead of trusting the envelope."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def call_model(images, prompt, api_key, model=DEFAULT_MODEL, opener=None,
               timeout=REQUEST_TIMEOUT):
    """Send prompt + images, return (parsed_json, usage). Raises SolveError."""
    content = [{"type": "text", "text": prompt}] + [_data_url(b) for b in images]
    body = {
        "model": model,
        "provider": PROVIDER_PREFS,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
    }
    req = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "X-Title": "omni-captcha"})
    try:
        _open = opener or urllib.request.urlopen
        with _open(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - body is decoration
            pass
        raise SolveError(f"model HTTP {e.code}: {detail}",
                         transient=e.code in TRANSIENT_CODES) from e
    except Exception as e:  # noqa: BLE001
        raise SolveError(f"could not reach the model: {e}", transient=True) from e

    # An error can arrive INSIDE a 200 response; that is not an exception, so
    # it has to be looked for explicitly or it reads as a nonsense answer.
    err = payload.get("error")
    if err:
        code = (err or {}).get("code")
        raise SolveError(f"model error {code}: {(err or {}).get('message')}",
                         transient=code in TRANSIENT_CODES)
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise SolveError(f"unexpected model response: {json.dumps(payload)[:200]}",
                         transient=True) from e
    return _extract_json(text), payload.get("usage") or {}


def call_model_with_retries(images, prompt, api_key, model=DEFAULT_MODEL,
                            opener=None, retries=MODEL_RETRIES,
                            fallbacks=FALLBACK_MODELS, deadline=None):
    """call_model, but a transient upstream failure does not lose the puzzle.

    Rate limits get a much longer wait than other transients, because a 429 is
    the upstream asking for time rather than a blip to paper over. If a model
    stays unavailable through every retry, a sibling is tried: abandoning a
    puzzle four rounds in costs more than one extra call on another model.

    `deadline` is an absolute time.monotonic() past which no further attempt is
    STARTED and no backoff is slept off (see STEP_DEADLINE_S). Failing in time
    to answer beats retrying into a client that has already given up."""
    last = None
    for candidate in (model, *[f for f in (fallbacks or ()) if f != model]):
        for attempt in range(retries):
            budget = REQUEST_TIMEOUT
            if deadline is not None:
                budget = min(budget, deadline - time.monotonic())
                if budget <= 0:
                    raise last or SolveError("ran out of time before the model "
                                             "answered", transient=True)
            try:
                return call_model(images, prompt, api_key, candidate, opener,
                                  timeout=budget)
            except SolveError as e:
                last = e
                if not e.transient:
                    raise
                if attempt == retries - 1:
                    break          # this model is out; try the next one
                rate_limited = "429" in str(e)
                base = RATE_LIMIT_BACKOFF_S if rate_limited else RETRY_BACKOFF_S
                nap = base * (attempt + 1)
                # No point sleeping off a backoff there is no time to use.
                if deadline is not None and time.monotonic() + nap >= deadline:
                    break
                time.sleep(nap)
    raise last or SolveError("no model was attempted", transient=True)


VALID_ACTIONS = ("submit", "next", "unsure")


def decide_step(screenshot_b64, api_key, model=DEFAULT_MODEL, opener=None,
                deadline=None):
    """Judge the ONE option currently on screen. Returns (action, detail).

    One image per call, one decision per call. This replaced a version that
    captured all six carousel options up front and asked once: that needed the
    puzzle's layout hardcoded (where the target sits, where the candidates
    sit), so it only ever worked for the dice game. Sending the whole card and
    asking "is what I'm looking at right?" works for any variant Arkose serves,
    because the instruction the model reads is in the picture.

    It is also what a person does - look, reject, advance - and it costs less,
    since most rounds stop before all six are seen."""
    # An unparseable reply is usually a hiccup, not a verdict: the frame that
    # killed a 3-round-deep puzzle in testing replayed perfectly a minute later.
    # Ask again before giving up, because "unsure" costs the whole challenge.
    usage = {}
    # ONE deadline for the whole decision, re-asks included: the caller is a
    # browser sitting on a live challenge, not a batch job.
    if deadline is None:
        deadline = time.monotonic() + STEP_DEADLINE_S
    for attempt in range(CONTENT_RETRIES):
        if time.monotonic() >= deadline:
            return "unsure", {"reason": "out of time", "usage": usage}
        parsed, usage = call_model_with_retries([screenshot_b64], STEP_PROMPT,
                                                api_key, model, opener,
                                                deadline=deadline)
        if not parsed:
            continue
        action = str(parsed.get("action", "")).strip().lower()
        detail = {"reading": parsed.get("reading"), "usage": usage}
        if action in VALID_ACTIONS:
            return action, detail
        # A model that invents an action must never be read as a click.
        if attempt == CONTENT_RETRIES - 1:
            return "unsure", {**detail, "reason": f"unknown action {action!r}"}
    return "unsure", {"reason": "model did not return JSON", "usage": usage}


BATCH_PROMPT = (
    "This is an Arkose FunCaptcha. The FIRST image is the target/instruction "
    "panel. The images after it are the candidate answers, in order 1..N. "
    "Read the instruction, judge every candidate, and pick the one that "
    "satisfies it. If the instruction is arithmetic (summing the numbers on "
    "the dice top faces), compute each candidate exactly. Reply with STRICT "
    "JSON only, no prose and no code fence: "
    '{"sums":[n,...],"choice":K}'
    " where sums is your total per candidate (use null when the puzzle is not "
    "arithmetic) and K is the 1-based index of the answer, or null if unsure."
)


def decide_batch(target_b64, candidate_b64s, api_key, model=DEFAULT_MODEL,
                 opener=None):
    """Judge every candidate in ONE call. Returns (choice_or_None, detail).

    Roughly four times faster than asking option by option, because a round
    costs one request instead of one per candidate. The trade is that it
    needs the puzzle's LAYOUT - where the target sits, where a candidate
    sits - so it only fits the carousel games, whereas the per-step loop
    works on any variant by letting the model read the instruction itself.

    When the model reports per-candidate sums the choice is RECOMPUTED from
    them rather than trusted: arithmetic is the part it is worst at and the
    part we can actually check."""
    parsed = None
    usage = {}
    for _ in range(CONTENT_RETRIES):
        parsed, usage = call_model_with_retries(
            [target_b64] + list(candidate_b64s), BATCH_PROMPT, api_key, model,
            opener)
        if parsed:
            break
    if not parsed:
        return None, {"reason": "model did not return JSON", "usage": usage}
    choice = parsed.get("choice")
    sums = parsed.get("sums")
    detail = {"sums": sums, "model_choice": choice, "usage": usage}
    if isinstance(sums, list) and sums and isinstance(choice, int) \
            and 1 <= choice <= len(sums) and isinstance(sums[choice - 1], (int, float)):
        target = sums[choice - 1]
        matches = [i + 1 for i, v in enumerate(sums) if v == target]
        if len(matches) != 1:
            # Two candidates sharing the target sum means a misread, and a
            # wrong submit spends one of the few attempts Arkose allows.
            return None, {**detail, "reason":
                          f"{len(matches)} candidates share the target sum"}
        return matches[0], detail
    if isinstance(choice, int) and 1 <= choice <= len(candidate_b64s):
        return choice, {**detail, "unverified": True}
    return None, {**detail, "reason": "no usable choice"}


def handle_solve(payload, api_key, model=DEFAULT_MODEL, dataset_root=None,
                 opener=None, authorize=None, charge=None):
    """Batch sibling of handle_step: one call for a whole round."""
    cands = payload.get("candidates")
    if not isinstance(cands, list) or not cands:
        return {"choice": None, "reason": "candidates must be a non-empty list"}, 400
    if not payload.get("target"):
        return {"choice": None, "reason": "target is required"}, 400
    user_token = payload.get("userToken") or ""
    _authorize = authorize or authorize_user
    allowed, balance, _note = _authorize(user_token)
    if not allowed:
        return {"choice": None, "reason": "insufficient_credits",
                "balanceMicros": balance or 0}, 200
    started = time.time()
    try:
        choice, detail = decide_batch(payload["target"], cands, api_key, model,
                                      opener)
    except SolveError as e:
        return {"choice": None, "reason": str(e)}, 502
    sid = log_step(dataset_root,
                   {"screenshot": payload["target"], "round": payload.get("round"),
                    "seen": 0},
                   f"batch:{choice}", detail)
    _charge = charge or charge_user
    billed = _charge(user_token, detail.get("usage"), {"solve_id": sid, "model": model})
    out = {"choice": choice, "sums": detail.get("sums"),
           "elapsed": round(time.time() - started, 2), "solve_id": sid}
    if choice is None:
        out["reason"] = detail.get("reason", "no confident answer")
    if billed:
        out["balanceMicros"] = billed.get("balanceMicros")
    return out, 200


# ---------------------------------------------------------------------------
# metering
# ---------------------------------------------------------------------------

def _credits_post(path, payload):
    req = urllib.request.Request(
        f"{CREDITS_BASE}{path}", data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json",
                                "X-Service-Token": SERVICE_TOKEN})
    with urllib.request.urlopen(req, timeout=CREDITS_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def authorize_user(user_token):
    """May this user run one more step? Returns (allowed, balance_micros, note).

    With metering switched off (no CREDITS_BASE) everything is allowed, so a
    self-hosted deployment does not need an accounts backend at all.

    A metering outage FAILS OPEN. The alternative is that a blip in the billing
    service stops every customer solving captchas, which costs far more good
    will than the fraction of a cent it protects."""
    if not CREDITS_BASE:
        return True, None, "metering off"
    try:
        d = (_credits_post("/api/v1/credits/internal/authorize",
                           {"userToken": user_token or ""}) or {}).get("data") or {}
        return bool(d.get("allowed")), d.get("balanceMicros"), None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, 0, "not signed in"
        return True, None, f"metering unavailable ({e.code})"
    except Exception as e:  # noqa: BLE001
        return True, None, f"metering unreachable ({type(e).__name__})"


def charge_user(user_token, usage, meta=None):
    """Bill what the model cost us, at the backend's markup.

    Charged if and only if the provider reported a cost: a policy refusal from
    the model still costs money upstream and is billed, while a gateway timeout
    that returns no usage is free because we were not charged either."""
    if not CREDITS_BASE:
        return None
    cost = (usage or {}).get("cost")
    micros = int(round(float(cost or 0) * MICROS_PER_DOLLAR))
    if micros <= 0:
        return None
    try:
        return (_credits_post("/api/v1/credits/internal/charge",
                              {"userToken": user_token or "",
                               "upstreamCostMicros": micros,
                               "meta": meta or {}}) or {}).get("data")
    except Exception as e:  # noqa: BLE001 - never fail a solve over billing
        print(f"[captcha] charge failed: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# dataset: every request is a future training example
# ---------------------------------------------------------------------------

def log_step(root, payload, action, detail):
    """Persist one decision: the picture, what we decided, and why.

    Never raises - losing a training sample must not fail a solve. Paired with
    record_outcome(), each row becomes a supervised example whose label came
    from the puzzle itself rather than from the model's own opinion."""
    if not root:
        return None
    try:
        sid = uuid.uuid4().hex[:16]
        d = Path(root)
        d.mkdir(parents=True, exist_ok=True)
        raw = base64.b64decode(payload["screenshot"])
        # Name it by what it actually IS. The client switched to JPEG captures
        # and these files are training data - a jpeg called .png will trip up
        # whatever reads the set later.
        ext = "jpg" if raw[:2] == bytes([0xFF, 0xD8]) else "png"
        (d / f"{sid}.{ext}").write_bytes(raw)
        rec = {"id": sid, "at": time.time(), "file": f"{sid}.{ext}",
               "round": payload.get("round"), "seen": payload.get("seen"),
               "action": action, "reading": detail.get("reading"),
               "reason": detail.get("reason")}
        with (d / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + chr(10))
        return sid
    except Exception:  # noqa: BLE001 - logging is never worth a failed solve
        return None


def record_outcome(root, solve_id, advanced):
    """The client tells us whether the click actually worked. THIS is the
    ground-truth label — the model's opinion is only a guess until the puzzle
    confirms it, and a verified label is what a distilled model can learn from
    later."""
    if not root or not solve_id:
        return False
    try:
        with (Path(root) / "outcomes.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": solve_id, "at": time.time(),
                                "advanced": bool(advanced)}) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# validation + dispatch
# ---------------------------------------------------------------------------

def validate_payload(payload):
    """Returns error string, or None. Rejects before spending a model call."""
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    shot = payload.get("screenshot")
    if not shot:
        return "screenshot is required"
    if not isinstance(shot, str):
        return "screenshot must be a base64 string"
    if len(shot) > MAX_IMAGE_BYTES:
        return "screenshot is too large"
    try:
        base64.b64decode(shot, validate=True)
    except Exception:  # noqa: BLE001
        return "screenshot is not valid base64"
    return None


def handle_step(payload, api_key, model=DEFAULT_MODEL, dataset_root=None,
                opener=None, authorize=None, charge=None):
    """Pure request -> response. Kept free of HTTP so it is directly testable."""
    err = validate_payload(payload)
    if err:
        return {"action": "unsure", "reason": err}, 400

    user_token = payload.get("userToken") or ""
    _authorize = authorize or authorize_user
    allowed, balance, note = _authorize(user_token)
    if not allowed:
        # Out of credit is not an error: the client falls back to the human,
        # who solves it in the window exactly as before. Account creation keeps
        # working, it is just slower.
        return {"action": "unsure", "reason": "insufficient_credits",
                "balanceMicros": balance or 0}, 200

    started = time.time()
    try:
        action, detail = decide_step(payload["screenshot"], api_key, model,
                                     opener)
    except SolveError as e:
        return {"action": "unsure", "reason": str(e)}, 502
    sid = log_step(dataset_root, payload, action, detail)
    _charge = charge or charge_user
    billed = _charge(user_token, detail.get("usage"), {"solve_id": sid,
                                                       "model": model})
    out = {"action": action, "reading": detail.get("reading"),
           "elapsed": round(time.time() - started, 2), "solve_id": sid}
    if billed:
        out["balanceMicros"] = billed.get("balanceMicros")
    if action == "unsure" and detail.get("reason"):
        out["reason"] = detail["reason"]
    return out, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "omni-captcha/1.0"
    api_key = ""
    model = DEFAULT_MODEL
    dataset_root = None
    auth_token = ""

    def log_message(self, fmt, *args):   # quieter than the default
        print(f"[captcha] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not self.auth_token:
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {self.auth_token}"

    def do_GET(self):
        if self.path == "/healthz":
            return self._send({"ok": True, "model": self.model})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._send({"action": "unsure", "reason": "unauthorized"}, 401)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return self._send({"action": "unsure", "reason": "bad JSON"}, 400)

        if self.path == "/v1/solve":
            out, code = handle_solve(payload, self.api_key, self.model,
                                     self.dataset_root)
            return self._send(out, code)
        if self.path == "/v1/step":
            out, code = handle_step(payload, self.api_key, self.model,
                                    self.dataset_root)
            return self._send(out, code)
        if self.path == "/v1/feedback":
            ok = record_outcome(self.dataset_root, payload.get("solve_id"),
                                payload.get("advanced"))
            return self._send({"ok": ok})
        self._send({"error": "not found"}, 404)


def serve(host="127.0.0.1", port=8788, api_key=None, model=DEFAULT_MODEL,
          dataset_root=None, auth_token=""):
    Handler.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    Handler.model = model
    Handler.dataset_root = dataset_root
    Handler.auth_token = auth_token
    if not Handler.api_key:
        raise SystemExit("no OPENROUTER_API_KEY set")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"[captcha] serving on http://{host}:{port} model={model} "
          f"dataset={dataset_root or 'off'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dataset", default=None,
                    help="directory to log solves into (training data)")
    ap.add_argument("--token", default="", help="require this bearer token")
    a = ap.parse_args()
    serve(a.host, a.port, model=a.model, dataset_root=a.dataset,
          auth_token=a.token)
