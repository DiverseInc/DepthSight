#!/usr/bin/env python3
"""
Seed script: populate the platform with realistic-looking demo data so the
admin panel doesn't look empty to your first investor.

Marks every created row with admin_notes='demo_seed_v1' (on users) and
similar markers on related tables, so you can delete all of it later with:

    docker compose exec postgres env PGPASSWORD=$POSTGRES_PASSWORD psql \
        -h 127.0.0.1 -U $POSTGRES_USER -d $POSTGRES_DB \
        -c "DELETE FROM users WHERE admin_notes = 'demo_seed_v1';"

Run inside the api container so SQLAlchemy + DB are reachable:

    docker compose cp scripts/seed_demo_data.py api:/tmp/seed_demo_data.py
    docker compose exec api python /tmp/seed_demo_data.py
"""
import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone

# Add the app to the path so we can import the models
sys.path.insert(0, "/app")

from sqlalchemy import select  # noqa: E402

from api.database import AsyncSessionLocal, engine  # noqa: E402
from api.models import (  # noqa: E402
    Base,
    User,
    StrategyConfig,
    BacktestRun,
    AffiliatePayout,
)

DEMO_MARKER = "demo_seed_v1"


# Realistic-looking investor personas
DEMO_USERS = [
    # username, email, plan, days_since_join, strategy_archetype
    ("alex_trader",   "alex@example.com",     "pro",     45, "rsi_breakout"),
    ("sarah_quant",   "sarah@example.com",   "premium", 90, "ma_crossover"),
    ("mike_scalper",  "mike@example.com",    "pro",     30, "scalping"),
    ("lina_hodler",   "lina@example.com",    "free",    14, None),  # new user, no strategy yet
    ("raj_algo",      "raj@example.com",     "premium", 120, "grid_dca"),
    ("emma_retail",   "emma@example.com",    "pro",     60, "mean_reversion"),
    ("demo_inactive", "old@example.com",     "free",    200, None),  # churned
]

STRATEGY_NAMES = {
    "rsi_breakout":     "RSI Breakout v2",
    "ma_crossover":     "EMA 50/200 Golden Cross",
    "scalping":         "Order Book Imbalance Scalper",
    "grid_dca":         "Grid DCA — BTC range",
    "mean_reversion":   "Bollinger Mean Reversion",
}

EXCHANGE_CONFIGS = [
    {"exchange": "binance",  "testnet": True},
    {"exchange": "binance",  "testnet": False},
    {"exchange": "bybit",    "testnet": True},
    {"exchange": "bybit",    "testnet": False},
]


def jitter_dt(base: datetime, hours: int = 48) -> datetime:
    """Return a datetime randomly offset up to `hours` before base."""
    return base - timedelta(hours=random.randint(0, hours))


async def seed():
    # Schema is created by alembic migrations at container start — no need to create_all here.
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        # Idempotency: if we already ran, wipe the old demo data first
        existing = (await db.execute(
            select(User).where(User.admin_notes == DEMO_MARKER)
        )).scalars().all()
        if existing:
            print(f"[seed] Found {len(existing)} existing demo users — wiping first")
            for u in existing:
                await db.delete(u)
            await db.commit()

        # 1. Users
        users_created = []
        for username, email, plan, days_ago, archetype in DEMO_USERS:
            is_active = (username != "demo_inactive")  # one churned demo
            joined_at = now - timedelta(days=days_ago, hours=random.randint(0, 23))
            u = User(
                username=username,
                email=email,
                # Real bcrypt hash of "DemoPassword123!" — generated locally, verified.
                # The Login page has a "Use demo credentials" button that auto-fills
                # these creds; without a real hash, login fails for seeded users.
                hashed_password="$2b$12$2OhJWOEl5n58YAd5y05XC.TH6Lqyq0V4kpwNz47NhT9nemijpagW2",
                is_active=is_active,
                plan=plan,
                plan_expires_at=(now + timedelta(days=365)) if plan != "free" else None,
                role="user",
                # Note: User model no longer has is_admin — admin status is role="admin" only.
                xp=random.randint(0, 5000),
                level=random.randint(1, 15),
                admin_notes=DEMO_MARKER,
                referral_code=f"DEMO-{username[:6].upper()}-{random.randint(100,999)}",
                created_at=joined_at,
            )
            db.add(u)
            users_created.append((u, archetype))
        await db.commit()
        print(f"[seed] Created {len(users_created)} demo users")

        # 2. Strategies + backtests for each active user with an archetype
        # NOTE: StrategyConfig / BacktestRun / AffiliatePayout models have diverged
        # from what this script was originally written for. The user creation above
        # is the high-value part for the demo — strategies/backtests are best-effort
        # and failures here are non-fatal (the script completes successfully).
        strategies_created = 0
        backtests_created = 0
        try:
            for u, archetype in users_created:
                if not u.is_active or not archetype:
                    continue

                # 1-2 strategies per user
                for s_idx in range(random.randint(1, 2)):
                    cfg = StrategyConfig(
                        user_id=u.id,
                        name=STRATEGY_NAMES[archetype] + (f" #{s_idx+1}" if s_idx else ""),
                        description=f"Auto-generated demo strategy: {archetype} variant {s_idx+1}",
                        config_data={  # was config_json in old model
                            "timeframe": random.choice(["5m", "15m", "1h", "4h"]),
                            "symbol": random.choice(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]),
                            "archetype": archetype,
                            "params": {
                                "lookback": random.randint(14, 50),
                                "threshold": round(random.uniform(0.5, 0.95), 2),
                                "stop_loss_pct": round(random.uniform(0.5, 3.0), 2),
                                "take_profit_pct": round(random.uniform(1.0, 5.0), 2),
                            },
                        },
                        created_at=jitter_dt(now, hours=24 * 30),
                    )
                    db.add(cfg)
                    strategies_created += 1
                    await db.flush()  # get cfg.id

                    # 1-3 backtests per strategy
                    for b_idx in range(random.randint(1, 3)):
                        bt = BacktestRun(
                            user_id=u.id,
                            strategy_name=cfg.name,  # BacktestRun uses string name, not FK
                            symbol=random.choice(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]),
                            market_type="futures_usdtm",
                            start_date=now - timedelta(days=random.randint(60, 180)),
                            end_date=now - timedelta(days=random.randint(1, 30)),
                            initial_balance=10000.0,
                            parameters_json={"timeframe": "1h"},  # required field
                            status="completed",
                            created_at=jitter_dt(now, hours=24 * 14),
                            completed_at=jitter_dt(now, hours=24 * 14),
                            kpi_results_json={
                                "total_return_pct": round(random.uniform(-15, 45), 2),
                                "sharpe_ratio": round(random.uniform(0.5, 2.8), 2),
                                "max_drawdown_pct": round(random.uniform(-25, -3), 2),
                                "win_rate": round(random.uniform(0.4, 0.7), 2),
                                "total_trades": random.randint(20, 250),
                                "profitable_trades": random.randint(10, 150),
                            },
                        )
                        db.add(bt)
                        backtests_created += 1

            await db.commit()
            print(f"[seed] Created {strategies_created} strategies + {backtests_created} backtests")
        except Exception as e:
            print(f"[seed] WARNING: strategy/backtest seed skipped due to model drift: {e}")
            await db.rollback()
            strategies_created = 0
            backtests_created = 0

        # 3. Affiliate relationships (one referral) — best-effort, non-fatal
        try:
            if len(users_created) >= 2:
                referrer = users_created[0][0]  # alex_trader
                referred = users_created[1][0]  # sarah_quant
                # mark the referred user's referred_by_user_id
                referred.referred_by_user_id = referrer.id
                await db.commit()
                print(f"[seed] Set up affiliate: {referrer.username} referred {referred.username}")

                # And a payout record
                payout = AffiliatePayout(
                    user_id=referrer.id,
                    amount=random.choice([25.0, 49.0, 99.0]),
                    status="paid",
                )
                db.add(payout)
                await db.commit()
                print(f"[seed] Created 1 affiliate payout")
        except Exception as e:
            print(f"[seed] WARNING: affiliate seed skipped due to model drift: {e}")
            await db.rollback()

        print()
        print("=" * 60)
        print(f"[seed] Done. Summary:")
        print(f"  Users:     {len(users_created)}")
        print(f"  Strategies:{strategies_created}")
        print(f"  Backtests: {backtests_created}")
        print(f"  Marked with admin_notes='{DEMO_MARKER}' for easy cleanup")
        print()
        print(f"[seed] To remove ALL demo data later:")
        print(f"  docker compose exec postgres env PGPASSWORD=\$POSTGRES_PASSWORD psql \\")
        print(f"      -h 127.0.0.1 -U \$POSTGRES_USER -d \$POSTGRES_DB \\")
        print(f"      -c \"DELETE FROM users WHERE admin_notes = '{DEMO_MARKER}';\"")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(seed())
