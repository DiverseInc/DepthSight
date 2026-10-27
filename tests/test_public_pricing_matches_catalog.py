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
        self, cards, plans_by_name
    ):
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            granted = {e["label"] for e in plan["entitlements"] if e["included"]}

            assert set(card["included"]) == granted, (
                f"the {card['name']} card does not list exactly the entitlements "
                f"the catalog grants. missing={sorted(granted - set(card['included']))} "
                f"extra={sorted(set(card['included']) - granted)}"
            )

    def test_no_card_claims_a_quota_from_another_tier(self, cards, plans_by_name):
        """The old page gave the $29 tier 'Unlimited backtest runs'."""
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            granted = {e["label"] for e in plan["entitlements"] if e["included"]}

            for other_name, other in plans_by_name.items():
                if other_name == card["name"]:
                    continue
                for entitlement in other["entitlements"]:
                    if not entitlement["included"]:
                        continue
                    if entitlement["label"] in granted:
                        # Shared across tiers (e.g. "Paper trading"): not a claim.
                        continue
                    assert entitlement["label"] not in card["included"], (
                        f"the {card['name']} card claims "
                        f"{entitlement['label']!r}, which belongs to the "
                        f"{other_name} tier"
                    )

    def test_every_not_included_label_is_genuinely_excluded(
        self, cards, plans_by_name
    ):
        for card in cards:
            plan = plans_by_name.get(card["name"])
            assert plan is not None
            excluded = {e["label"] for e in plan["entitlements"] if not e["included"]}

            for label in card["not_included"]:
                assert label in excluded, (
                    f"the {card['name']} card lists {label!r} as not included, "
                    "but the catalog does not exclude it"
                )


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

    def test_the_indexed_offers_point_at_the_registration_path(self, page):
        for offer in _software_application(page)["offers"]:
            assert offer["url"].startswith("https://depthsight.diverseinc.net/register")

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
