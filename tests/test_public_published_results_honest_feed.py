# tests/test_public_published_results_honest_feed.py
"""The public feed must list a published result even when it LOST money.
#
# WHY THIS FILE EXISTS
# --------------------
# The public screener page (frontend/src/pages/PublicScreener.tsx) is a
# no-login page. It has been live and EMPTY since it shipped: the endpoint
# answered 200 with "total": 0.
#
# The reason was measured, not guessed. The feed's query joined
# LeaderboardEntry -> BacktestRun -> SharedBacktest, so a result could only
# appear if a LeaderboardEntry row existed. A LeaderboardEntry row is
# created only for a strategy that passed the leaderboard's own entry
# rules -- at least 30 trades, AND a Sharpe ratio strictly above buy and
# hold over the same symbol and window. The validation raises BEFORE the
# share is committed, so a strategy that lost money never even got a
# public link.
#
# The net effect was a feed that could only ever show winners. That is not
# a rule anybody decided on for the FEED; it was a side effect of where
# the query started. A feed that hides every losing result is worth
# nothing to a prospect deciding whether to trust the platform.
#
# THE DECISION THESE TESTS PIN
# ----------------------------
# The leaderboard keeps its anti-buy-and-hold rule -- it is a competition,
# and that rule is deliberate. The PUBLIC FEED is separated from it: it
# lists any completed, publicly-shared, non-demo run, including one that
# lost money, ranked by the run's OWN KPI.
#
# What these tests hold in place:
#   * a losing, completed, shared, non-demo run IS listed;
#   * the filters that exist today still hold (not finished / link off /
#     demo account / never published);
#   * the ranking is by the run's own KPI, not by a stored leaderboard
#     score;
#   * the payload carries no identifying fields -- for a losing result too.
#
# The leaderboard's own anti-buy-and-hold validator is NOT touched here.
# It is proved untouched by a byte-level diff against origin/main, not by
# a test: the validator needs live market data, so a unit test around it
# would only ever prove that the mock was right.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api import models
from api.depthsight_api import app

ENDPOINT = "/api/v1/public/published-results"

# The numbers of the account's own pre-declared test strategy, which is
# measurably bad: net -5,761.30 over 105 trades at a 13.5% win rate.
# Its Sharpe is negative, so it can NEVER clear the leaderboard's
# buy-and-hold bar -- and it must still be publishable and listable.
LOSING_KPIS = {
    "total_pnl": -5761.30,
    "win_rate": 13.5,
    "max_drawdown": 22.4,
    "trades": 105,
    "sharpe_ratio": -0.87,
}


def _make_user(db, username, email, plan="free"):
    user = models.User(
        username=username,
        email=email,
        hashed_password="x",
        plan=plan,
    )
    db.add(user)
    return user


async def _make_shared_run(
    db,
    user,
    *,
    strategy_name="LosingStrategy",
    symbol="BTCUSDT",
    kpis=None,
    status="COMPLETED",
    is_active=True,
    is_strategy_name_public=True,
    published_at=None,
    leaderboard_entry=False,
    leaderboard_score=None,
):
    """Publish one backtest the way the app does.

    A finished run, a public link, and -- only when the owner asked for it
    AND passed the leaderboard's own validation -- a leaderboard entry.
    `leaderboard_entry` is False by default because a strategy that lost
    money never gets one: the validation raises first.
    """
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
        status=status,
        kpi_results_json=LOSING_KPIS if kpis is None else kpis,
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
            created_at=published_at,
        )
    )
    await db.flush()

    if leaderboard_entry:
        db.add(
            models.LeaderboardEntry(
                user_id=user.id,
                backtest_run_id=run.id,
                shared_backtest_slug=slug,
                period=models.LeaderboardPeriod.ALL_TIME,
                category="sharpe_ratio",
                score=(
                    leaderboard_score
                    if leaderboard_score is not None
                    else LOSING_KPIS["sharpe_ratio"]
                ),
                rank=1,
                meta_data={"symbol": symbol},
            )
        )
        await db.flush()

    return run


class TestTheFeedListsAResultThatLostMoney:
    """The whole point: a losing result is published, so it is listed."""

    async def test_a_losing_shared_run_is_listed(self, db_session, test_client):
        user = _make_user(db_session, "loser-owner", "loser@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(db_session, user, strategy_name="MeasurablyBadStrategy")
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text

        results = response.json()["data"]["results"]
        assert len(results) == 1, response.text
        assert results[0]["strategyName"] == "MeasurablyBadStrategy"

    async def test_the_losing_numbers_are_published_unchanged(
        self, db_session, test_client
    ):
        """A negative number has to be published as the negative number it
        is. Rounding it to zero, or dropping the sign, would be a lie."""
        user = _make_user(db_session, "loser-numbers", "numbers@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(db_session, user)
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        result = response.json()["data"]["results"][0]

        assert result["metrics"]["sharpeRatio"] == pytest.approx(-0.87)
        assert result["metrics"]["netPnl"] == pytest.approx(-5761.30)
        assert result["metrics"]["netPnlPercent"] == pytest.approx(-57.613)
        assert result["metrics"]["winRate"] == pytest.approx(13.5)
        assert result["metrics"]["trades"] == 105
        assert result["score"] == pytest.approx(-0.87)

    async def test_a_losing_run_needs_no_leaderboard_entry(
        self, db_session, test_client
    ):
        """The losing run has NO LeaderboardEntry row at all -- the
        leaderboard's validation raised before one could be written -- and
        it is listed anyway. That is the separation, proved end to end."""
        user = _make_user(db_session, "loser-noentry", "noentry@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(db_session, user)
        await db_session.commit()

        entries = await db_session.execute(models.LeaderboardEntry.__table__.select())
        assert (
            entries.first() is None
        ), "the fixture must not have created a leaderboard entry"

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]["results"]) == 1

    async def test_a_losing_run_is_ranked_by_its_own_negative_return(
        self, db_session, test_client
    ):
        """Ranking by net return puts the losing run BELOW the winning one --
        it is last because it lost, not because it is missing."""
        user = _make_user(db_session, "loser-ranked", "ranked@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session,
            user,
            strategy_name="Winner",
            kpis={
                "total_pnl": 1200.0,
                "win_rate": 55.0,
                "max_drawdown": 8.0,
                "trades": 40,
                "sharpe_ratio": 1.5,
            },
        )
        await _make_shared_run(
            db_session, user, strategy_name="Loser", kpis=LOSING_KPIS
        )
        await db_session.commit()

        response = await test_client.get(f"{ENDPOINT}?category=net_pnl_percent")
        results = response.json()["data"]["results"]

        assert [r["strategyName"] for r in results] == ["Winner", "Loser"]
        assert results[1]["score"] == pytest.approx(-57.613)
        assert [r["rank"] for r in results] == [1, 2]


class TestTheFiltersThatMustStillHold:
    """The feed is more open than it was, but it is not open to everything."""

    async def test_a_run_that_did_not_finish_is_not_listed(
        self, db_session, test_client
    ):
        user = _make_user(db_session, "unfinished", "unfinished@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session,
            user,
            strategy_name="StillRunningStrategy",
            status="RUNNING",
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["results"] == []
        assert "StillRunningStrategy" not in response.text

    async def test_a_failed_run_is_not_listed(self, db_session, test_client):
        user = _make_user(db_session, "failed", "failed@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session, user, strategy_name="FailedStrategy", status="FAILED"
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.json()["data"]["results"] == []
        assert "FailedStrategy" not in response.text

    async def test_a_revoked_public_link_is_not_listed(self, db_session, test_client):
        user = _make_user(db_session, "revoked", "revoked@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session, user, strategy_name="RevokedStrategy", is_active=False
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.json()["data"]["results"] == []
        assert "RevokedStrategy" not in response.text

    async def test_a_demo_account_result_is_not_listed(self, db_session, test_client):
        """A seeded demo account is not a customer. Its fabricated results
        must never be advertised -- losing ones included."""
        demo = _make_user(db_session, "alex_trader", "alex@example.com")
        await db_session.flush()
        await _make_shared_run(db_session, demo, strategy_name="FabricatedDemoStrategy")
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.json()["data"]["results"] == []
        assert "FabricatedDemoStrategy" not in response.text

    async def test_a_backtest_that_was_never_published_is_not_listed(
        self, db_session, test_client
    ):
        """Only a run whose owner made it public may be listed."""
        user = _make_user(db_session, "unpublished", "unpublished@realcustomer.com")
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
                kpi_results_json=LOSING_KPIS,
            )
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.json()["data"]["results"] == []
        assert "NeverPublishedStrategy" not in response.text


class TestTheRankingIsByTheRunsOwnKpi:
    async def test_the_score_is_the_runs_own_number_not_the_stored_leaderboard_score(
        self, db_session, test_client
    ):
        """A leaderboard entry carries the score the leaderboard ranked the
        entry by. The feed ranks by the RUN's own number, so the two can
        never be confused for one another."""
        user = _make_user(db_session, "own-kpi", "ownkpi@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session,
            user,
            strategy_name="OwnKpiStrategy",
            kpis={
                "total_pnl": 400.0,
                "win_rate": 51.0,
                "max_drawdown": 9.0,
                "trades": 33,
                "sharpe_ratio": 0.4,
            },
            leaderboard_entry=True,
            leaderboard_score=99.0,
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        result = response.json()["data"]["results"][0]

        assert result["score"] == pytest.approx(0.4)
        assert result["metrics"]["sharpeRatio"] == pytest.approx(0.4)

    async def test_the_order_is_by_the_runs_own_metric(self, db_session, test_client):
        user = _make_user(db_session, "own-order", "ownorder@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session,
            user,
            strategy_name="Middle",
            kpis={"total_pnl": 0.0, "trades": 10, "sharpe_ratio": 0.0},
        )
        await _make_shared_run(
            db_session,
            user,
            strategy_name="Best",
            kpis={"total_pnl": 900.0, "trades": 12, "sharpe_ratio": 1.9},
        )
        await _make_shared_run(
            db_session,
            user,
            strategy_name="Worst",
            kpis={"total_pnl": -900.0, "trades": 12, "sharpe_ratio": -1.9},
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        results = response.json()["data"]["results"]

        assert [r["strategyName"] for r in results] == ["Best", "Middle", "Worst"]
        assert [r["rank"] for r in results] == [1, 2, 3]

    async def test_a_result_that_never_measured_the_metric_is_still_listed(
        self, db_session, test_client
    ):
        """A run that never produced a Sharpe ratio is still published and
        still listed -- with the metric left unset, ranked after every
        result that did measure it. It is not a 0.0."""
        user = _make_user(db_session, "no-sharpe", "nosharpe@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session,
            user,
            strategy_name="Measured",
            kpis={"total_pnl": 100.0, "trades": 8, "sharpe_ratio": 0.6},
        )
        await _make_shared_run(
            db_session,
            user,
            strategy_name="NeverMeasured",
            kpis={"total_pnl": 250.0, "trades": 7},
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        results = response.json()["data"]["results"]

        assert [r["strategyName"] for r in results] == ["Measured", "NeverMeasured"]
        assert results[1]["score"] is None
        assert results[1]["metrics"]["sharpeRatio"] is None
        assert results[1]["metrics"]["netPnlPercent"] == pytest.approx(2.5)


class TestThePublishedWindow:
    async def test_the_weekly_window_only_lists_recently_published_results(
        self, db_session, test_client
    ):
        """'weekly' means published in the last 7 days. A result published
        long ago is not part of that ranking, so it is neither listed nor
        counted."""
        user = _make_user(db_session, "window", "window@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session,
            user,
            strategy_name="Recent",
            published_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        await _make_shared_run(
            db_session,
            user,
            strategy_name="Old",
            published_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        await db_session.commit()

        weekly = await test_client.get(f"{ENDPOINT}?period=weekly")
        names = [r["strategyName"] for r in weekly.json()["data"]["results"]]
        assert names == ["Recent"]
        assert weekly.json()["data"]["total"] == 1

        all_time = await test_client.get(f"{ENDPOINT}?period=all_time")
        assert all_time.json()["data"]["total"] == 2


class TestALosingResultCarriesNoIdentifyingFields:
    """The payload of a LOSING result must be as anonymous as the payload of
    a winning one. A negative number is not a licence to publish the
    account behind it."""

    async def test_the_owner_identity_is_never_exposed(self, db_session, test_client):
        user = _make_user(db_session, "loser-owner-2", "owner2@realcustomer.com")
        await db_session.flush()
        db_session.add(
            models.PaperWallet(user_id=user.id, asset="USDT", balance=987654.32)
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
        await _make_shared_run(db_session, user)
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert response.status_code == 200, response.text
        body = response.text

        assert "loser-owner-2" not in body
        assert "owner2@realcustomer.com" not in body
        assert "987654.32" not in body
        assert "987654" not in body
        assert "test...9999" not in body
        assert "enc-key" not in body
        assert "enc-secret" not in body

        result = response.json()["data"]["results"][0]
        for forbidden in (
            "user",
            "userId",
            "user_id",
            "username",
            "email",
            "plan",
            "balance",
            "equity",
            "xp",
            "position",
            "apiKey",
            "api_key",
        ):
            assert forbidden not in result, f"{forbidden} leaked into the public result"

    async def test_a_private_strategy_name_is_still_hidden_on_a_losing_result(
        self, db_session, test_client
    ):
        user = _make_user(db_session, "loser-anon", "anon2@realcustomer.com")
        await db_session.flush()
        await _make_shared_run(
            db_session,
            user,
            strategy_name="SecretLosingStrategy",
            is_strategy_name_public=False,
        )
        await db_session.commit()

        response = await test_client.get(ENDPOINT)
        assert "SecretLosingStrategy" not in response.text

        result = response.json()["data"]["results"][0]
        assert result["strategyName"] == "Private Strategy"
        # The report is still reachable -- only the NAME is hidden.
        assert result["slug"]
        assert result["hasPublishedBacktest"] is True


class TestThePublishedSchemaStillDescribesTheFeed:
    def _query_param_schema(self, name: str) -> dict:
        published = app.openapi()
        assert (
            ENDPOINT in published["paths"]
        ), f"{ENDPOINT} is not in the published OpenAPI schema"
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
