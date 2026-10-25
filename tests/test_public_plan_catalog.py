# tests/test_public_plan_catalog.py
"""FIX 1 -- the public pricing page must not advertise anything the backend
does not actually sell.

The marketing pricing page used to carry a hand-written list of plans and
features. It advertised a "$30 Pro" tier with unlimited backtests, 30 live
strategies, PRO blocks and genetic strategy search -- none of which the $30
tier grants -- and advertised a 14-day trial the backend never gives.

The pricing page now renders the catalog built by `build_plan_catalog()`
straight from api/plans_config.yml. These tests pin that catalog against
the real config values.
"""

import pytest

from api.plans import build_plan_catalog, plans_config


def _plan(catalog, key):
    return next(p for p in catalog["plans"] if p["key"] == key)


def _included(plan):
    return [e["label"] for e in plan["entitlements"] if e["included"]]


def _excluded(plan):
    return [e["label"] for e in plan["entitlements"] if not e["included"]]


class TestCatalogMatchesConfig:
    """Every number the page shows must equal the number in the config."""

    @pytest.fixture(scope="class")
    def catalog(self):
        return build_plan_catalog()

    def test_all_three_tiers_are_published(self, catalog):
        assert {p["key"] for p in catalog["plans"]} == {"free", "standard", "pro"}

    @pytest.mark.parametrize(
        "plan_key,quota_key,subject,period",
        [
            ("free", "run_vector_backtest_per_day", "backtest", "day"),
            ("free", "use_ai_assistant_per_day", "AI Assistant query", "day"),
            ("standard", "run_vector_backtest_per_day", "backtest", "day"),
            ("standard", "use_ai_assistant_per_day", "AI Assistant query", "day"),
            ("pro", "use_ai_assistant_per_day", "AI Assistant query", "day"),
            ("pro", "run_genetic_search_per_month", "genetic search", "month"),
        ],
    )
    def test_quota_labels_match_the_config_value(
        self, catalog, plan_key, quota_key, subject, period
    ):
        quotas = plans_config.get_plan(plan_key)["quotas"]
        value = quotas[quota_key]
        plan = _plan(catalog, plan_key)
        included = _included(plan)

        # Matched on the NUMBER (the thing that must never drift) plus the
        # subject stem, so the test does not re-derive the exact wording
        # ("query" -> "queries" changes the letter, so startswith fails).
        stem = subject[:-1] if subject.endswith("y") else subject
        if value == -1:
            matches = [
                label
                for label in included
                if label.startswith("Unlimited ") and stem in label
            ]
        else:
            matches = [
                label
                for label in included
                if label.startswith(f"{value} ") and stem in label
            ]

        assert matches, (
            f"{plan_key}.{quota_key} is {value} in plans_config.yml but no "
            f"entitlement mentions {value} {subject}"
        )

        if value != -1:
            # The period must be stated too, so "per day" is never confused
            # with "per month".
            assert any(f"per {period}" in label for label in matches), (
                f"{plan_key}.{quota_key} does not state 'per {period}'"
            )

    def test_the_30_dollar_tier_does_not_claim_unlimited_backtests(self, catalog):
        standard = _plan(catalog, "standard")
        quotas = plans_config.get_plan("standard")["quotas"]
        assert quotas["run_vector_backtest_per_day"] == 50
        assert "Unlimited backtests" not in _included(standard)
        assert "50 backtests per day" in _included(standard)

    def test_the_30_dollar_tier_does_not_claim_30_live_strategies(self, catalog):
        standard = _plan(catalog, "standard")
        limits = plans_config.get_plan("standard")["limits"]
        assert limits["max_live_strategies"] == 10
        assert "30 live trading strategies" not in _included(standard)
        assert "10 live trading strategies" in _included(standard)

    def test_the_30_dollar_tier_does_not_claim_genetic_search(self, catalog):
        standard = _plan(catalog, "standard")
        config = plans_config.get_plan("standard")
        assert config["quotas"]["run_genetic_search_per_month"] == 0
        assert "run_genetic_search" not in config["permissions"]
        assert "Genetic strategy search" not in _included(standard)
        # It says so rather than pretending the tier has it.
        assert "Genetic strategy search" in _excluded(standard)

    def test_the_30_dollar_tier_does_not_claim_pro_blocks(self, catalog):
        standard = _plan(catalog, "standard")
        assert "PRO blocks (Tape, Book, Open Interest)" not in _included(standard)
        assert "PRO blocks (Tape, Book, Open Interest)" in _excluded(standard)

    def test_the_100_dollar_tier_is_published_with_its_real_entitlements(
        self, catalog
    ):
        pro = _plan(catalog, "pro")
        assert pro["price_usd"] == 100
        assert "Unlimited backtests" in _included(pro)
        assert "PRO blocks (Tape, Book, Open Interest)" in _included(pro)
        assert "Genetic strategy search" in _included(pro)
        assert "30 live trading strategies" in _included(pro)

    def test_the_free_tier_stays_accurate(self, catalog):
        free = _plan(catalog, "free")
        assert free["price_usd"] == 0
        assert "20 backtests per day" in _included(free)
        assert "10 AI Assistant queries per day" in _included(free)
        assert "90-day backtest history" in _included(free)

    def test_lifetime_prices_are_published(self, catalog):
        assert _plan(catalog, "standard")["billing"]["lifetime"]["price_usd"] == 99
        assert _plan(catalog, "pro")["billing"]["lifetime"]["price_usd"] == 299

    def test_no_tier_promises_a_trial_the_backend_does_not_grant(self, catalog):
        """The config grants a trial of ZERO days, so there is no trial.

        The page must therefore not be able to advertise one. `trial.active`
        is what the frontend reads to decide whether to show a trial.
        """
        trial_config = plans_config.get_registration_trial_config()
        assert trial_config["days"] == 0
        assert catalog["trial"]["active"] is False
        assert catalog["trial"]["days"] == 0


class TestTrialFailsClosed:
    """A malformed commercial value must not silently grant free product."""

    def test_a_malformed_trial_duration_grants_no_trial(self, monkeypatch):
        from api import plans as plans_module

        monkeypatch.setitem(
            plans_module.plans_config._registration_trial, "days", "not-a-number"
        )
        config = plans_module.plans_config.get_registration_trial_config()
        assert config["days"] == 0, (
            "A malformed registration_trial.days must fail closed (0 days), "
            "not default to a paid trial."
        )


class TestPublicPlansEndpoint:
    """The marketing page reads this endpoint, unauthenticated."""

    async def test_public_plans_needs_no_authentication(self, test_client):
        response = await test_client.get("/api/v1/public/plans")
        assert response.status_code == 200, response.text

        data = response.json()
        assert {p["key"] for p in data["plans"]} == {"free", "standard", "pro"}

    async def test_public_plans_matches_the_catalog(self, test_client):
        response = await test_client.get("/api/v1/public/plans")
        data = response.json()
        catalog = build_plan_catalog()
        assert data["trial"] == catalog["trial"]
        assert [p["entitlements"] for p in data["plans"]] == [
            p["entitlements"] for p in catalog["plans"]
        ]

    async def test_public_plans_never_claims_a_trial(self, test_client):
        response = await test_client.get("/api/v1/public/plans")
        data = response.json()
        assert data["trial"]["active"] is False

        standard = next(p for p in data["plans"] if p["key"] == "standard")
        included = [e["label"] for e in standard["entitlements"] if e["included"]]
        assert "Unlimited backtests" not in included
        assert "50 backtests per day" in included
