# tests/test_public_published_results.py
"""The public screener must expose only PUBLISHED data -- never user state.
#
# The public screener page (frontend/src/pages/PublicScreener.tsx) is a
# no-login page. It reads three public endpoints:
#
#   * GET /api/v1/strategy-templates        -- the curated template catalogue
#   * GET /api/v1/stats/platform            -- platform counters
#   * GET /api/v1/public/published-results  -- backtest results a user
#                                               explicitly published for
#                                               public comparison
#
# The third endpoint is the only new public surface, so its boundary is pinned
# here. It may list a PUBLISHED BACKTEST result. It may never expose anything
# that belongs to an ACCOUNT: no owner identity, no balance, no equity, no XP,
# no positions, no API keys. And it may never expose a backtest that its owner
# did not publish, or whose public link the owner has since switched off.
"""

import uuid
from datetime import datetime, timezone

import pytest

from api import models
from api.depthsight_api import app

ENDPOINT = "/api/v1/public/published-results"

DEFAULT_KPIS = {
    "total_pnl": 1200.0,
    "win_rate": 55.0,
    "max_drawdown": 8.0,
    "trades": 40,
    "sharpe_ratio": 1.5,
}


def _make_user(db, username, email, plan="free", admin_notes=None):
    user = models.User(
        username=username,
        email=email,
        hashed_password="x",
        plan=plan,
        admin_notes=admin_notes,
    )
    db.add(user)
    return user


async def _make_published_result(
    db,
    user,
    *,
    strategy_name="RSI Breakout",
    symbol="BTCUSDT",
    kpis=None,
    is_strategy_name_public=True,
    is_active=True,
    category="sharpe_ratio",
    period=models.LeaderboardPeriod.ALL_TIME,
    score=1.5,
):
    """Publish one backtest for public comparison, the way the app does it."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    db.add(
        models.Task(
            user_id=user.id,
            task_id=task_id,
            task_type="backtest",
            status="COMPLETED",
        )
    )
    run = models.BacktestRun(
        id=str(uuid.uuid4()),
        user_id=user.id,
        task_id=task_id,
        strategy_name=strategy_name,
        symbol=symbol,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_balance=10000.0,
        parameters_json={"name": strategy_name},
        status="COMPLETED",
        kpi_results_json=DEFAULT_KPIS if kpis is None else kpis,
    )
    db.add(run)
    await db.flush()

    slug = f"slug{uuid.uuid4().hex[:10]}"
    db.add(
        models.SharedBacktest(
            backtest_run_id=run.id,
            user_id=user.id,
            public_slug=slug,
            is_strategy_name_public=is_strategy_name_public,
            are_parameters_public=False,
            is_active=is_active,
        )
    )
    await db.flush()

    db.add(
        models.LeaderboardEntry(
            user_id=user.id,
            backtest_run_id=run.id,
            shared_backtest_slug=slug,
            period=period,
            category=category,
            score=score,
            rank=1,
            meta_data={"symbol": symbol},
        )
    )
    await db.flush()
    return run


def _walk(node, key=None):
    """Yield every (key, value) pair in a JSON payload."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from _walk(v, k)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, key)


class TestPublishedResultsArePublic:
    async def test_the_endpoint_needs_no_authentication(self, db_session, test_client):
        user = _make_user(db_session, "screener-pub", "screener-pub@realcustomer.com")
        await db_session.flush()
        await _make_published_result(db_session, user)
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]["results"]) == 1

    async def test_a_result_that_was_never_published_is_not_listed(
        self, db_session, test_client
    ):
        """A backtest with no public share must not appear just because it ran."""
        user = _make_user(db_session, "screener-private", "sp@realcustomer.com")
        await db_session.flush()

        task_id = f"task-{uuid.uuid4().hex[:8]}"
        db_session.add(
            models.Task(
                user_id=user.id,
                task_id=task_id,
                task_type="backtest",
                status="COMPLETED",
            )
        )
        db_session.add(
            models.BacktestRun(
                id=str(uuid.uuid4()),
                user_id=user.id,
                task_id=task_id,
                strategy_name="NeverPublishedStrategy",
                symbol="ETHUSDT",
                start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
                initial_balance=10000.0,
                parameters_json={},
                status="COMPLETED",
                kpi_results_json=DEFAULT_KPIS,
            )
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["results"] == []
        assert "NeverPublishedStrategy" not in response.text

    async def test_a_revoked_public_link_is_not_listed(self, db_session, test_client):
        """Switching the public link off must remove the result from the feed."""
        user = _make_user(db_session, "screener-revoked", "sr@realcustomer.com")
        await db_session.flush()
        await _make_published_result(
            db_session, user, strategy_name="RevokedStrategy", is_active=False
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["results"] == []
        assert "RevokedStrategy" not in response.text

    async def test_a_demo_account_result_is_not_listed(self, db_session, test_client):
        """Seeded demo accounts are not customers; their results must not be
        advertised to a prospect (same rule the public counters already use)."""
        demo = _make_user(db_session, "alex_trader", "alex@example.com")
        await db_session.flush()
        await _make_published_result(
            db_session, demo, strategy_name="FabricatedDemoStrategy"
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["results"] == []
        assert "FabricatedDemoStrategy" not in response.text


class TestPublishedResultsLeakNoUserState:
    async def test_the_owner_identity_is_never_exposed(
        self, db_session, test_client
    ):
        user = _make_user(db_session, "screener-owner", "owner@realcustomer.com")
        await db_session.flush()
        await _make_published_result(db_session, user)
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        body = response.text

        assert "screener-owner" not in body
        assert "owner@realcustomer.com" not in body

        result = response.json()["data"]["results"][0]
        for forbidden in ("user", "userId", "user_id", "username", "email", "plan"):
            assert forbidden not in result, f"{forbidden} leaked into the public result"

        # No field may carry the owner's account id. `rank` is skipped: it is a
        # published position number, not an identity.
        for key, value in _walk(response.json()):
            if key == "rank":
                continue
            assert value != user.id, "the owner's account id leaked into the payload"

    async def test_account_state_is_never_exposed(self, db_session, test_client):
        """Balance, equity, XP, positions and API keys are ACCOUNT state. None of
        them may appear in the public feed."""
        user = _make_user(db_session, "screener-state", "state@realcustomer.com")
        await db_session.flush()
        db_session.add(
            models.PaperWallet(
                user_id=user.id, asset="USDT", balance=987654.32
            )
        )
        db_session.add(
            models.ApiKey(
                user_id=user.id,
                name="Default Test Key",
                exchange="binance",
                encrypted_api_key="enc-key",
                encrypted_api_secret="enc-secret",
                key_prefix="test...9999",
                status="valid",
                is_active=True,
            )
        )
        await _make_published_result(db_session, user)
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        body = response.text

        assert "987654.32" not in body
        assert "987654" not in body
        assert "test...9999" not in body
        assert "enc-key" not in body
        assert "enc-secret" not in body
        for forbidden in ("balance", "equity", "xp", "position", "apiKey", "api_key"):
            assert forbidden.lower() not in body.lower(), (
                f"{forbidden} leaked into the public feed"
            )

    async def test_a_private_strategy_name_is_not_revealed(
        self, db_session, test_client
    ):
        """The owner may publish the RESULT while keeping the strategy name
        private. The feed must then say so instead of naming the strategy."""
        user = _make_user(db_session, "screener-anon", "anon@realcustomer.com")
        await db_session.flush()
        await _make_published_result(
            db_session,
            user,
            strategy_name="SecretAlphaStrategy",
            is_strategy_name_public=False,
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        assert "SecretAlphaStrategy" not in response.text

        result = response.json()["data"]["results"][0]
        assert result["strategyName"] == "Private Strategy"
        # The report is still reachable -- only the NAME is hidden.
        assert result["slug"]
        assert result["hasPublishedBacktest"] is True


class TestPublishedResultsAreHonest:
    async def test_the_metrics_are_the_published_backtest_numbers(
        self, db_session, test_client
    ):
        user = _make_user(db_session, "screener-metrics", "m@realcustomer.com")
        await db_session.flush()
        await _make_published_result(db_session, user)
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        result = response.json()["data"]["results"][0]

        assert result["symbol"] == "BTCUSDT"
        assert result["metrics"]["sharpeRatio"] == 1.5
        assert result["metrics"]["netPnl"] == 1200.0
        assert result["metrics"]["netPnlPercent"] == pytest.approx(12.0)
        assert result["metrics"]["winRate"] == 55.0
        assert result["metrics"]["maxDrawdown"] == 8.0
        assert result["metrics"]["trades"] == 40
        # The period of the backtest itself is published with it.
        assert result["period"]["start"].startswith("2026-01-01")
        assert result["period"]["end"].startswith("2026-03-31")

    async def test_a_metric_the_backtest_never_produced_stays_null(
        self, db_session, test_client
    ):
        """A missing metric must be null, never 0.0 -- otherwise the page would
        print a number the backtest never produced."""
        user = _make_user(db_session, "screener-partial", "p@realcustomer.com")
        await db_session.flush()
        await _make_published_result(
            db_session,
            user,
            kpis={"total_pnl": 250.0, "trades": 7},
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        result = response.json()["data"]["results"][0]

        assert result["metrics"]["sharpeRatio"] is None
        assert result["metrics"]["winRate"] is None
        assert result["metrics"]["maxDrawdown"] is None
        assert result["metrics"]["trades"] == 7
        assert result["metrics"]["netPnlPercent"] == pytest.approx(2.5)

    async def test_the_feed_states_what_the_numbers_are(self, db_session, test_client):
        """The payload says which metric it is ranked by, over which period, and
        that the numbers are backtest results."""
        user = _make_user(db_session, "screener-basis", "b@realcustomer.com")
        await db_session.flush()
        await _make_published_result(db_session, user)
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        data = response.json()["data"]

        assert data["category"] == "sharpe_ratio"
        assert data["period"] == "all_time"
        assert data["basis"] == "backtest"

    async def test_an_unknown_category_is_refused(self, db_session, test_client):
        response = await test_client.get(f"{ENDPOINT}?category=not_a_metric")
        assert response.status_code == 400, response.text


class TestThePublishedSchemaAdvertisesTheAllowedValues:
    """FastAPI 0.156 removed `enum` from `Query()`, so a constrained parameter
    has to spell the same OpenAPI metadata out itself. A client that reads the
    published schema needs to know which values it may send."""

    def _query_param_schema(self, name: str) -> dict:
        published = app.openapi()
        assert ENDPOINT in published["paths"], (
            f"{ENDPOINT} is not in the published OpenAPI schema"
        )
        operation = published["paths"][ENDPOINT]["get"]
        for param in operation.get("parameters", []):
            if param.get("name") == name and param.get("in") == "query":
                return param.get("schema") or {}
        raise AssertionError(f"{ENDPOINT} publishes no query parameter {name!r}")

    def test_the_category_parameter_publishes_its_enum(self):
        schema = self._query_param_schema("category")
        assert schema.get("enum") == ["sharpe_ratio", "net_pnl_percent"]

    def test_the_period_parameter_publishes_its_enum(self):
        schema = self._query_param_schema("period")
        assert schema.get("enum") == ["weekly", "monthly", "all_time"]


class TestTheOtherPublicEndpointsTheScreenerUses:
    async def test_the_template_catalogue_needs_no_authentication(
        self, db_session, test_client
    ):
        response = await test_client.get("/api/v1/strategy-templates")
        assert response.status_code == 200, response.text
        assert "data" in response.json()

    async def test_the_template_catalogue_exposes_no_user_data(
        self, db_session, test_client
    ):
        user = _make_user(db_session, "screener-tpl-user", "tpl@realcustomer.com")
        await db_session.flush()
        await db_session.commit()

        response = await test_client.get("/api/v1/strategy-templates")
        assert response.status_code == 200, response.text
        body = response.text
        assert "screener-tpl-user" not in body
        assert "tpl@realcustomer.com" not in body
        for forbidden in ("userId", "user_id", "username", "email", "balance"):
            assert forbidden not in body

    async def test_the_platform_counters_need_no_authentication(
        self, db_session, test_client
    ):
        response = await test_client.get("/api/v1/stats/platform")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert "totalUsers" in data
        assert "strategyTemplates" in data
        # The counters are aggregates, never a row about one person.
        for forbidden in ("username", "email", "balance", "equity"):
            assert forbidden not in data
