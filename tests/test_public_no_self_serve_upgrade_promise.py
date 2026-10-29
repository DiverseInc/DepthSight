# tests/test_public_no_self_serve_upgrade_promise.py
"""The product must not promise a self-serve paid upgrade it does not have.

THE DEFECT THIS PINS
--------------------
DepthSight sells three plans -- Free ($0), Standard ($30) and Professional
($100) -- all of them billed monthly. `api/plans_config.yml` has no yearly
plan, no lifetime purchase and no Team tier, and `build_plan_catalog()` (the
source of the live `GET /api/v1/public/plans`) reports `billing_mode: monthly`
for every plan and a trial of {enabled: true, days: 0, active: false}, which
means there is no trial either.

There is also no way for a customer to buy one of those plans themselves:

  * there is no /billing route -- `frontend/src/App.tsx` has /settings
    (line 240) and /account (line 241) and nothing else;
  * no payment gateway is deployed -- `docker-compose.yml` carries no
    BITCART_*, PAYMENT_* or STRIPE_* variable, and `deploy.sh:135` sets
    START_BITCART="n";
  * `api/routes/payments.py` raises "Payment provider is not configured."
    unless BITCART_API_URL, BITCART_API_KEY and BITCART_STORE_ID are all
    set, so a crypto checkout on the account page could never complete;
  * a plan requested at signup is validated and then only LOGGED
    (`api/crud.py:1082-1089`), never persisted and never notified, so
    "choose Standard" ends on Free with the request dropped into a log line.

What the product actually says about this is a set of promises the product
cannot keep. The knowledge-base article "Plans and Billing" described a
BitCart redirect, a choice of cryptocurrency (USDT TRC-20, BTC, ETH), a
monthly OR yearly subscription period, instant automatic activation, exactly
30 or 365 days of validity, no auto-renewal, and a prorated mid-month
Standard -> Pro upgrade. The dashboard banner pointed at "Settings ->
Billing". The account page carried a whole crypto checkout screen. The
register page promised "upgrade any time from your account" in English and
"сменить план можно в любой момент в аккаунте" in Russian.

None of that is true, and a customer reading it expects to be able to pay.

THE HONEST PATH, WHICH IS WHAT THE COPY NOW SAYS
------------------------------------------------
A customer gets the plan they are entitled to. To activate a paid tier you
CONTACT US and we set it up for you, and a support ticket is how you ask
about billing. The support ticket category "Billing / Plans" genuinely
exists (frontend/src/locales/en/support.json line 30), so the copy points
at something real.

WHAT THIS TEST DOES
-------------------
It reads every customer-visible surface that carries this promise -- the
register copy, the knowledge-base articles and their index, and the React
components and locale files that render plan copy -- and asserts that none
of them contains:

  * a promise of a self-serve upgrade ("upgrade", "сменить план", "обновите
    ваш план", "в любой момент");
  * a reference to a Billing screen (a place to go), as opposed to the
    support category that is called "Billing / Plans";
  * a "Subscribe" button, or a claim that a subscription exists;
  * "BitCart";
  * a cryptocurrency offered as a way to pay;
  * a 30-day or 365-day validity claim;
  * an automatic renewal;
  * a prorated mid-month upgrade;
  * a Team tier.

Three facts have to be stated OUT LOUD in the article -- that there is no
one-off lifetime purchase, no yearly plan and no self-service checkout --
so those three rules are allowed inside a sentence that denies them and
flagged everywhere else. A rule that could not tell "we do not sell a
yearly plan" from "buy a yearly plan" would be a rule worth nothing.

It also pins the knowledge-base article to the LIVE CATALOG, so the article
cannot drift back into selling a plan or a price that does not exist.

WHAT THIS TEST DELIBERATELY DOES NOT SCAN
------------------------------------------
`frontend/src/lib/pricing.ts`,
`frontend/src/components/marketing/Pricing.tsx` and
`frontend/welcome.html` carry the same defect on this branch, but another
branch owns them and ships its own guard
(`tests/test_public_pricing_matches_catalog.py` on `fix/truthful-public-pricing`).
Scanning them here would make this branch fail on files this branch must not
touch, so they are left to their owner.
"""

import json
import re
from pathlib import Path

import pytest

from api.plans import build_plan_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCALES_DIR = REPO_ROOT / "frontend" / "src" / "locales"
KB_DIR = REPO_ROOT / "frontend" / "src" / "content" / "kb"


# --------------------------------------------------------------------------
# The surfaces this branch owns
# --------------------------------------------------------------------------
# The register copy, in every language.
REGISTER_LOCALES = tuple(sorted(LOCALES_DIR.glob("*/register.json")))

# The knowledge-base articles, in both languages.
KB_ARTICLES = tuple(sorted(KB_DIR.glob("en/*.md"))) + tuple(
    sorted(KB_DIR.glob("ru/*.md"))
)

# The React components that render plan copy a customer reads.
GUARDED_COMPONENTS = (
    "frontend/src/components/dashboard/WelcomeBanner.tsx",
    "frontend/src/components/shared/PricingModal.tsx",
    "frontend/src/components/AppSidebar.tsx",
    "frontend/src/components/UserNav.tsx",
    "frontend/src/pages/CommunityHub.tsx",
)

# The knowledge-base index.
KB_INDEX = KB_DIR / "articles.ts"

# The locale files this branch fixed.
GUARDED_LOCALES = (
    "frontend/src/locales/en/account.json",
    "frontend/src/locales/ru/account.json",
    "frontend/src/locales/en/common.json",
    "frontend/src/locales/ru/common.json",
    "frontend/src/locales/en/discovery.json",
    "frontend/src/locales/ru/discovery.json",
)


# --------------------------------------------------------------------------
# The register copy, and the one string this branch must not police
# --------------------------------------------------------------------------
# The English register copy is owned by another branch, which is rewriting
# it to the wording below. Until that branch lands, the file still holds
# the legacy promise, so THAT EXACT STRING is the only thing skipped here.
# The moment the owning branch rewrites the copy, the file is policed like
# every other register copy.
LEGACY_EN_REGISTER_COPY = (
    "You selected the {{plan}} plan. New accounts start on the plan they are "
    "entitled to \u2014 upgrade any time from your account."
)

# The agreed English wording, which the owning branch is landing.
AGREED_EN_REGISTER_COPY = (
    "You selected the {{plan}} plan. New accounts start on the plan they are "
    "entitled to \u2014 to activate a paid tier, contact us and we will set "
    "it up for you."
)

# The agreed Russian wording, landed here.
AGREED_RU_REGISTER_COPY = (
    "Вы выбрали план {{plan}}. Новый аккаунт начинает с того плана, который "
    "ему положен — чтобы активировать платный тариф, напишите нам, и мы "
    "настроим его для вас."
)


# --------------------------------------------------------------------------
# The forbidden promises
# --------------------------------------------------------------------------
# A rule is (name, pattern, denial_is_allowed, why). `denial_is_allowed` is
# True only for the three facts the article has to state out loud; a match
# in a sentence that carries a negation ("no yearly plan", "нет годового
# тарифа") is then a correction rather than a promise.
#
# Every rule names the PROMISE, not merely a word:
#
#   * "Billing" is forbidden only where it is a PLACE to go. The support
#     category is really called "Billing / Plans" and must stay allowed;
#   * "USDT" is forbidden only next to a payment word. The affiliate
#     program really does pay commissions to a USDT (TRC-20) wallet, and
#     BTCUSDT is a trading pair, so a bare ban would be wrong;
#   * "Subscription" as a noun is allowed -- the article is called "Plans
#     and Subscription Billing" -- but a "Subscribe" button, a "subscription
#     period" and "your subscription is active" are not.
FORBIDDEN = (
    (
        "the BitCart gateway",
        re.compile(r"\bbitcart\b", re.I),
        False,
        "names a payment gateway that is not deployed",
    ),
    (
        "a cryptocurrency payment method",
        re.compile(
            r"\bcryptocurrenc\w*"
            r"|\bкриптовалют\w*"
            r"|\bUSDT\s*\(?\s*TRC-?20\b"
            r"|\b(?:pay|paying|paid|payment|payments|checkout|charge|charges|"
            r"transfer|cryptocurrenc\w+)\b[^.\n]{0,60}\b(?:USDT|BTC|ETH|LTC|USDC|TRX)\b"
            r"|\b(?:USDT|BTC|ETH|LTC|USDC|TRX)\b[^.\n]{0,60}\b"
            r"(?:payment|payments|pay|checkout|charge|charges|transfer|cryptocurrenc\w+)\b"
            r"|\bоплат\w*[^.\n]{0,60}\b(?:USDT|BTC|ETH|LTC)\b"
            r"|\bплатеж\w*[^.\n]{0,60}\b(?:USDT|BTC|ETH|LTC)\b",
            re.I,
        ),
        False,
        "offers a cryptocurrency payment method",
    ),
    (
        "an Upgrade button",
        re.compile(r"\bupgrad\w*|\bапгрейд\w*", re.I),
        False,
        "promises an upgrade the customer cannot make themselves",
    ),
    (
        "a Subscribe button",
        re.compile(
            r"\bSubscribe\b"
            r"|\bsubscribe\s+to\s+(?:a|the|our)\s+(?:plan|tier|premium)\b"
            r"|\bSubscriptions?\s+(?:are|is|will)\b"
            r"|\bsubscription\s+period\b"
            r"|\bподписк\w*\s+(?:период|действует)\b",
            re.I,
        ),
        False,
        "offers a subscription the product does not have",
    ),
    (
        "a Billing screen",
        re.compile(
            r"\b(?:go|navigate|open|head|visit|find)\s+(?:to\s+)?"
            r"(?:the\s+|your\s+)?\**Billing\b"
            r"|\bBilling\b\s*\**\s*(?:section|page|tab|screen|menu|panel|settings)\b"
            r"|\b(?:section|page|tab|screen|menu|panel)\b[^.\n]{0,24}\bBilling\b"
            r"|\b(?:Settings|Настройки)\b[^\n]{0,8}\bBilling\b"
            r"|\bраздел\w*\s*\**Billing\b",
            re.I,
        ),
        False,
        "points the customer at a Billing screen that does not exist",
    ),
    (
        "a 30-day or 365-day validity claim",
        re.compile(
            r"\b365\s*[- ]?\s*days?\b"
            r"|\bexactly\s+\d+\s*[- ]?\s*days?\b"
            r"|\bvalid\w*\b[^.\n]{0,30}\b\d+\s*[- ]?\s*days?\b"
            r"|\bдней\s+с\s+момента\s+активац\w*"
            r"|\bдействует\s+ровно\s+\d+\s*дн\w+",
            re.I,
        ),
        False,
        "promises a fixed validity period the catalog does not define",
    ),
    (
        "an automatic renewal",
        re.compile(
            r"\bauto[-\s]?renew\w*"
            r"|\bautorenew\w*"
            r"|\brenew(?:s|ed|ing)?\s+automatically\b"
            r"|\brecurr\w*\s+(?:charge|charges|payment|payments|billing|subscription)\b"
            r"|\bавтопродлен\w*"
            r"|\bавтоспис\w*",
            re.I,
        ),
        False,
        "promises an automatic renewal the product does not do",
    ),
    (
        "a prorated mid-month upgrade",
        re.compile(
            r"\bprorat\w*"
            r"|\bprice\s+difference\b"
            r"|\bpay\s+only\s+the\s+difference\b"
            r"|\bmid[-\s]month\b"
            r"|\bразниц\w+\s+в\s+стоимости\b"
            r"|\bв\s+середине\s+месяца\b",
            re.I,
        ),
        False,
        "promises a prorated mid-month upgrade",
    ),
    (
        "a Team tier",
        re.compile(
            r"\bTeam\s+(?:plan|tier|package|subscription|seat)\w*"
            r"|\b(?:plan|tier|package|subscription)\w*\s+Team\b"
            r"|\bteam\s+(?:tier|plan)\b"
            r"|\bкоманд\w*\s+(?:тариф\w*|план\w*)",
            re.I,
        ),
        False,
        "sells a Team tier that the catalog does not have",
    ),
    (
        "a self-serve plan change",
        re.compile(
            r"\b(?:change|changing|switch|switching|update|updating)\s+"
            r"(?:your\s+|the\s+|their\s+|own\s+)?(?:plan|tier|subscription)\b"
            r"|\b(?:сменить|смените|изменить|измените|поменять|поменяйте|"
            r"обновить|обновите)\s+(?:свой\s+|ваш\s+)?(?:план|тариф)\w*"
            r"|\bв\s+любой\s+момент\b",
            re.I,
        ),
        False,
        "promises that the customer can change their own plan",
    ),
    (
        "a yearly or annual plan",
        re.compile(
            r"\b(?:yearly|annual)\s+(?:plan|plans|subscription|tier|tiers)\b"
            r"|\bгодов\w*\s+(?:тариф\w*|план\w*|подписк\w*)",
            re.I,
        ),
        True,
        "sells a yearly plan that the catalog does not have",
    ),
    (
        "a one-off lifetime purchase",
        re.compile(
            r"\blifetime\s+(?:purchase|payment|plan|option|access)\b"
            r"|\bнавсегда\b"
            r"|\bпожизненн\w*",
            re.I,
        ),
        True,
        "sells a one-off lifetime purchase the deployment cannot take",
    ),
    (
        "a self-service checkout",
        re.compile(r"\bself[-\s]?serv\w*|\bсамостоятельн\w*", re.I),
        True,
        "offers a self-service checkout that is not deployed",
    ),
)

# A sentence that says a thing does NOT happen.
NEGATION_RE = re.compile(
    r"\b(?:no|not|never|none|cannot|can't|neither|nor|isn't|aren't|don't|doesn't)\b"
    r"|\bнет\b|\bнельзя\b|\bне\b|\bбез\b",
    re.I,
)

_SENTENCE_SEPARATORS = ".!?;\n"


def _sentence_around(unit: str, start: int, end: int) -> str:
    """The sentence of `unit` that contains the match [start, end)."""
    left = max(
        (unit.rfind(sep, 0, start) for sep in _SENTENCE_SEPARATORS),
        default=-1,
    )
    rights = [
        unit.find(sep, end)
        for sep in _SENTENCE_SEPARATORS
        if unit.find(sep, end) != -1
    ]
    right = min(rights) if rights else len(unit)
    return unit[left + 1 : right]


# --------------------------------------------------------------------------
# Copy units
# --------------------------------------------------------------------------
def _json_strings(value) -> list:
    """Every string inside a parsed locale file, however deeply nested."""
    out = []
    if isinstance(value, str):
        out.append(" ".join(value.split()))
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_json_strings(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_json_strings(item))
    return [unit for unit in out if unit]


def _markdown_units(text: str) -> list:
    """The knowledge-base article, one line per unit.

    A line is the smallest thing a reader can see as a claim, and it keeps
    two unrelated sentences on neighbouring lines from being read as one
    sentence -- which matters, because the rules look for a payment word
    near a currency name.
    """
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def _ts_copy_units(source: str) -> list:
    """The customer-visible copy of a TS/TSX file.

    Two kinds of unit: the string literals, and the JSX text nodes. The JSX
    text is the copy a visitor reads on the page, and it is NOT a string
    literal -- "On the free plan ..." in the dashboard banner is JSX text.
    Comments are stripped: a comment that explains why the checkout was
    removed is not a promise to the customer.
    """
    units = []

    without_comments = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    without_comments = re.sub(r"(?m)(?<![:\w])//[^\n]*", " ", without_comments)

    for match in re.finditer(
        r'"((?:[^"\\]|\\.)*)"'
        r"|'((?:[^'\\]|\\.)*)'"
        r"|`([^`]*)`",
        without_comments,
    ):
        value = next(group for group in match.groups() if group is not None)
        value = " ".join(value.split())
        if value:
            units.append(value)

    for match in re.finditer(r">([^<>{}]+)<", without_comments):
        value = " ".join(match.group(1).split())
        if value and re.search(r"[A-Za-z\u0400-\u04ff]", value):
            units.append(value)

    return units


def _guarded_surfaces() -> dict:
    """Every customer-visible surface that can carry this promise."""
    surfaces: dict[str, list] = {}

    for path in REGISTER_LOCALES:
        units = _json_strings(json.loads(path.read_text(encoding="utf-8")))
        # The one string the owning branch is replacing is not policed here.
        surfaces[str(path.relative_to(REPO_ROOT))] = [
            unit for unit in units if unit != LEGACY_EN_REGISTER_COPY
        ]

    for path in KB_ARTICLES:
        surfaces[str(path.relative_to(REPO_ROOT))] = _markdown_units(
            path.read_text(encoding="utf-8")
        )

    surfaces[str(KB_INDEX.relative_to(REPO_ROOT))] = _ts_copy_units(
        KB_INDEX.read_text(encoding="utf-8")
    )

    for relative in GUARDED_COMPONENTS:
        path = REPO_ROOT / relative
        surfaces[relative] = _ts_copy_units(path.read_text(encoding="utf-8"))

    for relative in GUARDED_LOCALES:
        path = REPO_ROOT / relative
        surfaces[relative] = _json_strings(
            json.loads(path.read_text(encoding="utf-8"))
        )

    return surfaces


# --------------------------------------------------------------------------
# The catalog, which is what the copy has to agree with
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def catalog() -> dict:
    return build_plan_catalog()


def _catalog_prices(catalog: dict) -> set:
    """Every price the catalog sells."""
    prices = set()
    for plan in catalog["plans"]:
        prices.add(float(plan["price_usd"]))
    return prices


class TestNoSurfacePromisesASelfServeUpgrade:
    """The guard itself: no customer-visible surface may promise a purchase."""

    @pytest.fixture(scope="class")
    def surfaces(self):
        return _guarded_surfaces()

    def test_the_guard_inspected_every_guarded_surface(self, surfaces):
        """The guard may not pass by inspecting nothing."""
        assert surfaces, "no guarded surface was found"
        for name, units in surfaces.items():
            assert units, (
                f"nothing was inspected on {name}; the guard would pass "
                "vacuously"
            )

    def test_no_surface_promises_a_self_serve_upgrade(self, surfaces):
        seen = 0
        for name, units in surfaces.items():
            for unit in units:
                seen += 1
                for promise, pattern, denial_ok, why in FORBIDDEN:
                    match = pattern.search(unit)
                    if match is None:
                        continue
                    if denial_ok:
                        sentence = _sentence_around(
                            unit, match.start(), match.end()
                        )
                        if NEGATION_RE.search(sentence):
                            # The copy says the thing does NOT exist.
                            continue
                    assert False, (
                        f"{name} promises {promise}: {unit!r}. It {why}. "
                        "There is no self-serve upgrade: the payment provider "
                        "is opt-in (api/routes/payments.py raises 'Payment "
                        "provider is not configured.') and the default "
                        "deployment does not enable it (deploy.sh:135)"
                    )
        assert seen, "the guard inspected nothing; re-measure this test"

    def test_the_guard_would_notice_a_reintroduced_promise(self):
        """The rules must actually match the strings they were written for.

        A rule that matches nothing is a rule that protects nothing, and a
        green run of this file would then be worth nothing.
        """
        samples = (
            ("the BitCart gateway", "the secure BitCart payment gateway"),
            (
                "a cryptocurrency payment method",
                "Choose your preferred cryptocurrency (USDT TRC-20, BTC, ETH)",
            ),
            ("an Upgrade button", "upgrade anytime in Settings -> Billing"),
            (
                "a self-serve plan change",
                "сменить план можно в любой момент",
            ),
            ("a Subscribe button", "Click the Upgrade or Subscribe button"),
            ("a Subscribe button", "subscription period (monthly or yearly)"),
            (
                "a Billing screen",
                "Navigate to the Billing section in your profile settings",
            ),
            ("a Billing screen", "Перейдите в раздел Billing"),
            (
                "a 30-day or 365-day validity claim",
                "Subscriptions are valid for exactly 30 days",
            ),
            ("an automatic renewal", "No Auto-Renewals: recurring charges"),
            (
                "a prorated mid-month upgrade",
                "pay only the price difference based on the remaining days",
            ),
            ("a Team tier", "the $99 Team tier"),
            ("a self-serve plan change", "You can change your plan at will"),
            ("a yearly or annual plan", "a yearly plan at $99"),
            ("a one-off lifetime purchase", "or $299 once - lifetime access"),
            ("a self-service checkout", "a self-service checkout"),
        )
        rules = {name: pattern for name, pattern, _, _ in FORBIDDEN}
        for promise, sample in samples:
            assert promise in rules, f"no rule is named {promise!r}"
            assert rules[promise].search(sample), (
                f"the {promise!r} rule does not match {sample!r}, so it "
                "would not catch the promise it was written for"
            )

    def test_a_denial_is_not_treated_as_a_promise(self):
        """The three facts the article states out loud must stay allowed."""
        allowed = (
            "There is no one-off lifetime purchase, no yearly plan and no "
            "self-service checkout in this deployment.",
            "Разовой покупки «навсегда» нет, годового тарифа нет и "
            "самостоятельной оплаты в этом развертывании нет.",
        )
        denial_ok = {
            name: pattern
            for name, pattern, denial, _ in FORBIDDEN
            if denial
        }
        for unit in allowed:
            for name, pattern in denial_ok.items():
                match = pattern.search(unit)
                if match is None:
                    continue
                sentence = _sentence_around(unit, match.start(), match.end())
                assert NEGATION_RE.search(sentence), (
                    f"{unit!r} denies {name!r} but carries no negation marker, "
                    "so the guard cannot tell it from a promise"
                )


class TestTheBillingArticleAgreesWithTheCatalog:
    """The article is the place a customer reads what the plans are.

    It has to name the plans the catalog sells, at the prices the catalog
    sells, and it must not sell a plan the catalog does not have.
    """

    @pytest.fixture(scope="class")
    def articles(self) -> dict:
        return {
            language: (KB_DIR / language / "billing-guide.md").read_text(
                encoding="utf-8"
            )
            for language in ("en", "ru")
        }

    def test_the_article_names_every_plan_the_catalog_sells(self, articles, catalog):
        for language, text in articles.items():
            for plan in catalog["plans"]:
                assert plan["name"] in text, (
                    f"the {language} billing article does not mention the "
                    f"{plan['name']!r} plan the catalog sells"
                )

    def test_the_article_quotes_only_prices_the_catalog_sells(
        self, articles, catalog
    ):
        known = _catalog_prices(catalog)
        for language, text in articles.items():
            for quoted in re.findall(r"\$\s?(\d+(?:\.\d+)?)", text):
                assert float(quoted) in known, (
                    f"the {language} billing article quotes ${quoted}, which "
                    f"the catalog does not sell (it sells {sorted(known)})"
                )

    def test_the_article_says_the_billing_is_monthly(self, articles, catalog):
        assert catalog["billing_mode"] == "monthly"
        for language, text in articles.items():
            assert "month" in text.lower() or "месяц" in text.lower(), (
                f"the {language} billing article must say how the paid tiers "
                "are billed"
            )

    def test_the_article_points_at_the_support_ticket(self, articles):
        for language, text in articles.items():
            assert "Billing / Plans" in text or "Оплата / Тарифы" in text, (
                f"the {language} billing article must point at the support "
                "category that actually exists"
            )

    def test_the_kb_index_entry_no_longer_advertises_a_payment_method(self):
        source = KB_INDEX.read_text(encoding="utf-8")
        entry = re.search(
            r'id:\s*"billing-guide".*?tags:\s*\[(.*?)\]', source, re.S
        )
        assert entry, "the billing-guide entry was not found in the KB index"
        tags = [tag.strip().strip('"') for tag in entry.group(1).split(",")]
        for tag in tags:
            assert tag not in {"crypto", "bitcart", "payment", "checkout"}, (
                f"the KB index still tags the billing article {tag!r}, which "
                "advertises a payment method the product does not have"
            )


class TestTheRegisterCopy:
    """The register page told the customer they could change plan at will.

    The copy has to name the plan the visitor selected, say that a new
    account starts on the tier it is entitled to, and point at a real
    channel -- contact us -- instead of promising an automatic activation
    that the backend does not perform.
    """

    @pytest.fixture(scope="class")
    def copies(self) -> dict:
        return {
            language: json.loads(
                (LOCALES_DIR / language / "register.json").read_text(
                    encoding="utf-8"
                )
            )["selectedPlan"]
            for language in ("en", "ru")
        }

    def test_the_english_copy_is_the_agreed_wording(self, copies):
        """The English copy is owned by another branch.

        It must be the legacy promise (until that branch lands) or the
        agreed wording -- and nothing else.
        """
        assert copies["en"] in (
            LEGACY_EN_REGISTER_COPY,
            AGREED_EN_REGISTER_COPY,
        ), "the English register copy is neither the legacy copy nor the " "agreed wording"

    def test_the_russian_copy_is_the_agreed_wording(self, copies):
        assert copies["ru"] == AGREED_RU_REGISTER_COPY

    def test_the_copy_still_names_the_plan_the_visitor_selected(self, copies):
        for language, copy in copies.items():
            assert "{{plan}}" in copy, (
                f"the {language} register copy must still name the plan the "
                "visitor selected"
            )

    def test_the_copy_says_the_account_starts_on_the_entitled_plan(self, copies):
        assert "начинает с того плана" in copies["ru"], (
            "the Russian register copy must say that a new account starts "
            "on the tier it is entitled to"
        )
        if copies["en"] != LEGACY_EN_REGISTER_COPY:
            assert "start on the plan they are entitled to" in copies["en"], (
                "the English register copy must say that a new account "
                "starts on the tier it is entitled to"
            )

    def test_the_copy_points_at_a_real_channel(self, copies):
        assert "напишите нам" in copies["ru"], (
            "the Russian register copy must point the visitor at a real "
            "channel -- contact us -- because the backend does not persist "
            "the plan they asked for (api/crud.py:1082-1089 only logs it)"
        )
        if copies["en"] != LEGACY_EN_REGISTER_COPY:
            assert "contact us" in copies["en"], (
                "the English register copy must point the visitor at a real "
                "channel -- contact us -- because the backend does not "
                "persist the plan they asked for (api/crud.py:1082-1089 only "
                "logs it)"
            )

    def test_the_copy_does_not_promise_a_self_serve_plan_change(self, copies):
        assert "в любой момент" not in copies["ru"], (
            "the Russian register copy must not promise that the customer "
            "can change their plan at any time"
        )
        if copies["en"] != LEGACY_EN_REGISTER_COPY:
            assert "upgrade" not in copies["en"].lower(), (
                "the English register copy must not promise that the customer "
                "can change their plan themselves"
            )
