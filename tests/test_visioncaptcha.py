"""The captcha service and its client, without a browser or a model.

Everything here is the logic that decides whether we CLICK or hand the puzzle
to a human — the expensive mistake is a confident wrong click, because Arkose
allows only a few attempts per challenge.
"""
import base64
import json
import io

import pytest

import captchaserver as cs
import visioncaptcha as vc


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 200).decode()


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _model(reply_text, usage=None):
    """An opener that returns one canned model reply."""
    def opener(req, timeout=None):
        return _Resp({"choices": [{"message": {"content": reply_text}}],
                      "usage": usage or {}})
    return opener


# ------------------------------------------------------------------ parsing

def test_extract_json_survives_markdown_fences():
    """The model wraps answers in ```json often enough that a bare json.loads
    would reject perfectly good replies."""
    assert cs._extract_json('```json\n{"choice": 2}\n```') == {"choice": 2}
    assert cs._extract_json('{"choice": 2}') == {"choice": 2}
    assert cs._extract_json("no json here") is None
    assert cs._extract_json("") is None


def test_extract_json_takes_the_object_out_of_prose():
    assert cs._extract_json('Sure! {"choice": 3} hope that helps') == {"choice": 3}


# ------------------------------------------------------- choosing an answer

# ------------------------------------------------------------- the request

def test_call_model_keeps_provider_fallbacks_on():
    """A hard vendor pin turns one region's outage into a dead solver, and a
    pin left over from a previous model would fail every request outright."""
    assert "order" not in cs.PROVIDER_PREFS or cs.PROVIDER_PREFS["order"]
    assert cs.PROVIDER_PREFS["allow_fallbacks"] is True


def test_call_model_budgets_for_reasoning_tokens():
    """Gemini 3.7 spends reasoning tokens BEFORE any answer and they count
    against max_tokens; a budget sized for the JSON alone returns truncated
    mid-object, which looks like a model failure but is ours."""
    assert cs.MAX_TOKENS >= 2000


def test_call_model_sends_every_image_after_the_prompt():
    seen = {}

    def opener(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _Resp({"choices": [{"message": {"content": '{"choice":1}'}}]})

    cs.call_model([PNG, PNG, PNG], "do the thing", "key", opener=opener)
    content = seen["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert [c["type"] for c in content[1:]] == ["image_url"] * 3
    assert seen["body"]["temperature"] == 0


def test_call_model_reports_http_errors_verbatim():
    import urllib.error

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(
            "u", 402, "Payment Required", {}, io.BytesIO(b'{"error":"no credit"}'))

    with pytest.raises(cs.SolveError, match="402"):
        cs.call_model([PNG], "p", "key", opener=opener)


# --------------------------------------------------------------- validation

# ----------------------------------------------------------------- dataset

def test_record_outcome_captures_the_ground_truth_label(tmp_path):
    """Whether the click actually advanced the puzzle is the only real label —
    the model's opinion is a guess until the puzzle confirms it."""
    assert cs.record_outcome(tmp_path, "abc", True) is True
    rec = json.loads((tmp_path / "outcomes.jsonl").read_text(encoding="utf-8"))
    assert rec["id"] == "abc" and rec["advanced"] is True


# ------------------------------------------------------------- the client

def test_feedback_never_breaks_a_signup():
    """Telemetry failing must not cost an account."""
    def opener(req, timeout=None):
        raise OSError("refused")

    assert vc.SolverClient("http://x", opener=opener).feedback("id", True) == {"ok": False}
    assert vc.SolverClient("http://x", opener=opener).feedback(None, True) == {"ok": False}


# ------------------------------------------------------------- geometry

def test_capture_scale_is_2x():
    """The dice numerals are small; 1x captures were the difference between a
    readable digit and a guess."""
    assert vc.SHOT_SCALE == 2


# --------------------------------------------------- per-step decision loop

def test_decide_step_maps_each_action():
    for reply, want in (('{"action":"submit","reading":"33 matches"}', "submit"),
                        ('{"action":"next"}', "next"),
                        ('{"action":"unsure"}', "unsure")):
        action, _ = cs.decide_step(PNG, "k", opener=_model(reply))
        assert action == want


def test_decide_step_treats_an_unknown_action_as_unsure():
    """A model that invents an action must not be interpreted as a click."""
    action, detail = cs.decide_step(PNG, "k", opener=_model('{"action":"click_it"}'))
    assert action == "unsure" and "unknown action" in detail["reason"]


def test_decide_step_is_unsure_on_unparseable_replies():
    action, detail = cs.decide_step(PNG, "k", opener=_model("no idea sorry"))
    assert action == "unsure" and "did not return JSON" in detail["reason"]


def test_decide_step_sends_exactly_one_image():
    """One picture per decision - the whole card, so the model reads the
    instruction itself instead of the client hardcoding a puzzle's layout."""
    seen = {}

    def opener(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _Resp({"choices": [{"message": {"content": '{"action":"next"}'}}]})

    cs.decide_step(PNG, "key", opener=opener)
    content = seen["body"]["messages"][0]["content"]
    assert [c["type"] for c in content] == ["text", "image_url"]


def test_step_prompt_prefers_next_over_guessing():
    """A wrong submit spends one of the few attempts Arkose allows; skipping
    costs nothing, so the prompt must bias that way."""
    assert "Prefer next over a guess" in cs.STEP_PROMPT
    assert "STRICT JSON" in cs.STEP_PROMPT


@pytest.mark.parametrize("payload,fragment", [
    ({}, "screenshot is required"),
    ({"screenshot": 123}, "base64 string"),
    ({"screenshot": "not base64!!"}, "not valid base64"),
])
def test_validate_payload_rejects_before_spending_a_model_call(payload, fragment):
    err = cs.validate_payload(payload)
    assert err and fragment in err


def test_validate_payload_accepts_a_screenshot():
    assert cs.validate_payload({"screenshot": PNG, "round": 1}) is None


def test_handle_step_happy_path_and_errors():
    out, code = cs.handle_step({"screenshot": PNG}, "k",
                               opener=_model('{"action":"submit","reading":"ok"}'))
    assert code == 200 and out["action"] == "submit" and out["reading"] == "ok"

    out, code = cs.handle_step({}, "k")
    assert code == 400 and out["action"] == "unsure"

    def dead(req, timeout=None):
        raise OSError("refused")
    out, code = cs.handle_step({"screenshot": PNG}, "k", opener=dead)
    assert code == 502 and out["action"] == "unsure"


def test_log_step_writes_the_card_and_an_index(tmp_path):
    sid = cs.log_step(tmp_path, {"screenshot": PNG, "round": 2, "seen": 3},
                      "next", {"reading": "sum 31"})
    assert sid and (tmp_path / f"{sid}.png").exists()
    rec = json.loads((tmp_path / "index.jsonl").read_text(encoding="utf-8"))
    assert rec["action"] == "next" and rec["seen"] == 3


def test_client_step_posts_the_card():
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp({"action": "next"})

    c = vc.SolverClient("http://x:8788", opener=opener)
    assert c.step(PNG, 2, 3)["action"] == "next"
    assert seen["url"].endswith("/v1/step")
    assert seen["body"]["round"] == 2 and seen["body"]["seen"] == 3


def test_card_capture_covers_the_whole_card():
    """The crop must include the instruction line above the images, or the
    model has nothing to read and every variant becomes a guess."""
    box = vc.box_from((584, 524), vc.OFF_CARD)
    assert box[0] < 584 - 190 and box[1] < 524 - 370      # left/top of the card
    assert box[2] > 584 + 190 and box[3] > 524            # right/below Submit


def test_human_pause_is_never_zero_and_varies(monkeypatch):
    """Uniform gaps between clicks are a cheap bot signal."""
    slept = []
    monkeypatch.setattr(vc.time, "sleep", slept.append)
    for _ in range(20):
        vc._human_pause(1.0)
    assert all(s >= 0.15 for s in slept)
    assert len(set(slept)) > 1


def test_error_inside_a_200_response_is_detected():
    """OpenRouter returns {"error": {"code": 504}} with HTTP 200. Treating that
    as a normal reply surfaced as "the model is unsure" and abandoned a
    challenge that was going fine."""
    def opener(req, timeout=None):
        return _Resp({"error": {"code": 504, "message": "The operation was aborted"}})

    with pytest.raises(cs.SolveError) as ei:
        cs.call_model([PNG], "p", "k", opener=opener)
    assert "504" in str(ei.value) and ei.value.transient is True


def test_transient_failures_are_retried_then_succeed():
    calls = {"n": 0}

    def opener(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp({"error": {"code": 504, "message": "aborted"}})
        return _Resp({"choices": [{"message": {"content": '{"action":"next"}'}}]})

    parsed, _ = cs.call_model_with_retries([PNG], "p", "k", opener=opener)
    assert parsed == {"action": "next"} and calls["n"] == 3


def test_permanent_failures_are_not_retried():
    """A bad key fails identically forever; retrying just wastes time."""
    import urllib.error
    calls = {"n": 0}

    def opener(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error":"bad key"}'))

    with pytest.raises(cs.SolveError):
        cs.call_model_with_retries([PNG], "p", "k", opener=opener)
    assert calls["n"] == 1


# ------------------------------------------------------------- metering

def test_insufficient_credits_hands_the_puzzle_to_a_human():
    """Out of credit is not an error. The client already does the right thing
    with 'unsure' - leave the browser open - so signup keeps working, slower."""
    out, code = cs.handle_step({"screenshot": PNG}, "k",
                               authorize=lambda t: (False, 0, None))
    assert code == 200
    assert out["action"] == "unsure" and out["reason"] == "insufficient_credits"


def test_no_model_call_is_made_when_the_user_cannot_pay():
    called = {"n": 0}

    def opener(req, timeout=None):
        called["n"] += 1
        return _Resp({"choices": [{"message": {"content": '{"action":"next"}'}}]})

    cs.handle_step({"screenshot": PNG}, "k", opener=opener,
                   authorize=lambda t: (False, 0, None))
    assert called["n"] == 0


def test_a_solve_is_billed_with_what_it_cost_upstream():
    billed = {}

    def charge(token, usage, meta):
        billed["usage"] = usage
        billed["meta"] = meta
        return {"balanceMicros": 7_000_000}

    out, code = cs.handle_step(
        {"screenshot": PNG, "userToken": "jwt"}, "k",
        opener=_model('{"action":"submit"}', usage={"cost": 0.0015}),
        authorize=lambda t: (True, 10_000_000, None), charge=charge)
    assert code == 200 and out["action"] == "submit"
    assert billed["usage"]["cost"] == 0.0015
    assert out["balanceMicros"] == 7_000_000


def test_metering_is_optional(monkeypatch):
    """A self-hosted deployment with no accounts backend must still solve."""
    monkeypatch.setattr(cs, "CREDITS_BASE", "")
    allowed, balance, note = cs.authorize_user("")
    assert allowed is True and note == "metering off"
    assert cs.charge_user("", {"cost": 1.0}) is None


def test_a_metering_outage_fails_open(monkeypatch):
    """A blip in billing must not stop every customer solving captchas - that
    costs far more than the fraction of a cent it would protect."""
    monkeypatch.setattr(cs, "CREDITS_BASE", "http://backend")

    def boom(path, payload):
        raise OSError("connection refused")

    monkeypatch.setattr(cs, "_credits_post", boom)
    allowed, _balance, note = cs.authorize_user("jwt")
    assert allowed is True and "unreachable" in note


def test_a_rejected_token_fails_closed(monkeypatch):
    """Unreachable is not the same as unauthorized: a 401 means this caller has
    no right to spend, and failing open there would be a free solver."""
    import urllib.error
    monkeypatch.setattr(cs, "CREDITS_BASE", "http://backend")

    def unauthorized(path, payload):
        raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(cs, "_credits_post", unauthorized)
    allowed, balance, note = cs.authorize_user("jwt")
    assert allowed is False and balance == 0


def test_a_free_failure_is_not_billed(monkeypatch):
    """A gateway timeout returns no usage - we were not charged, so neither is
    the user. A policy refusal DOES report a cost and is billed."""
    monkeypatch.setattr(cs, "CREDITS_BASE", "http://backend")
    posts = []
    monkeypatch.setattr(cs, "_credits_post", lambda p, pl: posts.append(pl) or {"data": {}})
    assert cs.charge_user("jwt", {}) is None
    assert cs.charge_user("jwt", {"cost": 0}) is None
    assert posts == []
    cs.charge_user("jwt", {"cost": 0.0015})
    assert posts[0]["upstreamCostMicros"] == 1500


def test_one_cycle_matches_the_carousel_size():
    """Reading past the wrap just pays to be told the same thing twice: the
    carousel holds six, so a pass is six."""
    assert vc.MAX_OPTIONS == 6


def test_the_recheck_pass_is_magnified():
    """A whole pass with no match means a die was misread, not that the puzzle
    was unsolvable - the model said as much once: 'sum to about 33 or 42'.
    Re-reading the same pixels would repeat the error; larger ones might not."""
    assert vc.RETRY_SCALE > vc.SHOT_SCALE


def test_a_rate_limit_waits_longer_than_a_blip():
    """429 is the upstream asking for time; the 2s backoff that clears a 504
    just burns the retries and strands the puzzle."""
    assert cs.RATE_LIMIT_BACKOFF_S > cs.RETRY_BACKOFF_S * 3


def test_a_dead_model_falls_back_to_a_sibling(monkeypatch):
    """Abandoning a puzzle four rounds in costs more than one call elsewhere."""
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)
    tried = []

    def opener(req, timeout=None):
        body = json.loads(req.data.decode())
        tried.append(body["model"])
        if body["model"] == "primary/model":
            return _Resp({"error": {"code": 429, "message": "rate limited"}})
        return _Resp({"choices": [{"message": {"content": '{"action":"next"}'}}]})

    parsed, _ = cs.call_model_with_retries([PNG], "p", "k", model="primary/model",
                                           opener=opener, fallbacks=("backup/model",))
    assert parsed == {"action": "next"}
    assert tried[0] == "primary/model" and tried[-1] == "backup/model"


def test_fallbacks_never_retry_the_failing_model_as_its_own_backup(monkeypatch):
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)
    tried = []

    def opener(req, timeout=None):
        tried.append(json.loads(req.data.decode())["model"])
        raise OSError("down")

    with pytest.raises(cs.SolveError):
        cs.call_model_with_retries([PNG], "p", "k", model="same/model",
                                   opener=opener, fallbacks=("same/model",))
    assert set(tried) == {"same/model"}


def test_the_card_is_sent_as_jpeg_not_png():
    """A 2x PNG of the card is ~327 kB. Every one of those bytes is upload time
    and image tokens, ~20 times per puzzle."""
    assert vc.CARD_FORMAT == "jpeg"
    # High enough that 12-vs-2 and 11-vs-1 survive; the puzzle turns on that.
    assert vc.CARD_QUALITY >= 80


def test_a_garbled_reply_is_retried_before_giving_up():
    """One bad reply used to kill a puzzle three rounds in, even though the
    same frame replayed perfectly a minute later."""
    calls = {"n": 0}

    def opener(req, timeout=None):
        calls["n"] += 1
        body = "" if calls["n"] == 1 else '{"action":"next","reading":"32 not 31"}'
        return _Resp({"choices": [{"message": {"content": body}}]})

    action, detail = cs.decide_step(PNG, "k", opener=opener)
    assert action == "next" and calls["n"] == 2
    assert detail["reading"] == "32 not 31"


def test_persistent_garbage_still_ends_as_unsure():
    calls = {"n": 0}

    def opener(req, timeout=None):
        calls["n"] += 1
        return _Resp({"choices": [{"message": {"content": "no idea"}}]})

    action, detail = cs.decide_step(PNG, "k", opener=opener)
    assert action == "unsure" and calls["n"] == cs.CONTENT_RETRIES
    assert "did not return JSON" in detail["reason"]


def test_the_dataset_names_files_by_their_real_format(tmp_path):
    """These are training data; a JPEG called .png trips up whatever reads the
    set later."""
    jpeg = base64.b64encode(bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"0" * 200).decode()
    sid = cs.log_step(tmp_path, {"screenshot": jpeg}, "next", {})
    assert (tmp_path / f"{sid}.jpg").exists()
    sid2 = cs.log_step(tmp_path, {"screenshot": PNG}, "next", {})
    assert (tmp_path / f"{sid2}.png").exists()


def test_a_still_loading_card_is_waited_out_not_abandoned():
    """The model reported 'Loading spinners are displayed instead of dice' and
    the run gave up on a puzzle that was merely mid-paint. Its own words are
    the signal to wait and look again."""
    assert vc.LOADING_RETRIES >= 3
    assert any(w in "loading spinners are displayed instead of dice"
               for w in vc.LOADING_WORDS)


def test_later_rounds_still_get_a_real_settle_window():
    """Trimming this too far is what handed the model an unpainted card."""
    assert vc.POST_SUBMIT_S >= 5


def test_batch_solve_recomputes_the_choice_from_the_sums():
    """Arithmetic is what the model is worst at and the one thing we can
    check, so its own pick is a hint, not the answer."""
    choice, detail = cs.decide_batch(PNG, [PNG]*6, "k",
        opener=_model('{"sums":[33,31,32,30,29,28],"choice":1}'))
    assert choice == 1 and detail["sums"][0] == 33


def test_batch_solve_refuses_an_ambiguous_round():
    choice, detail = cs.decide_batch(PNG, [PNG]*6, "k",
        opener=_model('{"sums":[33,33,32,30,29,28],"choice":1}'))
    assert choice is None and "share the target sum" in detail["reason"]


def test_batch_solve_falls_back_to_the_pick_without_sums():
    choice, detail = cs.decide_batch(PNG, [PNG]*6, "k",
                                     opener=_model('{"choice":4}'))
    assert choice == 4 and detail["unverified"] is True


def test_batch_handle_solve_validates_and_meters():
    out, code = cs.handle_solve({"target": PNG, "candidates": [PNG]*6}, "k",
        opener=_model('{"sums":[1,2,3,4,5,6],"choice":3}'),
        authorize=lambda t: (True, 1, None), charge=lambda *a: None)
    assert code == 200 and out["choice"] == 3

    out, code = cs.handle_solve({"candidates": [PNG]}, "k")
    assert code == 400 and "target" in out["reason"]

    out, code = cs.handle_solve({"target": PNG, "candidates": [PNG]}, "k",
                                authorize=lambda t: (False, 0, None))
    assert out["reason"] == "insufficient_credits"


def test_batch_client_posts_target_and_candidates():
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp({"choice": 2})

    c = vc.SolverClient("http://x:8788", opener=opener)
    assert c.solve(PNG, [PNG, PNG], 3)["choice"] == 2
    assert seen["url"].endswith("/v1/solve")
    assert len(seen["body"]["candidates"]) == 2 and seen["body"]["round"] == 3


# ------------------------------------------------------- the retry deadline
#
# The client is a browser sitting on a LIVE Arkose challenge, not a batch job.
# A retry budget that outlasts SolverClient's patience does not buy a better
# answer, it loses the whole puzzle: the client hangs up, the rounds already
# solved cannot be replayed, and the account is gone. Watched happening three
# rounds in, so the relationship between the two timeouts is pinned here.


def test_the_server_answers_before_the_client_gives_up():
    """The arithmetic, not just the constant: whatever the retry knobs are set
    to, one /v1/step must still fit inside SolverClient's default timeout."""
    assert cs.STEP_DEADLINE_S < vc.SolverClient("http://x").timeout


def test_retries_stop_at_the_deadline_instead_of_running_the_budget_out():
    import time

    calls = []

    def always_transient(req, timeout=None):
        calls.append(timeout)
        raise urllib_error().HTTPError(req.full_url, 503, "busy", {}, None)

    def urllib_error():
        import urllib.error
        return urllib.error

    started = time.monotonic()
    with pytest.raises(cs.SolveError):
        cs.call_model_with_retries([PNG], "p", "k", opener=always_transient,
                                   deadline=time.monotonic() + 0.2)
    # MODEL_RETRIES * REQUEST_TIMEOUT across two models is over half an hour;
    # the deadline has to end it in a fraction of a second instead.
    assert time.monotonic() - started < 5.0
    # Every socket timeout handed down was the REMAINING time, never the full
    # per-call budget -- a 180s read on a 0.2s deadline is the same bug again.
    assert calls and all(c <= cs.REQUEST_TIMEOUT for c in calls)
    assert all(c <= 0.2 for c in calls)


def test_a_decision_that_runs_out_of_time_is_unsure_not_an_exception():
    """Out of time must degrade to the human in the window, which is what
    'unsure' means to the client -- never to a 502 that reads as a bug."""
    import time

    def slow(req, timeout=None):
        raise OSError("timed out")

    action, detail = cs.decide_step(PNG, "k", opener=slow,
                                    deadline=time.monotonic() - 1)
    assert action == "unsure"
    assert "time" in (detail.get("reason") or "")
