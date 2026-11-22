# tests/test_public_pricing_live_trading_labels.py
"""The PUBLIC catalog must not deny a tier the live trading it really has.

THE DEFECT THIS PINS
--------------------
The Free tier DOES trade live -- on Bybit only:

  * api/plans_config.yml:80-88 gives it `allow_real_trading: false`,
    `max_live_strategies: 0` AND `allow_free_bybit_trading: true`,
    `max_free_bybit_live_strategies: 5`;
  * api/routes/strategies.py:541-549 lets a tier without the
    `allow_real_trading` permission trade live when the API key is a Bybit
    key -- any other venue is answered with a 403; and
  * api/depthsight_api.py:638-641 caps that tier at
    `max_free_bybit_live_strategies` live strategies.

But the catalog derived its live-trading entitlements from
`allow_real_trading` and `max_live_strategies` ONLY, so the public catalog
published Free as "Live trading: not included" and "Live trading strategies:
not included".

That is not cosmetic. The React pricing page renders every not-included
entitlement under the literal heading "Not included"
(frontend/src/components/marketing/Pricing.tsx:104-119) and it is fed the
catalog's own labels (frontend/src/lib/pricing.ts:121-126), so the public page
told every visitor that the Free tier cannot trade live -- while the backend
lets it trade live on Bybit.

The static page already told the truth (frontend/welcome.html:411 -- "Live
trading on Bybit (up to 5 live trading strategies)"), so the two public pages
contradicted each other and the React one was the false one.

WHAT THIS TEST DOES
-------------------
It builds the catalog with `build_plan_catalog()` -- the very function the
public endpoint `GET /api/v1/public/plans` returns
(api/routes/public.py:25-36) -- and asserts:

  * Free is published as HAVING live trading, with a label that names Bybit;
  * Free's live-strategy label carries the cap the plan really has, read
    from plans_config.yml rather than written into this test, and the label
    follows the config when that cap changes;
  * Free is NOT published as excluding live trading;
  * Standard and Professional keep their own labels and their own caps;
  * a tier with no live trading at all is still published as excluding it.

The last case is produced by removing the Bybit exception from a COPY of the
real plans_config.yml and running the same builder, so it is the product's
own output rather than a hand-made fixture.
"""

import copy
from pathlib import Path

import pytest
import yaml

from api.plans import build_plan_catalog, plans_config

REPO_ROOT = Path(__file__).resolve().parents[1]
PLANS_CONFIG = REPO_ROOT / "api" / "plans_config.yml"

# The tiers the catalog sells, by plan key.
FREE_KEY = "free"
PAID_KEYS = ("standard", "pro", "managed")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def catalog() -> dict:
    """The catalog the PUBLIC endpoint serves.

    `GET /api/v1/public/plans` (api/routes/public.py:25-36) returns exactly
    this object, so this fixture is the public catalog -- not a copy of it.
    """
    return build_plan_catalog()


@pytest.fixture(scope="module")
def raw_plans() -> dict:
    """The real plans_config.yml, keyed by plan key.

    The caps are read from here so that the expectations follow the config
    instead of repeating it: a change to the configured cap must change the
    expectation, and the label under test must change with it.
    """
    config = yaml.safe_load(PLANS_CONFIG.read_text(encoding="utf-8"))
    return config["plans"]


def _plan(catalog: dict, key: str) -> dict:
    for plan in catalog["plans"]:
        if plan["key"] == key:
            return plan
    raise AssertionError(f"the catalog does not sell a {key!r} tier")


def _entitlement(plan: dict, code: str) -> dict:
    """The one entitlement a plan carries with this code."""
    matches = [entry for entry in plan["entitlements"] if entry["code"] == code]
    assert len(matches) == 1, (
        f"the {plan['key']!r} tier must carry exactly one {code!r} "
        f"entitlement, found {len(matches)}: {matches!r}"
    )
    return matches[0]


def _plans_without_bybit_exception() -> dict:
    """A copy of the real plans, with the Free tier's Bybit exception gone.

    This is how a tier with NO live trading at all is produced: the real plan
    definitions, minus the one limit that grants live trading, run through the
    product's own builder.
    """
    config = yaml.safe_load(PLANS_CONFIG.read_text(encoding="utf-8"))
    plans = copy.deepcopy(config["plans"])
    limits = plans[FREE_KEY].get("limits", {}) or {}
    limits.pop("allow_free_bybit_trading", None)
    limits.pop("max_free_bybit_live_strategies", None)
    return plans


# --------------------------------------------------------------------------
# The Free tier really does trade live -- on Bybit
# --------------------------------------------------------------------------
class TestTheFreeTierIsPublishedAsHavingLiveTrading:
    def test_the_free_tier_is_published_as_having_live_trading(self, catalog):
        free = _plan(catalog, FREE_KEY)
        live_trading = _entitlement(free, "live_trading")

        assert live_trading["included"] is True, (
            "the Free tier trades live on Bybit "
            "(api/routes/strategies.py:541-549), so the catalog must not "
            f"publish it as excluded: {live_trading!r}"
        )

    def test_the_free_tiers_live_trading_label_names_bybit(self, catalog):
        free = _plan(catalog, FREE_KEY)
        live_trading = _entitlement(free, "live_trading")

        assert "bybit" in live_trading["label"].lower(), (
            "the Free tier may trade live on Bybit ONLY -- every other venue "
            f"is refused with a 403 -- so the label must say so: "
            f"{live_trading['label']!r}"
        )

    def test_the_free_tier_is_not_published_as_excluding_live_trading(self, catalog):
        free = _plan(catalog, FREE_KEY)
        excluded = [
            entry for entry in free["entitlements"] if not entry["included"]
        ]
        excluded_labels = {entry["label"].lower() for entry in excluded}

        assert "live trading" not in excluded_labels, (
            "the public pricing page renders these under the heading "
            f'"Not included", so a Free reader would be told the opposite: '
            f"{sorted(excluded_labels)!r}"
        )
        assert "live trading strategies" not in excluded_labels, (
            "the public pricing page renders these under the heading "
            f'"Not included", so a Free reader would be told the opposite: '
            f"{sorted(excluded_labels)!r}"
        )
        assert not [
            entry
            for entry in excluded
            if entry["code"] in ("live_trading", "live_strategies")
        ], "the Free tier must not carry an excluded live-trading entitlement"

    def test_the_free_tiers_live_strategy_cap_is_the_configured_one(
        self, catalog, raw_plans
    ):
        cap = int(
            (raw_plans[FREE_KEY].get("limits", {}) or {}).get(
                "max_free_bybit_live_strategies", 0
            )
        )
        assert cap > 0, "the Free tier must have a live-strategy cap to publish"

        free = _plan(catalog, FREE_KEY)
        live_strategies = _entitlement(free, "live_strategies")

        assert live_strategies["included"] is True, (
            f"the Free tier may run {cap} live strategies, so the catalog "
            f"must not publish it as excluded: {live_strategies!r}"
        )
        assert str(cap) in live_strategies["label"], (
            f"the label must carry the tier's real cap ({cap}): "
            f"{live_strategies['label']!r}"
        )
        assert "bybit" in live_strategies["label"].lower(), (
            f"the cap only applies on Bybit: {live_strategies['label']!r}"
        )

    def test_the_cap_label_follows_the_config(self, monkeypatch):
        """The number is DERIVED, not written into the product.

        The config is the source of truth: change the cap and the published
        label must change with it.
        """
        plans = _plans_without_bybit_exception()
        plans[FREE_KEY]["limits"]["allow_free_bybit_trading"] = True
        plans[FREE_KEY]["limits"]["max_free_bybit_live_strategies"] = 7
        monkeypatch.setattr(plans_config, "get_all_plans", lambda: plans)

        free = _plan(build_plan_catalog(), FREE_KEY)
        live_strategies = _entitlement(free, "live_strategies")

        assert "7" in live_strategies["label"], (
            "the label must carry the cap the config gives this tier: "
            f"{live_strategies['label']!r}"
        )
        assert "5" not in live_strategies["label"], (
            "the cap must come from the config, not from a hard-coded number: "
            f"{live_strategies['label']!r}"
        )


# --------------------------------------------------------------------------
# The paid tiers are untouched
# --------------------------------------------------------------------------
class TestThePaidTiersAreUnaffected:
    @pytest.mark.parametrize("key", PAID_KEYS)
    def test_a_paid_tier_keeps_its_own_live_trading_label(self, catalog, key):
        plan = _plan(catalog, key)
        live_trading = _entitlement(plan, "live_trading")

        assert live_trading["included"] is True
        assert live_trading["label"] == "Live trading on OKX, Binance, Bybit"

    @pytest.mark.parametrize("key", PAID_KEYS)
    def test_a_paid_tier_keeps_its_own_live_strategy_cap(self, catalog, raw_plans, key):
        cap = int(
            (raw_plans[key].get("limits", {}) or {}).get("max_live_strategies", 0)
        )
        assert cap > 0

        plan = _plan(catalog, key)
        live_strategies = _entitlement(plan, "live_strategies")

        assert live_strategies["included"] is True
        assert live_strategies["label"] == f"{cap} live trading strategies"


# --------------------------------------------------------------------------
# A tier with no live trading at all still says so
# --------------------------------------------------------------------------
class TestATierWithNoLiveTradingSaysSo:
    def test_a_tier_without_any_live_trading_is_published_as_excluded(
        self, monkeypatch
    ):
        plans = _plans_without_bybit_exception()
        monkeypatch.setattr(plans_config, "get_all_plans", lambda: plans)

        free = _plan(build_plan_catalog(), FREE_KEY)

        live_trading = _entitlement(free, "live_trading")
        assert live_trading["included"] is False, (
            "a tier with no live trading must be published as excluded: "
            f"{live_trading!r}"
        )
        assert live_trading["label"] == "Live trading"

        live_strategies = _entitlement(free, "live_strategies")
        assert live_strategies["included"] is False, (
            "a tier with no live trading must be published as excluded: "
            f"{live_strategies!r}"
        )
        assert live_strategies["label"] == "Live trading strategies"
