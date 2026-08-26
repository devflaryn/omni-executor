"""accountcreator: identity generation, config validation, vault, solver.

The Selenium flow itself is only smoke-tested here (a fake driver records the
calls); the point of these tests is that everything AROUND the browser — the
generators, the validation contract shared with main.Api, the vault file and
the 2captcha.com client's request/response shape — is pinned down without
touching a network or a Chrome.
"""

import json
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

import pytest

import accountcreator as ac


@pytest.fixture
def vault_root():
    """A throwaway home for the vault.

    A unique PLAIN mkdir rather than tempfile.mkdtemp/pytest's tmp_path:
    mkdtemp hardens the directory's ACL to owner-only, which some sandboxed
    environments (restricted tokens, service accounts) then cannot write back
    into. A normal directory inherits the parent's ACL everywhere."""
    import uuid
    try:
        base = Path(tempfile.gettempdir()) / "omni-vault-tests"
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        base = Path(__file__).resolve().parent.parent / "test-scratch"
        base.mkdir(parents=True, exist_ok=True)
    d = base / f"vault-{uuid.uuid4().hex[:10]}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- generators

def test_username_styles_match_their_examples():
    """Each style must produce names shaped like the examples shown in the UI:
    Eric848381 / FrozenWolf1192 / Bright_Shark858 / mwu78cnas782n."""
    for _ in range(200):
        name = ac.generate_username("name_no")
        assert ac.valid_username(name) and any(c.isdigit() for c in name)
        assert name[0].isalpha()

        adj_noun = ac.generate_username("adj_noun")
        assert ac.valid_username(adj_noun)
        stem = "".join(ch for ch in adj_noun if not ch.isdigit())
        assert any(adj_noun.startswith(a) for a in ac.ADJECTIVES)
        assert any(n in stem for n in ac.ANIMAL_NOUNS)

        tag = ac.generate_username("gamertag")
        assert ac.valid_username(tag)
        assert any(tag.startswith(a) for a in ac.GAMER_ADJECTIVES)

        stealth = ac.generate_username("stealth")
        assert ac.valid_username(stealth)
        assert stealth == stealth.lower()
        assert any(c.isdigit() for c in stealth)


def test_usernames_are_roblox_valid():
    for style in ac.USERNAME_STYLES:
        for _ in range(100):
            name = ac.generate_username(style)
            assert 3 <= len(name) <= ac.USERNAME_MAX_LEN, (style, name)
            assert ac.USERNAME_RE.fullmatch(name), (style, name)


def test_unknown_style_raises():
    with pytest.raises(ValueError):
        ac.generate_username("nope")


def test_birthday_is_always_adult_but_plausible():
    today = date.today()
    for _ in range(300):
        y, m, d = ac.generate_birthday()
        bday = date(y, m, d)
        age = today.year - y - ((today.month, today.day) < (m, d))
        assert age >= 18, (y, m, d)
        assert age <= 45


def test_password_meets_all_classes_and_is_random():
    pw = ac.generate_password()
    assert len(pw) >= 16
    assert any(c.isupper() for c in pw)
    assert any(c.islower() for c in pw)
    assert any(c.isdigit() for c in pw)
    assert any(c in ac.PASSWORD_SYMBOLS for c in pw)
    assert {pw, ac.generate_password(), ac.generate_password()} != {pw}


def test_gender_pick():
    assert ac.pick_gender() in ("male", "female")


# ------------------------------------------------------------- configuration

def test_validate_config_defaults_and_clamping():
    clean, err = ac.validate_creation_config()
    assert err is None
    assert clean["amount"] == 1
    assert clean["usernameStyle"] == "name_no"
    assert clean["customPassword"] is None
    assert clean["captchaProvider"] == "2captcha"
    assert set(clean["captchaApiKeys"]) == {"2captcha"}


@pytest.mark.parametrize("bad", [0, -3, 51, "lots"])
def test_validate_config_rejects_bad_amount(bad):
    clean, err = ac.validate_creation_config(amount=bad)
    assert clean is None and err


def test_validate_config_rejects_unknown_style_provider():
    _, e1 = ac.validate_creation_config(username_style="l33t")
    assert e1
    _, e2 = ac.validate_creation_config(captcha_provider="anticaptcha")
    assert e2


def test_validate_config_requires_strong_custom_password():
    clean, err = ac.validate_creation_config(custom_password="short")
    assert clean is None and "8" in err
    clean, _ = ac.validate_creation_config(custom_password="long-enough-1!")
    assert clean["customPassword"] == "long-enough-1!"


def test_validate_config_strips_api_keys():
    clean, _ = ac.validate_creation_config(captcha_api_keys={"2captcha": "  key-123  ",
                                                            "unknown": "x"})
    assert clean["captchaApiKeys"]["2captcha"] == "key-123"
    assert set(clean["captchaApiKeys"]) == {"2captcha"}


# -------------------------------------------------------------------- vault

def test_vault_roundtrip(vault_root):
    v = ac.Vault(vault_root)
    v.add("Eric848381", "s3cret-Pass!", birthday=(1999, 4, 12), style="name_no")
    rec = v.get("Eric848381")
    assert rec["password"] == "s3cret-Pass!"
    assert rec["birthday"] == [1999, 4, 12]
    # Listing hides secrets by default; reveal is explicit.
    rows = v.list()
    assert rows[0]["username"] == "Eric848381"
    assert rows[0]["hasPassword"] is True and "password" not in rows[0]
    full = v.list(include_secrets=True)
    assert full[0]["password"] == "s3cret-Pass!"
    assert v.remove("Eric848381") is True
    assert v.get("Eric848381") is None
    assert v.remove("Eric848381") is False


def test_vault_file_is_json_and_survives_reload(vault_root):
    v = ac.Vault(vault_root)
    v.add("Ultra_Hawk948", "pw", style="adj_noun")
    data = json.loads((vault_root / ac.VAULT_FILE).read_text(encoding="utf-8"))
    assert data["accounts"]["Ultra_Hawk948"]["style"] == "adj_noun"
    assert ac.Vault(vault_root).get("Ultra_Hawk948")["createdAt"] <= time.time()


# ------------------------------------------------------------ 2captcha solver

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _solver_with(monkeypatch, responses, sleep=lambda s: None):
    calls = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        url = req.full_url
        calls.append({"url": url, "body": body})
        # Beyond the scripted responses the service keeps saying the same
        # thing (normally "processing"), so polling tests can rely purely on
        # their own deadlines.
        idx = min(len(responses) - 1, len(calls) - 1)
        return _FakeResponse(responses[idx])

    monkeypatch.setattr(ac.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ac.time, "sleep", sleep)
    return calls


def test_solver_happy_path(monkeypatch):
    responses = [
        {"errorId": 0, "taskId": "t-1"},
        {"status": "processing"},
        {"errorId": 0, "status": "ready",
         "solution": {"token": "TOKEN|abc"}},
    ]
    calls = _solver_with(monkeypatch, responses, sleep=lambda s: None)
    s = ac.TwoCaptchaSolver("key-123", poll=0)
    token = s.solve_funcaptcha("https://www.roblox.com/up/registration",
                               ac.ROBLOX_ARKOSE_PUBLIC_KEY)
    assert token == "TOKEN|abc"
    create, result = calls[0], calls[-1]
    assert create["url"].endswith(ac.TWOCAPTCHA_CREATE_PATH)
    assert result["url"].endswith(ac.TWOCAPTCHA_RESULT_PATH)
    assert create["body"]["clientKey"] == "key-123"
    task = create["body"]["task"]
    assert task["type"] == ac.FUN_CAPTCHA_TASK_TYPE
    assert task["websitePublicKey"] == ac.ROBLOX_ARKOSE_PUBLIC_KEY
    assert result["body"]["taskId"] == "t-1"


def test_solver_error_paths(monkeypatch):
    _solver_with(monkeypatch, [{"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST"}])
    with pytest.raises(ac.CaptchaError, match="KEY"):
        ac.TwoCaptchaSolver("bad", poll=0).solve_funcaptcha("https://x", "k")

    _solver_with(monkeypatch, [{"errorId": 0, "taskId": "t"}, {"status": "processing"}])
    s = ac.TwoCaptchaSolver("key", timeout=0.01, poll=0)
    with pytest.raises(ac.CaptchaError, match="did not finish"):
        s.solve_funcaptcha("https://x", "k")

    with pytest.raises(ac.CaptchaError, match="empty"):
        ac.TwoCaptchaSolver("   ")


def test_make_solver_gates_on_key():
    assert ac.make_solver("2captcha", {"2captcha": ""}) is None
    assert isinstance(ac.make_solver("2captcha", {"2captcha": "k"}),
                      ac.TwoCaptchaSolver)


# ------------------------------------------------------- selenium flow (fake)

class FakeElement:
    def __init__(self, tag="input", text="", value="", on_click=None):
        self.tag = tag
        self.text = text
        self.value = value
        self.on_click = on_click
        self.cleared = False
        self.typed = []
        self.clicked = False

    def clear(self):
        self.cleared = True

    def send_keys(self, v):
        self.typed.append(v)

    def click(self):
        self.clicked = True
        if self.on_click:
            self.on_click()

    def is_displayed(self):
        return True

    def find_elements(self, *a):
        return []

    def get_attribute(self, name):
        if name == "value":
            return str(self.value)
        return None


class FakeDriver:
    """Records the signup conversation; enough surface for create_account."""

    def __init__(self, script_results=None):
        self.urls_visited = []
        self.url = "https://www.roblox.com/up/registration"
        self.cookies = {}
        self.elements = {}
        self.script_calls = []
        self._script_results = script_results or {}

    def get(self, url):
        self.urls_visited.append(url)
        self.url = url

    @property
    def current_url(self):
        return self.url

    def find_element(self, by, sel):
        el = self.elements.get(sel)
        if el is None:
            from selenium.common.exceptions import NoSuchElementException
            raise NoSuchElementException(sel)
        return el

    def find_elements(self, by, sel):
        els = self.elements.get(sel) or []
        return els if isinstance(els, list) else [els]

    def execute_script(self, script, *args):
        self.script_calls.append((script, args))
        return self._script_results.get("inject", [])

    def get_cookie(self, name):
        return self.cookies.get(name)

    def quit(self):
        pass


def test_create_account_success_path(monkeypatch):
    """With a driver whose form fills cleanly, create_account must harvest the
    cookie, verify it via whoami, and hand back every secret."""
    drv = FakeDriver(script_results={"inject": ["hook:__arkose_enforcement"]})
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)

    def land_on_home():
        drv.url = "https://www.roblox.com/home"
        drv.cookies[ac.COOKIE_NAME] = {"value": "ROBL-COOKIE"}

    drv.elements = {
        "#signup-username": FakeElement(),
        "#signup-password": FakeElement(),
        "#signup-button": FakeElement(tag="button", text="Sign Up",
                                      on_click=land_on_home),
    }
    lines = []

    class FakeAccounts:
        @staticmethod
        def whoami(cookie):
            assert cookie == "ROBL-COOKIE"
            return 987654, "Eric848381"

        @staticmethod
        def _driver(browser, headless=False):
            assert headless is False   # the window stays visible on purpose
            return drv

    monkeypatch.setattr(ac, "_omni_accounts", lambda: FakeAccounts)

    res = ac.create_account(on_status=lines.append,
                            username_style="name_no",
                            driver_factory=lambda: drv)
    assert res["ok"], res
    assert res["username"] == "Eric848381"
    assert res["user_id"] == 987654
    assert res["cookie"] == "ROBL-COOKIE"
    assert res["birthday"][0] <= date.today().year - 18
    assert res["gender"] in ("male", "female")
    assert len(res["password"]) >= 8
    # The password field actually received the SAME password we handed back.
    pw_el = drv.elements["#signup-password"]
    assert pw_el.typed == [res["password"]]
    assert any("registration page loaded" in l for l in lines)


def test_create_account_retries_taken_username(monkeypatch):
    """A 'username already taken' error regenerates instead of failing."""
    drv = FakeDriver()
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    clicks = {"n": 0}
    seen = []

    def counting(style):
        seen.append(len(seen) + 1)
        return f"Attempt{len(seen)}"

    def submit_click():
        clicks["n"] += 1
        if clicks["n"] >= 3:
            # Third submission goes through: land authenticated, alert gone.
            drv.elements.pop("[role='alert']", None)
            drv.url = "https://www.roblox.com/home"
            drv.cookies[ac.COOKIE_NAME] = {"value": "C"}

    drv.elements = {
        "#signup-username": FakeElement(),
        "#signup-password": FakeElement(),
        "[role='alert']": FakeElement(text="Username already taken"),
        "#signup-button": FakeElement(tag="button", text="Sign Up",
                                      on_click=submit_click),
    }

    class FakeAccounts:
        @staticmethod
        def whoami(cookie):
            assert cookie == "C"
            return 1, "FreshName"

        @staticmethod
        def _driver(browser, headless=False):
            return drv

    monkeypatch.setattr(ac, "generate_username", counting)
    monkeypatch.setattr(ac, "_omni_accounts", lambda: FakeAccounts)

    res = ac.create_account(driver_factory=lambda: drv)
    assert res["ok"], res
    assert res["username"] == "FreshName"
    assert len(seen) == 3      # two taken, third accepted
    assert clicks["n"] == 3


def test_create_account_reports_browser_failure():
    def boom():
        raise OSError("chrome missing")

    res = ac.create_account(driver_factory=boom)
    assert res["ok"] is False
    assert res["error"] == "browser_failed"


# ------------------------------------------------------- captcha page probes

def test_public_key_scraped_from_iframe(monkeypatch):
    class IframeDrv:
        url = "https://www.roblox.com/"

        def find_elements(self, by, sel):
            class F:
                def get_attribute(self, n):
                    return ("https://client-api.arkoselabs.com/v2/iframe?"
                            "public_key=A2A14B1D-TEST&data[type]=default")

            return [F()]

    assert ac.extract_public_key(IframeDrv()) == "A2A14B1D-TEST"


def test_landed_on_home_matches_only_authenticated_paths():
    assert ac.landed_on_home("https://www.roblox.com/home?x=1")
    assert ac.landed_on_home("https://www.roblox.com/discover")
    assert not ac.landed_on_home("https://www.roblox.com/login")
    assert not ac.landed_on_home("")


# ------------------------------------------------ stale-provider self-heal

def test_creation_get_config_heals_removed_provider(tmp_path, monkeypatch):
    """settings.json outlives the surfsky -> 2captcha swap, so a leftover
    "surfsky" provider must be normalized to the default (and written back)
    instead of rejecting every creation batch."""
    import json
    import main

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"captcha": {"provider": "surfsky",
                                "apiKeys": {"surfsky": "old-key"}}}),
        encoding="utf-8")
    monkeypatch.setattr(main, "SETTINGS_FILE", settings_file)

    cfg = main.Api().creation_get_config()
    assert cfg["captcha"]["provider"] == "2captcha"
    # The orphaned surfsky key is dropped before it can leak or be validated.
    assert set(cfg["captcha"]["apiKeys"]) == {"2captcha"}
    assert cfg["captcha"]["apiKeys"]["2captcha"] == ""

    # Healed on disk too — one run fixes it permanently.
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["captcha"]["provider"] == "2captcha"
    assert set(on_disk["captcha"]["apiKeys"]) == {"2captcha"}
