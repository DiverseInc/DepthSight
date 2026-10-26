# tests/test_platform_stats_excludes_demo_accounts.py
"""FIX 3 -- the public counters must not count seeded demo accounts.

`scripts/seed_demo_data.py` creates fake trader accounts with fabricated
backtests. The public endpoint `GET /api/v1/stats/platform` counted every
row in the `users` and `backtest_runs` tables, so in production 7 of the 12
"traders" a prospect could count were fake.

The exclusion is applied at READ time (api/demo_seed.py) so no production row
is deleted or modified.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from api import models
from api.demo_seed import (
    DEMO_SEED_ADMIN_NOTES,
    is_demo_account,
    not_demo_accounts,
)

REAL_USERS = [
    ("diverseinc", "admin@depthsight.pro"),
    ("diverseindustriesinc1", "ops@depthsight.pro"),
    ("yusef115", "yusef@realcustomer.com"),
    ("incenterprisescom", "hello@incenterprises.com"),
    ("dakotajacksonnn", "dakota@realcustomer.com"),
]

DEMO_USERS = [
    ("alex_trader", "alex@example.com", "pro"),
    ("sarah_quant", "sarah@example.com", "standard"),
    ("mike_scalper", "mike@example.com", "pro"),
    ("lina_hodler", "lina@example.com", "free"),
    ("raj_algo", "raj@example.com", "standard"),
    ("emma_retail", "emma@example.com", "pro"),
    ("demo_inactive", "old@example.com", "free"),
]


def _make_user(username, email, plan="free", admin_notes=None):
    return models.User(
        username=username,
        email=email,
        hashed_password="x",
        plan=plan,
        admin_notes=admin_notes,
    )


async def _make_backtest(db, user):
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
        strategy_name="Fabricated Strategy",
        symbol="BTCUSDT",
        start_date=datetime.now(timezone.utc),
        end_date=datetime.now(timezone.utc),
        initial_balance=10000.0,
        parameters_json={},
        status="COMPLETED",
    )
    db.add(run)
    await db.flush()
    return run


class TestDemoAccountIdentification:
    def test_the_reserved_example_domain_is_demo(self):
        assert is_demo_account(email="alex@example.com") is True

    def test_the_seed_marker_is_demo(self):
        assert (
            is_demo_account(
                username="someone", email="a@real.com", admin_notes=DEMO_SEED_ADMIN_NOTES
            )
            is True
        )

    def test_a_real_customer_is_not_demo(self):
        assert is_demo_account(username="diverseinc", email="admin@depthsight.pro") is False
        assert is_demo_account(username="yusef115", email="yusef@realcustomer.com") is False

    def test_a_real_customer_with_no_marker_is_not_demo(self):
        """A NULL admin_notes must not make the user disappear."""
        assert is_demo_account(username="someone", email="a@real.com", admin_notes=None) is False

    @pytest.mark.parametrize(
        "username,email,expected",
        [
            ("alex_trader", "alex@example.com", True),
            # The domain must be at the END: "…@example.com.evil.com" is a
            # real domain and must not be caught by the reserved-domain rule.
            ("someone", "user@example.com.evil.com", False),
            # No "@" at all is not a match either.
            ("someone", "example.com", False),
            ("demo_inactive", "old@realcustomer.com", True),
            ("realuser", "real@realcustomer.com", False),
        ],
    )
    def test_the_python_rule_matches_the_sql_rule(self, username, email, expected):
        """The Python predicate and the SQL expression must agree.

        The SQL expression is the one that runs in production; the Python one
        is the documented rule. If they drift, the documented rule is a lie.
        """
        assert is_demo_account(username=username, email=email) is expected


class TestPublicStatsExcludeDemoAccounts:
    async def test_demo_users_are_not_counted_as_traders(self, db_session, test_client):
        for username, email in REAL_USERS:
            db_session.add(_make_user(username, email))
        for username, email, plan in DEMO_USERS:
            db_session.add(_make_user(username, email, plan=plan))
        await db_session.commit()

        expected_real = (
            await db_session.execute(
                select(func.count(models.User.id)).where(not_demo_accounts(models.User))
            )
        ).scalar() or 0

        response = await test_client.get("/api/v1/stats/platform")
        assert response.status_code == 200, response.text
        data = response.json()["data"]

        # The 5 real customers are counted; the 7 seeded accounts are not.
        assert data["totalUsers"] == expected_real
        assert data["totalUsers"] < expected_real + len(DEMO_USERS)

    async def test_demo_users_backtests_are_not_counted(self, db_session, test_client):
        real = _make_user("stats-real-user", "stats-real@realcustomer.com")
        demo = _make_user("stats-demo-user", "stats-demo@example.com")
        db_session.add_all([real, demo])
        await db_session.flush()

        await _make_backtest(db_session, real)
        await _make_backtest(db_session, demo)
        await _make_backtest(db_session, demo)
        await db_session.commit()

        expected_backtests = (
            await db_session.execute(
                select(func.count(models.BacktestRun.id))
                .join(models.User, models.BacktestRun.user_id == models.User.id)
                .where(not_demo_accounts(models.User))
            )
        ).scalar() or 0

        response = await test_client.get("/api/v1/stats/platform")
        data = response.json()["data"]

        assert data["totalBacktests"] == expected_backtests

    async def test_a_demo_account_with_the_marker_is_excluded_too(
        self, db_session, test_client
    ):
        """Catches a demo account that was created by hand, without the
        reserved e-mail domain."""
        db_session.add(
            _make_user(
                "handmade-demo",
                "handmade@not-reserved.invalid",
                admin_notes=DEMO_SEED_ADMIN_NOTES,
            )
        )
        await db_session.commit()

        expected_real = (
            await db_session.execute(
                select(func.count(models.User.id)).where(not_demo_accounts(models.User))
            )
        ).scalar() or 0

        response = await test_client.get("/api/v1/stats/platform")
        data = response.json()["data"]

        assert data["totalUsers"] == expected_real

    async def test_a_real_user_with_no_marker_is_still_counted(
        self, db_session, test_client
    ):
        """`admin_notes` is NULL for almost every real user.

        SQL three-valued logic makes this a classic way to LOSE real rows:
        `NULL = 'x'` is NULL, `FALSE OR NULL` is NULL, and `~NULL` is NULL --
        so a filter written without care drops exactly the users it should
        keep. This asserts the counter goes UP by one when a normal user is
        added, independently of the filter expression.
        """
        before = (
            await test_client.get("/api/v1/stats/platform")
        ).json()["data"]["totalUsers"]

        db_session.add(_make_user("null-marker-user", "null-marker@realcustomer.com"))
        await db_session.commit()

        after = (
            await test_client.get("/api/v1/stats/platform")
        ).json()["data"]["totalUsers"]

        assert after == before + 1, (
            "A real user with no admin marker was not counted. NULL handling "
            "in the demo filter is dropping genuine customers."
        )

    async def test_the_sql_rule_only_catches_the_reserved_domain(
        self, db_session, test_client
    ):
        """The SQL expression is what runs in production, so it is pinned
        directly: the reserved domain is excluded, a look-alike domain is
        NOT."""
        before = (
            await test_client.get("/api/v1/stats/platform")
        ).json()["data"]["totalUsers"]

        db_session.add(_make_user("lookalike-user", "user@example.com.evil.com"))
        await db_session.commit()

        after = (
            await test_client.get("/api/v1/stats/platform")
        ).json()["data"]["totalUsers"]

        assert after == before + 1, (
            "A user on a look-alike domain (example.com.evil.com) was "
            "wrongly excluded from the public counters."
        )
