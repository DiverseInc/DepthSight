# tests/test_public_pricing_matches_catalog.py
"""The PUBLIC marketing page must sell exactly what the backend sells.

THE DEFECT THIS PINS
--------------------
`frontend/welcome.html` is the page that is actually served at /welcome.
caddy.config matches `/welcome` and rewrites it to the static file, so it
OVERRIDES the React SPA -- and therefore overrides the React Pricing
component that PR #36 rewired to the live catalog.

The static page carried its own hand-written pricing table, and it sold
plans the backend does not sell:

  * a "$29 Pro" tier   -- the backend's top tier is $100 and is called
                          "Professional"; there is no $29 tier at all;
  * a "$99 Team" tier  -- the backend sells NO team tier, no seats and no
                          RBAC; `api/plans_config.yml` has three plans:
                          free, standard, pro;
  * per-tier quotas belonging to no tier -- "100 backtest runs / day" and
                          "500 AI Co-Pilot requests / day" on the $29 card,
                          "Unlimited backtest runs" on the $99 card.

The same wrong numbers were repeated in the JSON-LD `Offer` array, which is
what search engines actually index. Every call to action pointed at
`#waitlist` or `mailto:`, so a prospect could not sign up even though
/register is live and already understands `?plan=`. The page also promised a
"$20 credit" that no backend code grants, and described "Stripe Checkout in
test mode" when the billing provider is Bitcart and there is no annual
prepay.

WHAT THIS TEST DOES
-------------------
It parses the static page AND its JSON-LD and asserts that:

  * every advertised tier name and price equals the live catalog's;
  * the page advertises NO tier the catalog lacks (and omits none it has);
  * each card's bullets are the catalog's own derived entitlement labels, so
    a card cannot quietly claim a quota the tier does not grant;
  * each card's call to action points at the live registration path with the
    catalog's real plan key;
  * no price on the page is a price the catalog does not sell;
  * the Docker-service count on the page equals the real number of services
    in docker-compose.yml.

WHY THE CATALOG IS READ FROM THE CONFIG, NOT FETCHED
----------------------------------------------------
The JSON-LD has to be static markup for search engines, so the page cannot
render prices from a runtime fetch -- a fetch would leave the indexable
markup wrong. The page is therefore checked at test time against the source
the catalog is built from: `api/plans_config.yml`, through the same
`build_plan_catalog()` the live `GET /api/v1/public/plans` endpoint uses.
"""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

from api.plans import build_plan_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
WELCOME_HTML = REPO_ROOT / "frontend" / "welcome.html"
PLANS_CONFIG = REPO_ROOT / "api" / "plans_config.yml"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# The plan key whose CTA is the plain registration page (it is free, so
# there is nothing to choose). Mirrors frontend/src/lib/pricing.ts.
FREE_PLAN_KEY = "free"

# The two backtest engines the catalog sells, as quota keys.
VECTOR_QUOTA_KEY = "run_vector_backtest_per_day"
KLINE_QUOTA_KEY = "run_kline_backtest_per_day"


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def _norm(chunks) -> str:
    """Collapse the text chunks of an element into one tidy string."""
    return " ".join("".join(chunks).split())


class _PricingCardParser(HTMLParser):
    """Pulls the pricing cards out of the static marketing page.

    A card is a <div class="price ...">. Inside it we want the tier name
    (<h3>), the price (<div class="amt">), the blurb (<p class="desc">), the
    included bullets (the plain <ul>), the not-included bullets
    (<ul class="notinc">) and the call to action (<a class="btn ...">).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self._card: dict | None = None
        self._depth = 0
        self._capture: str | None = None
        self._buf: list[str] = []
        self._list_mode: str | None = None
        self._li_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()

        if tag == "div" and "price" in classes and self._card is None:
            self._card = {
                "key": attributes.get("data-plan-key"),
                "name": None,
                "price_text": None,
                "description": None,
                "included": [],
                "not_included": [],
                "cta": None,
            }
            self._depth = 1
            return

        if self._card is None:
            return

        if tag == "div":
            self._depth += 1
            if "amt" in classes and self._capture is None:
                self._capture, self._buf = "price_text", []
            return

        if tag == "h3" and self._capture is None:
            self._capture, self._buf = "name", []
        elif tag == "p" and "desc" in classes and self._capture is None:
            self._capture, self._buf = "description", []
        elif tag == "ul":
            self._list_mode = "not_included" if "notinc" in classes else "included"
        elif tag == "li" and self._list_mode:
            self._li_buf = []
        elif tag == "a" and "btn" in classes and self._card["cta"] is None:
            self._card["cta"] = attributes.get("href")

    def handle_endtag(self, tag):
        if self._card is None:
            return

        if tag in ("h3", "p") and self._capture:
            self._card[self._capture] = _norm(self._buf)
            self._capture, self._buf = None, []
            return

        if tag == "div":
            if self._capture == "price_text":
                self._card["price_text"] = _norm(self._buf)
                self._capture, self._buf = None, []
            self._depth -= 1
            if self._depth == 0:
                self.cards.append(self._card)
                self._card = None
                self._list_mode = None
            return

        if tag == "li" and self._list_mode:
            self._card[self._list_mode].append(_norm(self._li_buf))
            self._li_buf = []
            return

        if tag == "ul":
            self._list_mode = None

    def handle_data(self, data):
        if self._capture is not None:
            self._buf.append(data)
        elif self._list_mode is not None:
            self._li_buf.append(data)


def _price_of(card: dict) -> float:
    """The price the card shows, as a number.

    The pattern deliberately captures a DECIMAL too, so a card reading
    "$30.50" is compared as 30.5 against the catalog's 30 and fails, rather
    than being truncated to 30 and passing.
    """
    match = _PRICE_RE.search(card["price_text"] or "")
    assert match, (
        f"pricing card {card.get('name')!r} has no $ price: {card['price_text']!r}"
    )
    return float(match.group(1))


def _json_ld_blocks(page: str) -> list:
    blocks = []
    for match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S
    ):
        blocks.append(json.loads(match.group(1)))
    return blocks


# A price with an optional decimal part, so "$30.50" cannot be silently
# truncated to 30 and compared equal to a $30 plan.
_PRICE_RE = re.compile(r"\$(\d+(?:\.\d+)?)")

# A number that is attached to a QUOTA-like word. Numbers attached to
# anything else (service counts, exchange counts, timeframes) are not
# quotas and are checked separately.
_QUOTA_NUMBER_RE = re.compile(
    r"(\d+)\s+(?:"
    r"backtests?\b|backtest\s+runs?\b|"
    r"AI(?:\s+Assistant)?(?:\s+queries|\s+requests)?\b|"
    r"queries\b|requests?\b|runs?\b|"
    r"optimization\s+runs?\b|genetic\s+searches\b|model\s+trainings?\b|"
    r"live\s+(?:trading\s+)?strategies\b|strategies\b|"
    r"seats?\b|days?\b|months?\b|retention\b"
    r")",
    re.IGNORECASE,
)


def _quota_numbers(text: str) -> list:
    """The numbers in `text` that are attached to a quota-like word."""
    return [match.group(1) for match in _QUOTA_NUMBER_RE.finditer(text)]


def _prose(page: str) -> str:
    """The page with the pricing table and the JSON-LD removed.

    This is the part of the page that is not per-tier: the hero, the feature
    list, the tech list and the contact section. Anything it claims applies
    to the whole product, so it cannot name a quota that only one tier gets.
    """
    without_pricing = re.sub(
        r"<section id=\"pricing\">.*?</section>", " ", page, flags=re.S
    )
    return re.sub(
        r"<script[^>]*application/ld\+json[^>]*>.*?</script>",
        " ",
        without_pricing,
        flags=re.S,
    )


def _software_application(page: str) -> dict:
    for block in _json_ld_blocks(page):
        for node in block.get("@graph", [block]):
            if node.get("@type") == "SoftwareApplication":
                return node
    raise AssertionError("the page carries no JSON-LD SoftwareApplication node")


def _compose_service_count() -> int:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    return len(compose.get("services", {}))


# --------------------------------------------------------------------------
# Which engine does a tier's daily backtest allowance buy?
# --------------------------------------------------------------------------
def _quotas(raw_plan: dict) -> dict:
    return raw_plan.get("quotas", {}) or {}


def _is_precision_only_quoteless(raw_plan: dict) -> bool:
    """True when the tier may NOT use the Precision (kline) backtest engine.

    `run_kline_backtest_per_day: 0` means exactly that:
    `check_usage_quota("run_kline_backtest")` (api/dependencies.py:99-114)
    reads the limit, and `current_usage >= limit` is true on the very first
    request, so every Precision run is answered with a 429. The allowance a
    such a tier does have is therefore the OTHER engine -- the Turbo/vector
    one -- and nothing else.
    """
    return _quotas(raw_plan).get(KLINE_QUOTA_KEY, 0) == 0


def _vector_allowance(raw_plan: dict) -> int:
    """The daily vector (Turbo/fast) backtest allowance of a tier."""
    return int(_quotas(raw_plan).get(VECTOR_QUOTA_KEY, 0) or 0)


def _fast_allowance_sentence(raw_plan: dict) -> str:
    """The sentence a card must use for the daily backtest allowance.

    `api/plans.py` derives the entitlement label "<N> backtests per day" from
    the quota, and that label does not say WHICH engine it buys. The
    backend's own words for the vector engine are "fast": the catalog's
    free-tier feature copy says "20 fast backtests per day"
    (api/plans_config.yml:39) and the AI assistant describes the Turbo
    (vector) engine as "extremely fast" (api/ai_assistant.py:391).

    A card that says "20 backtests per day" without that word is read as
    "any backtest", so a Free reader would expect the Precision engine --
    which their tier cannot run at all.
    """
    allowance = _vector_allowance(raw_plan)
    assert allowance > 0, "this helper only applies to a tier with an allowance"
    return f"{allowance} fast backtests per day"


def _has_bybit_live_exception(raw_plan: dict) -> bool:
    """True when the tier may trade live on Bybit ONLY.

    `allow_free_bybit_trading: true` with no `allow_real_trading` permission
    means exactly that. The enforcement is real and measured:

      * api/routes/strategies.py:538-549 rejects any non-Bybit key for such
        a tier with 403 ("Trading on your plan is only allowed using Bybit
        API keys.");
      * api/depthsight_api.py:634-640 caps that tier at
        `max_free_bybit_live_strategies` live strategies.

    The catalog's derived label ("Live trading", excluded) cannot express
    that exception, so the card has to say it in words.
    """
    limits = raw_plan.get("limits", {}) or {}
    permissions = raw_plan.get("permissions", []) or []
    return bool(limits.get("allow_free_bybit_trading", False)) and (
        "allow_real_trading" not in permissions
    )


def _bybit_live_allowance(raw_plan: dict) -> int:
    """How many live strategies a Bybit-only tier may run."""
    limits = raw_plan.get("limits", {}) or {}
    return int(limits.get("max_free_bybit_live_strategies", 0) or 0)


# The two bullets a Bybit-only tier must show instead of the blanket
# "Live trading" / "Live trading strategies" exclusions.
BYBIT_LIVE_LABEL = "Live trading on Bybit (up to {n} live trading strategies)"
OTHER_VENUES_LABEL = "Live trading on other venues"

# The venues the platform routes to. Measured from the live-trading modal in
# the frontend (frontend/src/components/.../live-trading) and from
# api/routes/strategies.py:538-554, which restricts a tier to Bybit ONLY when
# the tier has no `allow_real_trading` permission.
ALL_VENUES = ("OKX", "Binance", "Bybit", "Bitget", "Gate.io", "BingX")
ALL_VENUES_LABEL = "Live trading on all six exchanges ({venues})".format(
    venues=", ".join(ALL_VENUES)
)


def _allows_every_venue(raw_plan: dict) -> bool:
    """True when the tier may trade live on every venue the platform has.

    api/routes/strategies.py:538-554 rejects a key for a venue the tier may
    not use ONLY when the tier has no `allow_real_trading` permission, so a
    paid tier can trade on all six; and
    api/depthsight_api.py:545-548 (allows_all_keys) keeps every active key
    for such a tier.
    """
    permissions = raw_plan.get("permissions", []) or []
    return "allow_real_trading" in permissions


def _can_save_strategies(raw_plan: dict) -> bool:
    """True when the tier can save strategy configurations.

    The catalog derives "Saved strategies" from the `save_strategy_config`
    PERMISSION, and that permission is enforced NOWHERE: the route
    POST /api/v1/strategies/config (api/routes/strategies.py:308-317) takes
    only `get_current_user` -- no `require_permission` -- and
    crud.create_strategy_config (api/crud.py:2269-2277) has no plan check.
    The frontend calls that route from the strategy editor with no plan gate
    either (frontend/src/lib/api.ts:1210-1220).

    So a tier without the permission can still save strategies, and the
    card must not claim a restriction the backend does not impose.
    """
    permissions = raw_plan.get("permissions", []) or []
    return "save_strategy_config" not in permissions


def _expected_included_labels(plan: dict, raw_plan: dict) -> set:
    """The bullets a card must list, with the enforced exceptions applied."""
    labels = {e["label"] for e in plan["entitlements"] if e["included"]}

    if not _is_precision_only_quoteless(raw_plan):
        pass
    else:
        allowance = _vector_allowance(raw_plan)
        if allowance > 0:
            plain = f"{allowance} backtests per day"
            if plain in labels:
                labels.discard(plain)
                labels.add(f"{allowance} fast backtests per day")

    if _has_bybit_live_exception(raw_plan):
        labels.add(
            BYBIT_LIVE_LABEL.format(n=_bybit_live_allowance(raw_plan))
        )

    if _allows_every_venue(raw_plan):
        # The catalog's label names three of the six venues
        # (api/plans.py:280) while the backend lets a paid tier trade on all
        # six (api/routes/strategies.py:538-554). The card says what the
        # backend actually allows.
        labels.discard("Live trading on OKX, Binance, Bybit")
        labels.add(ALL_VENUES_LABEL)

    if _can_save_strategies(raw_plan):
        labels.add("Saved strategies")

    return labels


def _expected_excluded_labels(plan: dict, raw_plan: dict) -> set:
    """The "not included" labels a card may list."""
    labels = {e["label"] for e in plan["entitlements"] if not e["included"]}

    if _has_bybit_live_exception(raw_plan):
        # The blanket exclusions are wrong for this tier: it DOES get live
        # trading, on Bybit only.
        labels.discard("Live trading")
        labels.discard("Live trading strategies")
        labels.add(OTHER_VENUES_LABEL)

    if _can_save_strategies(raw_plan):
        # The backend does not enforce the permission, so the tier can save
        # strategies and must not be told it cannot.
        labels.discard("Saved strategies")

    return labels


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def page() -> str:
    return WELCOME_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def catalog() -> dict:
    return build_plan_catalog()


@pytest.fixture(scope="module")
def plans_by_name(catalog) -> dict:
    return {plan["name"]: plan for plan in catalog["plans"]}


@pytest.fixture(scope="module")
def raw_plans() -> dict:
    """The raw plans_config.yml, keyed by plan key.

    The catalog builder derives labels from the quotas; this fixture gives the
    test the quotas themselves, so a rule can be written against the machine-
    readable value (0, -1, 20, ...) instead of against a label.
    """
    config = yaml.safe_load(PLANS_CONFIG.read_text(encoding="utf-8"))
    return config["plans"]


@pytest.fixture(scope="module")
def cards(page) -> list:
    parser = _PricingCardParser()
    parser.feed(page)
    return parser.cards


# --------------------------------------------------------------------------
# The catalog is the source of truth
# --------------------------------------------------------------------------
class TestTheCatalogIsTheSourceOfTruth:
    def test_the_catalog_is_built_from_the_config_file(self, catalog):
        """The prices the page must show come from api/plans_config.yml."""
        config = yaml.safe_load(PLANS_CONFIG.read_text(encoding="utf-8"))
        mode = config.get("billing", {}).get("mode", "monthly")

        assert {p["key"] for p in catalog["plans"]} == set(config["plans"])

        for plan in catalog["plans"]:
            raw = config["plans"][plan["key"]]
            assert plan["name"] == raw["name"]
            expected = raw.get("billing", {}).get(mode, {}).get(
                "price_usd", raw["price_usd"]
            )
            assert plan["price_usd"] == expected

    def test_the_catalog_sells_three_tiers_and_no_team_tier(self, catalog):
        assert {p["key"] for p in catalog["plans"]} == {"free", "standard", "pro"}
        assert "team" not in {p["key"] for p in catalog["plans"]}
        assert "Team" not in {p["name"] for p in catalog["plans"]}


# --------------------------------------------------------------------------
# The page must not sell anything else
# --------------------------------------------------------------------------
class TestThePageSellsExactlyTheCatalog:
    def test_the_page_advertises_every_catalog_tier_and_no_others(
        self, cards, plans_by_name
    ):
        advertised = {card["name"] for card in cards}

        assert advertised, "the page has no pricing cards at all"
        assert advertised == set(plans_by_name), (
            "the page advertises tiers the catalog does not sell, or omits "
            f"tiers it does sell. advertised={sorted(advertised)} "
            f"catalog={sorted(plans_by_name)}"
        )

    def test_no_card_is_a_team_tier(self, cards):
        for card in cards:
            assert "team" not in (card["name"] or "").lower()

    def test_every_card_price_equals_the_catalog_price(self, cards, plans_by_name):
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None, f"unknown tier on the page: {card['name']!r}"
            assert _price_of(card) == plan["price_usd"], (
                f"the page advertises {card['name']} at ${_price_of(card)} but the "
                f"catalog sells it at ${plan['price_usd']}"
            )

    def test_the_free_tier_is_advertised_as_free(self, cards, plans_by_name):
        free = [c for c in cards if c["name"] == plans_by_name["Free"]["name"]]
        assert free, "the page does not advertise the Free tier"
        assert _price_of(free[0]) == 0
        assert "forever" in (free[0]["price_text"] or "").lower(), (
            "the Free tier is priced at $0 and must not be billed per month"
        )

    def test_a_paid_tier_is_advertised_per_month(self, cards, plans_by_name):
        for card in cards:
            if card["name"] == plans_by_name["Free"]["name"]:
                continue
            assert "month" in (card["price_text"] or "").lower(), (
                f"the paid tier {card['name']!r} must state its billing period"
            )

    def test_the_page_contains_no_price_the_catalog_does_not_sell(self, page, catalog):
        allowed = {float(plan["price_usd"]) for plan in catalog["plans"]}
        for amount in _PRICE_RE.findall(page):
            assert float(amount) in allowed, (
                f"the page advertises ${amount}, which is not a price the "
                f"catalog sells (catalog prices: {sorted(allowed)})"
            )

    def test_each_card_blurb_is_the_catalog_description(self, cards, plans_by_name):
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            assert card["description"] == plan["description"], (
                f"the {card['name']} blurb does not match the catalog description"
            )


# --------------------------------------------------------------------------
# The per-tier entitlements must be the catalog's own
# --------------------------------------------------------------------------
class TestTheEntitlementsAreTheCatalogsOwn:
    def test_every_included_bullet_is_a_label_the_tier_grants(
        self, cards, plans_by_name, raw_plans
    ):
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            granted = _expected_included_labels(plan, raw_plans[plan["key"]])

            assert set(card["included"]) == granted, (
                f"the {card['name']} card does not list exactly the entitlements "
                f"the catalog grants. missing={sorted(granted - set(card['included']))} "
                f"extra={sorted(set(card['included']) - granted)}"
            )

    def test_no_card_claims_a_quota_from_another_tier(
        self, cards, plans_by_name, raw_plans
    ):
        """The old page gave the $29 tier 'Unlimited backtest runs'."""
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            mine = _expected_included_labels(plan, raw_plans[plan["key"]])

            for other_name, other in plans_by_name.items():
                if other_name == card["name"]:
                    continue
                theirs = _expected_included_labels(other, raw_plans[other["key"]])
                for label in theirs - mine:
                    assert label not in card["included"], (
                        f"the {card['name']} card claims "
                        f"{label!r}, which belongs to the "
                        f"{other_name} tier"
                    )

    def test_every_not_included_label_is_genuinely_excluded(
        self, cards, plans_by_name, raw_plans
    ):
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            excluded = _expected_excluded_labels(plan, raw_plans[plan["key"]])

            for label in card["not_included"]:
                assert label in excluded, (
                    f"the {card['name']} card lists {label!r} as not included, "
                    "but the catalog does not exclude it"
                )


# --------------------------------------------------------------------------
# The daily backtest allowance must name the engine it buys
# --------------------------------------------------------------------------
class TestTheBacktestAllowanceNamesTheEngine:
    """A tier without the Precision engine may only run the fast one.

    `run_kline_backtest_per_day` is 0 for Free and Standard, and
    `check_usage_quota` (api/dependencies.py:99-114) answers every request
    with a 429 once `current_usage >= limit`. Their daily allowance buys
    the Turbo/vector engine ONLY -- which the backend itself calls "fast"
    (api/plans_config.yml:39, api/ai_assistant.py:391).
    """

    def test_a_tier_without_the_precision_engine_says_fast_backtests(
        self, cards, plans_by_name, raw_plans
    ):
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            raw = raw_plans[plan["key"]]

            if not _is_precision_only_quoteless(raw):
                continue
            allowance = _vector_allowance(raw)
            if allowance <= 0:
                continue

            expected = f"{allowance} fast backtests per day"
            assert expected in card["included"], (
                f"the {card['name']} card must say {expected!r}: this tier may "
                f"not use the Precision (kline) engine "
                f"({KLINE_QUOTA_KEY} is 0), so its {allowance} daily backtests "
                f"are the fast (Turbo/vector) engine only"
            )
            assert f"{allowance} backtests per day" not in card["included"], (
                f"the {card['name']} card states {allowance} backtests per day "
                "without saying which engine that is"
            )

    def test_the_page_never_states_an_allowance_without_the_engine(
        self, page, catalog, raw_plans
    ):
        """The wording rule holds in EVERY block, not just in the cards.

        The same number can be quoted in a bullet, in the indexed JSON-LD
        offer description and in the contact section; none of them may drop
        the qualifier.
        """
        for plan in catalog["plans"]:
            raw = raw_plans[plan["key"]]
            if not _is_precision_only_quoteless(raw):
                continue
            allowance = _vector_allowance(raw)
            if allowance <= 0:
                continue

            qualified = f"{allowance} fast backtests per day"
            plain = f"{allowance} backtests per day"
            remainder = page.replace(qualified, "")

            assert plain not in remainder, (
                f"the page states {plain!r} somewhere without the word 'fast'. "
                f"This tier has {KLINE_QUOTA_KEY}: 0, so every one of its "
                f"backtests is the fast (Turbo/vector) engine"
            )

    def test_a_tier_without_the_precision_engine_lists_it_as_excluded(
        self, cards, plans_by_name, raw_plans
    ):
        """The card must also say which engine is NOT included."""
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            raw = raw_plans[plan["key"]]

            if not _is_precision_only_quoteless(raw):
                continue

            excluded = {e["label"] for e in plan["entitlements"] if not e["included"]}
            kline_labels = {label for label in excluded if "kline" in label.lower()}
            assert kline_labels, (
                f"the catalog does not exclude the Precision engine from the "
                f"{card['name']} tier, so this test needs re-measuring"
            )

            for label in kline_labels:
                assert label in card["not_included"], (
                    f"the {card['name']} card must list {label!r} as not "
                    "included: the tier cannot run that engine at all"
                )


# --------------------------------------------------------------------------
# What a tier may actually trade live on, and how a paid tier is obtained
# --------------------------------------------------------------------------
class TestTheLiveTradingAndPurchaseClaims:
    """The card must say what the tier CAN trade, and how to get a paid tier."""

    def test_a_tier_with_the_bybit_exception_says_it_can_trade_live_on_bybit(
        self, cards, plans_by_name, raw_plans
    ):
        """The derived label says "Live trading" is excluded -- and is wrong.

        `allow_free_bybit_trading: true` (api/plans_config.yml:87-88) lets a
        Free account trade live on Bybit, up to
        `max_free_bybit_live_strategies` strategies. The backend enforces it
        (api/routes/strategies.py:538-549 rejects any other exchange with a
        403; api/depthsight_api.py:634-640 applies the cap), and the catalog's
        derived label cannot express the exception, so the card must.
        """
        seen = False
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            raw = raw_plans[plan["key"]]

            if not _has_bybit_live_exception(raw):
                continue
            seen = True

            expected = BYBIT_LIVE_LABEL.format(n=_bybit_live_allowance(raw))
            assert expected in card["included"], (
                f"the {card['name']} card must list {expected!r}: this tier "
                "may trade live on Bybit, up to "
                f"{_bybit_live_allowance(raw)} live strategies"
            )
            assert "Live trading" not in card["not_included"], (
                f"the {card['name']} card lists 'Live trading' as not "
                "included, but this tier DOES get live trading on Bybit"
            )
            assert "Live trading strategies" not in card["not_included"], (
                f"the {card['name']} card lists 'Live trading strategies' as "
                "not included, but this tier may run live strategies"
            )
            assert OTHER_VENUES_LABEL in card["not_included"], (
                f"the {card['name']} card must say {OTHER_VENUES_LABEL!r}: "
                "every other venue is blocked for this tier"
            )

        assert seen, (
            "no tier in the catalog has the Bybit-only live-trading "
            "exception; re-measure this test"
        )

    def test_a_tier_that_may_trade_everywhere_says_so(self, cards, plans_by_name, raw_plans):
        """A paid tier may trade on every venue the platform has.

        api/routes/strategies.py:538-554 restricts a tier to Bybit ONLY when
        it has no `allow_real_trading` permission, so a paid tier is not
        limited to the three venues the catalog's label names.
        """
        seen = False
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            raw = raw_plans[plan["key"]]

            if not _allows_every_venue(raw):
                continue
            seen = True

            assert ALL_VENUES_LABEL in card["included"], (
                f"the {card['name']} card must list {ALL_VENUES_LABEL!r}: this "
                "tier may trade live on every venue the platform routes to"
            )
            assert "Live trading on OKX, Binance, Bybit" not in card["included"], (
                f"the {card['name']} card names only three venues, but this "
                "tier may trade on all of them"
            )

        assert seen, (
            "no tier has the allow_real_trading permission; re-measure this "
            "test"
        )

    def test_the_page_advertises_no_feature_that_does_not_exist(self, page):
        """The prose is a promise too, and it is not covered by the labels.

        The page used to advertise "Walk-forward, monte-carlo, and genetic
        optimization out of the box". There is no Monte-Carlo anything in
        this codebase: a case-insensitive search for "monte" across api/ and
        bot_module/ finds NOTHING, and the only walk-forward code is the
        `walk_forward_oos_ratio` option inside the genetic strategy finder
        (bot_module/genetic_strategy_finder.py:924).
        """
        implementation_dirs = [REPO_ROOT / "api", REPO_ROOT / "bot_module"]
        for directory in implementation_dirs:
            for path in directory.rglob("*.py"):
                text = path.read_text(encoding="utf-8", errors="replace")
                assert "monte" not in text.lower(), (
                    f"{path.relative_to(REPO_ROOT)} now mentions 'monte': if "
                    "that is a Monte-Carlo implementation, lift the ban below "
                    "and say what the page may claim"
                )

        lowered = page.lower()
        for phrase in ("monte-carlo", "monte carlo"):
            assert phrase not in lowered, (
                f"the page advertises {phrase!r}; the backend has no such "
                "feature (no 'monte' anywhere in api/ or bot_module/)"
            )

    def test_a_tier_that_can_save_strategies_is_not_told_it_cannot(
        self, cards, plans_by_name, raw_plans
    ):
        """The catalog's permission is not enforced, so do not claim it is.

        `save_strategy_config` is enforced nowhere: POST
        /api/v1/strategies/config (api/routes/strategies.py:308-317) takes
        only `get_current_user`, and crud.create_strategy_config
        (api/crud.py:2269-2277) has no plan check.
        """
        seen = False
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            raw = raw_plans[plan["key"]]

            if not _can_save_strategies(raw):
                continue
            seen = True

            assert "Saved strategies" in card["included"], (
                f"the {card['name']} card must list 'Saved strategies': this "
                "tier can save strategy configurations -- the backend does "
                "not enforce the permission"
            )
            assert "Saved strategies" not in card["not_included"], (
                f"the {card['name']} card tells the reader it cannot save "
                "strategies, but the backend lets it"
            )

        assert seen, (
            "no tier lacks the save_strategy_config permission; re-measure "
            "this test"
        )

    def test_an_indexed_live_trading_claim_names_the_tiers_it_is_about(
        self, page
    ):
        """The featureList is indexed, so its claims are tiered too.

        The Free tier gets live trading on Bybit only, so an unscoped
        "Live trading on 6 exchanges" tells a search engine -- and a
        prospect -- that every tier gets all six.
        """
        for feature in _software_application(page).get("featureList", []):
            if "live trading" not in feature.lower():
                continue
            assert "paid plan" in feature.lower(), (
                f"the indexed feature {feature!r} claims live trading without "
                "saying which tiers get it: the Free tier gets Bybit only"
            )

    def test_the_page_promises_no_self_serve_upgrade(self, page):
        """There is no self-serve upgrade in the deployment.

        A paid tier is granted by the payment webhook (api/routes/payments.py)
        or by an operator. The webhook needs a payment provider, and the
        repository ships no provider in its default deployment: docker-compose.yml
        has no Bitcart service, the provider arrives only through the opt-in
        docker-compose.bitcart.yml overlay (deploy.sh:135,222-228), and
        api/routes/payments.py:121-125 raises "Payment provider is not
        configured" without it.

        So the page may not promise an upgrade the visitor cannot perform.
        """
        lowered = page.lower()
        assert "upgrade" not in lowered, (
            "the page promises an upgrade path that the deployment does not "
            "provide: the payment provider is opt-in and is not running"
        )
        assert "new accounts start on the free plan" in lowered, (
            "the page must say that a new account starts on the Free tier"
        )

    def test_a_paid_offer_is_not_indexed_at_a_signup_that_cannot_grant_it(
        self, page, plans_by_name
    ):
        """A paid tier is never granted at signup.

        api/routes/auth.py:369-373 and api/crud.py:1078-1089: the plan in
        the signup is a REQUEST, the account starts on the tier it is
        entitled to (the trial tier, otherwise free), and a paid tier is
        never granted at signup. Indexing a $30 offer at the signup URL
        tells a search engine the tier can be bought there.
        """
        for offer in _software_application(page)["offers"]:
            plan = plans_by_name.get(offer["name"])
            assert plan is not None

            if plan["key"] == FREE_PLAN_KEY:
                assert offer["url"].startswith(
                    "https://depthsight.diverseinc.net/register"
                ), "the Free offer is indexed at the signup, where a free account really can be created"
                continue

            assert "/register" not in offer["url"], (
                f"the indexed {offer['name']} offer points at {offer['url']!r}, "
                "but a paid tier is never granted at signup"
            )
            assert offer["url"].startswith("https://depthsight.diverseinc.net")


# --------------------------------------------------------------------------
# A prospect must be able to START
# --------------------------------------------------------------------------
class TestAProspectCanActuallySignUp:
    def test_each_card_points_at_registration_with_its_real_plan_key(
        self, cards, plans_by_name
    ):
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            key = plan["key"]

            if key == FREE_PLAN_KEY:
                expected = "/register"
            else:
                expected = f"/register?plan={key}"

            assert card["cta"] == expected, (
                f"the {card['name']} card calls to action {card['cta']!r}; it "
                f"must point at {expected!r} so the prospect can start"
            )

    def test_the_plan_keys_used_by_the_page_are_real_catalog_keys(
        self, cards, catalog
    ):
        real = {plan["key"] for plan in catalog["plans"]}
        for card in cards:
            key = card.get("key")
            assert key, f"the {card['name']!r} card does not declare its plan key"
            assert key in real, f"{key!r} is not a plan the catalog sells"

    def test_each_card_declares_the_plan_key_of_the_tier_it_shows(
        self, cards, plans_by_name
    ):
        """A card must not claim one tier's name and another tier's key."""
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            assert card.get("key") == plan["key"], (
                f"the {card['name']!r} card declares plan key "
                f"{card.get('key')!r}, but that tier's catalog key is "
                f"{plan['key']!r}"
            )

    def test_the_primary_calls_to_action_point_at_registration(self, page):
        assert 'class="nav-cta" href="/register"' in page, (
            "the nav call to action must offer registration, not a waitlist"
        )
        assert 'class="btn btn-primary" href="/register"' in page, (
            "the hero call to action must offer registration, not a waitlist"
        )

    def test_the_page_does_not_only_offer_the_waitlist(self, page):
        assert 'href="#waitlist"' not in page, (
            "no call to action may point at the waitlist anchor any more: a "
            "prospect must be able to sign up"
        )


# --------------------------------------------------------------------------
# The JSON-LD is what search engines index
# --------------------------------------------------------------------------
class TestTheJsonLdOffers:
    def test_the_offers_are_the_catalog_tiers(self, page, plans_by_name):
        offers = _software_application(page)["offers"]
        assert {offer["name"] for offer in offers} == set(plans_by_name), (
            "the indexed offers must be exactly the tiers the catalog sells"
        )

    def test_every_offer_price_equals_the_catalog_price(self, page, plans_by_name):
        for offer in _software_application(page)["offers"]:
            plan = plans_by_name.get(offer["name"])
            assert plan is not None, f"unknown offer: {offer['name']!r}"
            assert float(offer["price"]) == plan["price_usd"], (
                f"the indexed {offer['name']} offer says {offer['price']} but the "
                f"catalog sells it at {plan['price_usd']}"
            )
            assert offer["priceCurrency"] == "USD"

    def test_the_indexed_offers_point_at_this_site(self, page):
        for offer in _software_application(page)["offers"]:
            assert offer["url"].startswith("https://depthsight.diverseinc.net"), (
                f"the indexed {offer['name']} offer points off-site: "
                f"{offer['url']!r}"
            )

    def test_no_offer_description_quotes_a_number_the_tier_does_not_grant(
        self, page, plans_by_name
    ):
        """The old JSON-LD said '100 backtest runs / day' for a $29 tier."""
        for offer in _software_application(page)["offers"]:
            plan = plans_by_name.get(offer["name"])
            assert plan is not None
            known = set()
            for entitlement in plan["entitlements"]:
                known.update(re.findall(r"\d+", entitlement["label"]))

            for number in re.findall(r"\d+", offer.get("description", "")):
                assert number in known, (
                    f"the indexed {offer['name']} offer quotes {number}, which "
                    "the catalog does not grant that tier"
                )

    def test_no_offer_description_names_an_entitlement_the_tier_lacks(
        self, page, plans_by_name
    ):
        """A Free offer must not say 'Unlimited backtests'."""
        for offer in _software_application(page)["offers"]:
            plan = plans_by_name.get(offer["name"])
            assert plan is not None
            granted = {e["label"] for e in plan["entitlements"] if e["included"]}
            excluded = {e["label"] for e in plan["entitlements"] if not e["included"]}
            description = offer.get("description", "")

            for label in excluded:
                assert label not in description, (
                    f"the indexed {offer['name']} offer names {label!r}, which "
                    "the catalog EXCLUDES from that tier"
                )

            other_tiers_only = {
                entitlement["label"]
                for other_name, other in plans_by_name.items()
                if other_name != offer["name"]
                for entitlement in other["entitlements"]
                if entitlement["included"] and entitlement["label"] not in granted
            }
            for label in other_tiers_only:
                assert label not in description, (
                    f"the indexed {offer['name']} offer names {label!r}, which "
                    "belongs to another tier"
                )

    def test_the_indexed_block_makes_no_claim_the_cards_do_not_make(self, page):
        """The indexed block is a PUBLIC promise, so it obeys the same rules.

        Google indexes this JSON-LD. A promise that only exists here is
        still a promise a prospect reads.
        """
        for block in _json_ld_blocks(page):
            text = json.dumps(block)
            lowered = text.lower()

            assert "cancel" not in lowered, (
                "the indexed block promises a cancellation the backend does "
                "not implement"
            )
            for phrase in ("read-only", "read only", "readonly"):
                assert phrase not in lowered, (
                    f"the indexed block advertises {phrase!r} API keys; the "
                    "product has no read-only key concept (AGENTS.md:71)"
                )

    def test_the_indexed_offer_description_uses_the_same_backtest_wording(
        self, page, plans_by_name, raw_plans
    ):
        """The JSON-LD is what a search engine reads: same rule as the card."""
        for offer in _software_application(page)["offers"]:
            plan = plans_by_name.get(offer["name"])
            assert plan is not None
            raw = raw_plans[plan["key"]]

            if not _is_precision_only_quoteless(raw):
                continue
            allowance = _vector_allowance(raw)
            if allowance <= 0:
                continue

            description = offer.get("description", "")
            assert f"{allowance} fast backtests per day" in description, (
                f"the indexed {offer['name']} offer must say "
                f"{allowance} fast backtests per day: this tier may not use "
                f"the Precision (kline) engine ({KLINE_QUOTA_KEY} is 0)"
            )
            assert f"{allowance} backtests per day" not in description, (
                f"the indexed {offer['name']} offer states the backtest "
                "allowance without saying which engine it buys"
            )


# --------------------------------------------------------------------------
# Promises the backend does not keep
# --------------------------------------------------------------------------
class TestThePagePromisesOnlyWhatTheProductDoes:
    def test_the_page_promises_no_credit(self, page):
        assert "$20" not in page, (
            "no credit is granted by the backend, so the page must not "
            "promise one"
        )

    def test_the_page_does_not_describe_a_test_mode_checkout(self, page):
        assert "Stripe" not in page, (
            "the billing provider is Bitcart, and there is no Stripe checkout "
            "in this codebase at all"
        )
        assert "test mode" not in page.lower()

    def test_the_page_does_not_promise_an_annual_prepay(self, page):
        assert "Annual prepay" not in page, (
            "the catalog sells monthly billing; there is no annual prepay"
        )

    def test_the_page_promises_no_trial(self, page):
        """The backend grants a trial of ZERO days, so there is no trial."""
        assert "trial" not in page.lower(), (
            "registration_trial.days is 0, so no trial is granted and none "
            "may be advertised"
        )

    def test_the_page_promises_no_cancellation(self, page):
        """There is no route in the backend that cancels a subscription.

        Every "cancel" in api/ is something else: a Bitcart invoice status
        string (api/routes/payments.py:331-332), a websocket task
        cancellation (api/websocket_server.py) and the onboarding-email
        opt-out (api/onboarding_emails.py:275). Nothing cancels a plan, so
        the page may not promise it -- not as a headline and not in a
        footnote.
        """
        lowered = page.lower()
        assert "cancel" not in lowered, (
            "the backend has no cancellation route, so the page must not "
            "promise that a subscription can be cancelled"
        )

    def test_the_page_does_not_claim_read_only_api_keys(self, page):
        """The product has no read-only key concept.

        The only "read-only" in api/ describes the emergency-stop endpoint
        (api/routes/portfolio.py:802,811) -- a report that never changes
        state. The product's own rule for the keys a trader hands over is
        "API keys are Trade-only. Never Withdraw." (AGENTS.md:71), so a
        card may not advertise read-only keys: the trader creates them.
        """
        lowered = page.lower()
        for phrase in ("read-only", "read only", "readonly"):
            assert phrase not in lowered, (
                f"the page still advertises {phrase!r} API keys. The product "
                "has no read-only key concept; its own rule is 'API keys are "
                "Trade-only. Never Withdraw.' (AGENTS.md:71)"
            )

    def test_portfolio_backtesting_follows_the_permission_not_the_quota(
        self, cards, plans_by_name, raw_plans
    ):
        """Portfolio backtesting is gated by a PERMISSION.

        `run_portfolio_backtest_per_day` is 0 for every tier, but that quota
        is enforced NOWHERE. There are exactly two quota mechanisms in the
        backend and neither touches the portfolio route:

          * `check_usage_quota` (api/dependencies.py:90) is wired up only on
            the two backtest engines (api/routes/backtests.py:232) and on
            genetic search (api/routes/discovery.py:112);
          * `QuotaManager.check_and_consume` (api/quota_manager.py:22, whose
            limit 0 means "forbidden", api/quota_manager.py:71-72) is called
            from exactly one place: api/ai_assistant.py:1474, with the
            feature "use_ai_assistant".

        The portfolio endpoint (api/routes/portfolio.py:917-925) checks the
        `run_portfolio_backtest` PERMISSION and the concurrent-task limit,
        and the Celery task (tasks.py:1869-1902) checks no daily quota at
        all. The live catalog agrees: GET /api/v1/public/plans returns
        "Portfolio backtesting" as INCLUDED for Standard and Professional,
        and the live OpenAPI publishes /api/v1/portfolio-backtests.

        So the capability follows the permission -- Standard and Pro hold
        it, Free does not -- and the page must follow the permission too.
        """
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            permissions = raw_plans[plan["key"]].get("permissions", []) or []
            holds = "run_portfolio_backtest" in permissions

            claims = "Portfolio backtesting" in card["included"]
            assert claims == holds, (
                f"the {card['name']} card {'claims' if claims else 'does not claim'} "
                "portfolio backtesting, but that tier "
                f"{'holds' if holds else 'does not hold'} the "
                "run_portfolio_backtest permission"
            )

    def test_a_tier_without_a_capability_says_so(self, cards, plans_by_name):
        """The page states the boundary in BOTH directions.

        A card that says "Portfolio backtesting" on Standard and says
        nothing about it on Free leaves the reader guessing. The catalog
        excludes the capability from Free, so the Free card must say it.
        """
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            excluded = {e["label"] for e in plan["entitlements"] if not e["included"]}

            for label in excluded:
                if label not in ("Portfolio backtesting",):
                    continue
                assert label in card["not_included"], (
                    f"the {card['name']} card must list {label!r} as not "
                    "included: the catalog excludes it from that tier"
                )

    def test_no_card_quotes_a_number_that_is_not_a_quota_of_that_tier(
        self, cards, plans_by_name, raw_plans
    ):
        """A number next to a quota word must be one of THIS tier's numbers.

        Comparing labels catches a wrong label; this catches the same lie
        reworded ("100 backtest runs / day"), which no label comparison
        would notice.
        """
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            raw = raw_plans[plan["key"]]

            own = set()
            for label in _expected_included_labels(plan, raw) | _expected_excluded_labels(
                plan, raw
            ):
                own.update(re.findall(r"\d+", label))

            quoted = _quota_numbers(" ".join(card["included"] + card["not_included"]))
            for number in quoted:
                assert number in own, (
                    f"the {card['name']} card quotes {number} next to a quota "
                    "word, but the catalog grants that tier no such quota"
                )

    def test_the_page_advertises_no_team_or_seat_tier(self, page):
        """The old page sold a $99 Team tier with 5 seats and RBAC.

        The backend sells no team tier, no seats and no RBAC, so none of that
        vocabulary may survive anywhere on the page -- including the meta
        tags, which search engines index.
        """
        lowered = page.lower()
        for phrase in (
            "team seat",
            "team tier",
            "team plan",
            "team account",
            "rbac",
            "multi-user",
            "multi user",
        ):
            assert phrase not in lowered, (
                f"the page still advertises {phrase!r}; the backend sells no "
                "team tier, no seats and no RBAC"
            )
        assert not re.search(r"\bTeam\b", page), (
            "the page still advertises a 'Team' tier; the catalog has none"
        )

    def test_the_page_prose_claims_no_quota_that_only_one_tier_gets(
        self, page, catalog
    ):
        """The prose is not per-tier, so it cannot quote a tier's quota.

        The old page's hero and body copy were never checked by anything:
        a claim like "Unlimited strategies" outside the pricing cards would
        have passed every test.
        """
        known_numbers = set()
        for plan in catalog["plans"]:
            for entitlement in plan["entitlements"]:
                known_numbers.update(re.findall(r"\d+", entitlement["label"]))

        prose = _prose(page)
        for number in _quota_numbers(prose):
            assert number in known_numbers, (
                f"the page's prose quotes {number} next to a quota word, but "
                "the catalog grants that quota to no tier"
            )

    def test_the_page_uses_unlimited_only_where_the_catalog_does(
        self, page, catalog
    ):
        """'Unlimited' is an entitlement, not an adjective."""
        unlimited = {
            entitlement["label"]
            for plan in catalog["plans"]
            for entitlement in plan["entitlements"]
            if entitlement["label"].lower().startswith("unlimited")
        }
        for match in re.finditer(r"Unlimited\s+[A-Za-z][A-Za-z\- ]*", page):
            phrase = match.group(0).strip().rstrip(" .,;:")
            assert any(label.startswith(phrase) for label in unlimited), (
                f"the page says {phrase!r}; the catalog grants no such "
                f"unlimited entitlement (it grants: {sorted(unlimited)})"
            )

    def test_the_indexed_feature_list_prices_nothing_and_quotas_nothing(
        self, page, catalog
    ):
        """The JSON-LD featureList is indexed too, so it is checked too."""
        features = _software_application(page).get("featureList", [])
        assert features, "the JSON-LD has no featureList"

        known_numbers = set()
        for plan in catalog["plans"]:
            for entitlement in plan["entitlements"]:
                known_numbers.update(re.findall(r"\d+", entitlement["label"]))

        for feature in features:
            assert "$" not in feature, (
                f"the indexed feature list prices a feature ({feature!r}); "
                "prices belong to the offers"
            )
            for number in _quota_numbers(feature):
                assert number in known_numbers, (
                    f"the indexed feature list quotes {number} in {feature!r}, "
                    "which the catalog grants to no tier"
                )


# --------------------------------------------------------------------------
# The service count is a measurable claim too
# --------------------------------------------------------------------------
class TestTheDockerServiceCount:
    def test_the_compose_file_really_has_thirteen_services(self):
        assert _compose_service_count() == 13, (
            "the measured service count changed; update the page and this "
            "test together"
        )

    def test_the_page_states_the_real_service_count(self, page):
        count = _compose_service_count()
        assert f"<b>{count}</b>Docker services" in page
        assert f"{count} services." in page
        assert f"{count} Docker services" in page
        assert f"{count}-service distributed system" in page

    def test_the_page_does_not_still_claim_fourteen_services(self, page):
        assert "14 Docker services" not in page
        assert "14 services" not in page
        assert "14-service" not in page
