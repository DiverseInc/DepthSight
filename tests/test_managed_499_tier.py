"""The $499 "Managed" tier: the catalog entry, the `premium` alias, and the
removal of the false "Portfolio backtesting" advertisement.

Owner decision 2026-11-04 ("go" = A/A/A):

  A1  a $499/month "Managed" tier exists in the catalog and on every surface
      that names a plan (the admin dropdown, the tier ladder, the marketing
      page);
  A2  the dead `premium` tier keeps WORKING: it resolves to Managed. The two
      live user rows on `premium` (sarah_quant id 11, raj_algo id 14) are NOT
      migrated -- an alias, not a data migration;
  A3  the false "Portfolio backtesting" advertisement is removed from
      Standard and Professional -- WITHOUT touching the entitlement
      (`tests/test_plans.py` pins standard -> 202 and free -> 403).

Everything is derived from api/plans_config.yml. The number of tiers is
written down once, in `test_the_catalog_sells_exactly_the_active_plans`.
"""

import json
import pathlib
import re

import pytest
import yaml

import api.plans as plans_module
from api.plans import (
    _PRO_BLOCK_PLANS,
    PlansConfig,
    build_plan_catalog,
    plans_config,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLANS_CONFIG = REPO_ROOT / "api" / "plans_config.yml"
WELCOME_HTML = REPO_ROOT / "frontend" / "welcome.html"
COMMUNITY_HUB = REPO_ROOT / "frontend" / "src" / "pages" / "CommunityHub.tsx"
ADMIN_DETAIL_PAGE = (
    REPO_ROOT / "frontend" / "src" / "pages" / "admin" / "AdminUserDetailPage.tsx"
)
FRONTEND_PLANS = REPO_ROOT / "frontend" / "src" / "lib" / "plans.ts"
LOCALES = REPO_ROOT / "frontend" / "src" / "locales"

# A limit of -1 means "no limit" everywhere in the config.
UNLIMITED = -1

# Celery's task priority is a number where 0 runs BEFORE 9, so for this one
# limit a LOWER value on Managed is a BETTER value. Every other limit is
# "higher is better".
LOWER_IS_BETTER_LIMITS = {"celery_task_priority"}


def _quota_value(value) -> float:
    """A quota value, with -1 (unlimited) as the biggest possible number."""
    value = float(value)
    return float("inf") if value == UNLIMITED else value


def _limit_value(key, value) -> float:
    """A limit value, with -1 (unlimited) as the biggest possible number.

    `celery_task_priority` is inverted: a lower number is a better number.
    """
    value = float(value)
    if key in LOWER_IS_BETTER_LIMITS:
        assert value != UNLIMITED, f"{key} must not use the unlimited sentinel"
        return -value
    return float("inf") if value == UNLIMITED else value


def _lifetime_price_or_none(plan: dict):
    """The rule of `lifetimePriceFor` in frontend/src/lib/pricing.ts:88-100.

    A one-off price is advertised only when the deployment is selling one
    (`billing_mode == "lifetime"`), the tier's lifetime block is enabled, and
    it carries a price.
    """
    lifetime = plan.get("billing", {}).get("lifetime", {}) or {}
    if plan.get("billing_mode") != "lifetime":
        return None
    if not lifetime.get("enabled"):
        return None
    price = lifetime.get("price_usd")
    return price if isinstance(price, (int, float)) else None


@pytest.fixture(scope="module")
def raw_config() -> dict:
    return yaml.safe_load(PLANS_CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw_plans(raw_config) -> dict:
    return raw_config["plans"]


@pytest.fixture(scope="module")
def catalog() -> dict:
    return build_plan_catalog()


@pytest.fixture(scope="module")
def plans_by_key(catalog) -> dict:
    return {plan["key"]: plan for plan in catalog["plans"]}


# ---------------------------------------------------------------------------
# 1. The catalog: four plans, Managed at $499, billed monthly.
# ---------------------------------------------------------------------------
class TestTheManagedTierIsInTheCatalog:
    def test_the_catalog_sells_exactly_the_active_plans(self, catalog, raw_plans):
        """The expected set is DERIVED from the source config.

        This is the only place in the suite where the number of tiers is
        written down.
        """
        expected = {
            key for key, plan in raw_plans.items() if plan.get("active", False)
        }
        assert {plan["key"] for plan in catalog["plans"]} == expected
        assert len(expected) == 4
        assert "managed" in expected

    def test_managed_is_priced_at_499(self, plans_by_key, raw_plans):
        managed = plans_by_key["managed"]
        assert managed["name"] == "Managed"
        assert managed["price_usd"] == 499
        assert managed["billing"]["monthly"]["price_usd"] == 499
        assert managed["billing"]["monthly"]["period_days"] == 30
        assert raw_plans["managed"]["price_usd"] == 499

    def test_managed_is_billed_monthly(self, plans_by_key, raw_config):
        assert raw_config["billing"]["mode"] == "monthly"
        assert plans_by_key["managed"]["billing_mode"] == "monthly"

    def test_managed_is_active(self, plans_by_key):
        assert plans_by_key["managed"]["active"] is True

    def test_managed_has_no_lifetime_price_in_either_billing_mode(
        self, tmp_path, monkeypatch
    ):
        """The Managed tier is a monthly service: it has no one-off price.

        The catalog is rebuilt with `billing.mode` set to each mode in turn,
        so BOTH branches of `lifetimePriceFor` are exercised for real.
        """
        source = yaml.safe_load(PLANS_CONFIG.read_text(encoding="utf-8"))
        for mode in ("monthly", "lifetime"):
            source["billing"]["mode"] = mode
            path = tmp_path / f"plans_config_{mode}.yml"
            path.write_text(
                yaml.safe_dump(source, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            monkeypatch.setattr(plans_module, "plans_config", PlansConfig(path))
            managed = next(
                plan
                for plan in build_plan_catalog()["plans"]
                if plan["key"] == "managed"
            )
            assert managed["billing_mode"] == mode
            assert _lifetime_price_or_none(managed) is None, (
                f"the Managed tier advertises a one-off price in {mode} mode"
            )
            # And it is still the monthly price that is charged.
            assert managed["price_usd"] == 499


# ---------------------------------------------------------------------------
# 2. Managed is never worse than Professional.
# ---------------------------------------------------------------------------
class TestManagedIsNeverWorseThanProfessional:
    def test_managed_holds_every_permission_pro_holds(self, plans_by_key):
        pro = plans_by_key["pro"]
        managed = plans_by_key["managed"]
        assert set(pro["permissions"]) <= set(managed["permissions"])

    def test_no_pro_quota_is_lower_on_managed(self, plans_by_key):
        pro = plans_by_key["pro"]
        managed = plans_by_key["managed"]
        for key, value in pro["quotas"].items():
            assert key in managed["quotas"], f"managed is missing the quota {key}"
            assert _quota_value(managed["quotas"][key]) >= _quota_value(value), (
                f"managed's {key} ({managed['quotas'][key]}) is lower than "
                f"pro's ({value})"
            )

    def test_no_pro_limit_is_lower_on_managed(self, plans_by_key):
        pro = plans_by_key["pro"]
        managed = plans_by_key["managed"]
        for key, value in pro["limits"].items():
            assert key in managed["limits"], f"managed is missing the limit {key}"
            assert _limit_value(key, managed["limits"][key]) >= _limit_value(
                key, value
            ), (
                f"managed's {key} ({managed['limits'][key]}) is worse than "
                f"pro's ({value})"
            )

    def test_managed_is_strictly_better_on_the_things_it_is_bought_for(
        self, plans_by_key
    ):
        """The tier must be genuinely worth more than Professional."""
        pro = plans_by_key["pro"]
        managed = plans_by_key["managed"]
        assert managed["limits"]["max_live_strategies"] > pro["limits"][
            "max_live_strategies"
        ]
        assert managed["limits"]["max_concurrent_tasks"] > pro["limits"][
            "max_concurrent_tasks"
        ]
        assert managed["limits"]["celery_task_priority"] < pro["limits"][
            "celery_task_priority"
        ]
        assert managed["quotas"]["run_optimization_per_month"] == UNLIMITED
        assert managed["quotas"]["run_genetic_search_per_month"] == UNLIMITED


# ---------------------------------------------------------------------------
# 3. Managed gets the PRO blocks.
# ---------------------------------------------------------------------------
class TestManagedGetsTheProBlocks:
    def test_the_derived_entitlement_row_says_managed_has_them(self, plans_by_key):
        row = next(
            entry
            for entry in plans_by_key["managed"]["entitlements"]
            if entry["code"] == "pro_blocks"
        )
        assert row["included"] is True

    def test_managed_is_in_the_pro_block_plans(self):
        assert "managed" in _PRO_BLOCK_PLANS

    def test_the_pro_block_gate_answers_for_managed_and_for_premium(self):
        # Imported here, not at module scope: on the UNMODIFIED code this
        # symbol does not exist, and the failure has to be this test's
        # failure rather than a collection error for the whole file.
        from api.plans import plan_has_pro_block_access

        assert plan_has_pro_block_access("managed")
        assert plan_has_pro_block_access("premium")
        assert plan_has_pro_block_access("pro")
        assert not plan_has_pro_block_access("free")
        assert not plan_has_pro_block_access("standard")

    def test_the_ai_assistant_tier_context_uses_the_shared_gate(self):
        """The AI assistant must not tell a Managed user they lack PRO blocks.

        api/ai_assistant.py decides this with a plan-key list of its own; it
        now calls the shared gate, so there is ONE place that answers it.
        """
        source = (
            REPO_ROOT / "api" / "ai_assistant.py"
        ).read_text(encoding="utf-8")
        assert "plan_has_pro_block_access(user.plan)" in source
        assert 'user.plan not in ["pro", "institutional"]' not in source


# ---------------------------------------------------------------------------
# 4. The `premium` alias (A2).
# ---------------------------------------------------------------------------
class TestThePremiumAlias:
    def test_premium_resolves_to_the_managed_config(self, plans_by_key):
        managed = plans_by_key["managed"]
        resolved = plans_config.get_plan("premium")
        assert resolved["permissions"] == managed["permissions"]
        assert resolved["quotas"] == managed["quotas"]
        assert resolved["limits"] == managed["limits"]
        assert resolved["name"] == managed["name"]

    def test_premium_resolves_for_billing_and_price(self):
        assert plans_config.get_plan_billing("premium") == (
            plans_config.get_plan_billing("managed")
        )
        assert plans_config.get_effective_plan_price("premium") == (
            plans_config.get_effective_plan_price("managed")
        )
        assert plans_config.get_effective_plan_price("premium") == 499

    def test_premium_is_not_a_second_sellable_tier(self, plans_by_key):
        """`managed` is the canonical key; `premium` is only an alias."""
        assert "premium" not in plans_by_key
        assert "premium" not in plans_config.get_all_plans()
        assert "premium" not in plans_config.get_plan_aliases().values()

    def test_an_unknown_plan_key_still_fails_closed(self):
        assert plans_config.get_plan("no-such-plan") == {
            "permissions": [],
            "quotas": {},
        }

    def test_the_alias_is_declared_in_the_config(self, raw_config):
        assert raw_config["plan_aliases"]["premium"] == "managed"

    def test_the_frontend_resolves_the_same_alias(self):
        source = FRONTEND_PLANS.read_text(encoding="utf-8")
        match = re.search(
            r"export const PLAN_ALIASES[^=]*=\s*\{(.*?)\};", source, re.S
        )
        assert match, (
            "frontend/src/lib/plans.ts no longer declares PLAN_ALIASES"
        )
        aliases = dict(re.findall(r"(\w+)\s*:\s*\"([^\"]*)\"", match.group(1)))
        assert aliases["premium"] == "managed"


# ---------------------------------------------------------------------------
# 5. A3: the advertisement is gone, the entitlement is intact.
# ---------------------------------------------------------------------------
class TestPortfolioBacktestingIsNotAdvertised:
    @pytest.mark.parametrize("plan_key", ["free", "standard", "pro", "managed"])
    def test_no_tier_advertises_the_capability(self, plans_by_key, plan_key):
        labels = [
            entry["label"] for entry in plans_by_key[plan_key]["entitlements"]
        ]
        assert "Portfolio backtesting" not in labels

    def test_the_suppression_is_config_driven(self, raw_config):
        """It must be a switch in the config, not a hand edit of copy."""
        assert "run_portfolio_backtest" in raw_config["deferred_permissions"]

    @pytest.mark.parametrize("plan_key", ["standard", "pro", "managed"])
    def test_the_entitlement_is_intact(self, raw_plans, plan_key):
        assert "run_portfolio_backtest" in raw_plans[plan_key]["permissions"]

    def test_free_still_has_no_portfolio_backtest(self, raw_plans):
        assert "run_portfolio_backtest" not in raw_plans["free"]["permissions"]
        assert raw_plans["free"]["quotas"]["run_portfolio_backtest_per_day"] == 0

    def test_portfolio_simulation_is_not_touched(self, raw_plans):
        """`run_simulation` is a DIFFERENT, working permission.

        It guards the simulation router (api/simulation_router.py), which has
        real endpoints with real response models. It is NOT the portfolio
        backtest scaffold, so it stays advertised.
        """
        for tier in ("pro", "managed"):
            assert "run_simulation" in raw_plans[tier]["permissions"]
        assert "run_simulation" not in raw_plans["standard"]["permissions"]


# ---------------------------------------------------------------------------
# 6. The marketing page.
# ---------------------------------------------------------------------------
class TestTheMarketingPageShowsTheManagedTier:
    @pytest.fixture(scope="class")
    def page(self) -> str:
        return WELCOME_HTML.read_text(encoding="utf-8")

    def test_the_page_has_a_managed_card(self, page):
        assert 'data-plan-key="managed"' in page
        assert "<h3>Managed</h3>" in page

    def test_the_page_prices_it_at_499_per_month(self, page):
        # The EXACT card markup, not a loose "$499 somewhere on the page": the
        # fineprint and the JSON-LD also carry the price, so only the card's
        # own price text proves what the card shows.
        assert "$499<small> /month</small>" in page
        assert "/register?plan=managed" in page

    def test_the_page_no_longer_claims_portfolio_backtesting(self, page):
        assert "Portfolio backtesting" not in page

    def test_the_json_ld_offers_the_managed_tier(self, page):
        assert '"name": "Managed"' in page
        assert '"price": "499"' in page
        assert '"priceCurrency": "USD"' in page

    def test_the_json_ld_feature_list_names_every_tier_that_grants_it(self, page):
        assert "Genetic strategy search (Professional and Managed plans)" in page
        assert "ML model laboratory (Professional and Managed plans)" in page


# ---------------------------------------------------------------------------
# 7. The tier ladder and the display-name map.
# ---------------------------------------------------------------------------
class TestTheTierLadderAndTheDisplayName:
    @pytest.fixture(scope="class")
    def community_hub(self) -> str:
        return COMMUNITY_HUB.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def ladder(self) -> dict:
        source = COMMUNITY_HUB.read_text(encoding="utf-8")
        match = re.search(
            r"const TIER_ORDINAL[^=]*=\s*\{(.*?)\};", source, re.S
        )
        assert match, "CommunityHub.tsx no longer declares TIER_ORDINAL"
        return {
            key: int(value)
            for key, value in re.findall(r"(\w+)\s*:\s*(\d+)", match.group(1))
        }

    def test_managed_and_premium_are_on_the_same_rung(self, ladder):
        assert "managed" in ladder
        assert "premium" in ladder
        assert ladder["managed"] == ladder["premium"]

    def test_managed_is_above_pro(self, ladder):
        assert ladder["managed"] > ladder["pro"]
        assert ladder["pro"] > ladder["standard"] > ladder["free"]

    def test_the_upgrade_button_uses_the_display_name_not_the_raw_key(
        self, community_hub
    ):
        assert "tierDisplayName(strategy.tier_required)" in community_hub
        assert '`Upgrade to ${strategy.tier_required ?? "Pro"}`' not in community_hub

    def test_the_display_name_map_names_premium_as_managed(self):
        source = FRONTEND_PLANS.read_text(encoding="utf-8")
        match = re.search(
            r"export const PLAN_DISPLAY_NAMES[^=]*=\s*\{(.*?)\};", source, re.S
        )
        assert match, (
            "frontend/src/lib/plans.ts no longer declares PLAN_DISPLAY_NAMES"
        )
        names = dict(re.findall(r"(\w+)\s*:\s*\"([^\"]*)\"", match.group(1)))
        assert names["premium"] == names["managed"] == "Managed"

    @pytest.mark.parametrize("locale", ["en", "ru"])
    def test_both_locales_name_the_managed_tier(self, locale):
        account = json.loads(
            (LOCALES / locale / "account.json").read_text(encoding="utf-8")
        )
        assert account["plans"]["managed"]
        assert account["plans"]["premium"] == account["plans"]["managed"]

    @pytest.mark.parametrize("locale", ["en", "ru"])
    def test_both_locales_translate_the_upgrade_button(self, locale):
        community = json.loads(
            (LOCALES / locale / "community.json").read_text(encoding="utf-8")
        )
        assert "{{tier}}" in community["verified"]["upgradeRequired"]

    def test_the_admin_dropdown_offers_managed_and_normalises_premium(self):
        source = ADMIN_DETAIL_PAGE.read_text(encoding="utf-8")
        assert '<SelectItem value="managed">Managed</SelectItem>' in source
        assert "canonicalPlanKey(editData.plan)" in source
