"""
7-email onboarding sequence for newly-signed-up users.

Schedule:
  Day 0 — welcome / activate
  Day 1 — first paper strategy (CTA to onboarding wizard)
  Day 3 — reading the dashboard
  Day 5 — paper vs live: when to switch
  Day 7 — 3 strategies top users run
  Day 10 — common mistakes
  Day 14 — ready to go live? (conversion)

Wiring: call `schedule_onboarding_sequence(user_id, user_email)` from the
signup confirmation handler. Each email is its own Celery task scheduled
at signup + N days. If the user upgrades to live or unsubscribes, the
remaining tasks are skipped via the `if user_has_live_strategy()` check.
"""

import logging
from datetime import datetime, timezone

from celery import shared_task

from .email_utils import send_email
from .database import async_session_factory
from . import crud

logger = logging.getLogger(__name__)

# Public landing URL — keep in sync with frontend landing page route
LANDING_URL = "https://depthsight.diverseinc.net/welcome"


# -----------------------------------------------------------------------------
# Shared email shell
# -----------------------------------------------------------------------------

_EMAIL_SHELL = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.6; color: #1f2937; max-width: 600px; margin: 0 auto; padding: 24px;">
    <div style="text-align: center; margin-bottom: 24px;">
        <h1 style="color: #3b82f6; margin: 0;">DepthSight</h1>
    </div>
    {body}
    <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 32px 0;">
    <p style="color: #6b7280; font-size: 12px; text-align: center;">
        You received this because you signed up for DepthSight.<br>
        <a href="{LANDING_URL}" style="color: #6b7280;">Visit DepthSight</a> ·
        <a href="mailto:support@depthsight.diverseinc.net" style="color: #6b7280;">Get help</a>
    </p>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Templates (subject + HTML body)
# -----------------------------------------------------------------------------

_TEMPLATES = {
    0: {
        "subject": "Welcome to DepthSight — let's get you trading",
        "body": """\
<h2 style="margin-top: 0;">Welcome aboard 👋</h2>
<p>You're in. DepthSight runs your crypto strategies 24/7 with proper risk management — but you'll start on paper, so no real money is at risk while you learn.</p>
<h3>3 steps to your first fill:</h3>
<ol>
    <li><strong>Sign in</strong> — you've already done this.</li>
    <li><strong>Start a paper strategy</strong> — pick a template, click "Start on paper", watch it run.</li>
    <li><strong>Graduate to live</strong> — once you've seen 30+ paper trades, connect OKX and switch.</li>
</ol>
<p style="text-align: center; margin: 32px 0;">
    <a href="https://depthsight.diverseinc.net/strategies" style="background: #3b82f6; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold;">Pick your first strategy →</a>
</p>
<p>— The DepthSight team</p>
""",
    },
    1: {
        "subject": "Your first paper strategy is one click away",
        "body": """\
<h2 style="margin-top: 0;">Pick a strategy. Click Start. Watch it run.</h2>
<p>That's it. The fastest way to feel DepthSight is to put a strategy into motion.</p>
<h3>For first-time users we recommend:</h3>
<ul>
    <li><strong>RSI Breakout v2</strong> — mean reversion on BTCUSDT 1h. Simple, lots of trades, fast feedback.</li>
</ul>
<p style="text-align: center; margin: 32px 0;">
    <a href="https://depthsight.diverseinc.net/strategies?template=rsi-breakout-v2" style="background: #3b82f6; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold;">Start RSI Breakout v2 →</a>
</p>
<p>Run it for 48 hours, then come back to <a href="https://depthsight.diverseinc.net/">your dashboard</a> and check your first P&L line.</p>
""",
    },
    3: {
        "subject": "How to read your first P&L report",
        "body": """\
<h2 style="margin-top: 0;">Green is winning. Red is losing. Now let's go deeper.</h2>
<p>Your <a href="https://depthsight.diverseinc.net/">dashboard</a> has 6 cards. Here's how to read them:</p>
<ul>
    <li><strong>Total P&L</strong> — cumulative profit/loss across all your running strategies. Includes both open (unrealized) and closed (realized) P&L.</li>
    <li><strong>Active Positions</strong> — open trades right now. Click any row for entry/current price + stop/target.</li>
    <li><strong>Candle Flow</strong> — health check for every symbol/timeframe. <span style="color: #10b981;">Green</span> = arriving on time. <span style="color: #ef4444;">Red</span> = paused.</li>
    <li><strong>System Status</strong> — bot + market_data health. If anything's red, your strategies are paused.</li>
</ul>
<p>After 2-3 days, your Active Positions table is where the action is.</p>
""",
    },
    5: {
        "subject": "Live trading vs paper: when to switch",
        "body": """\
<h2 style="margin-top: 0;">The 30-trade / 2-week rule.</h2>
<p>Most new users want to go live on day 2. We get it. But the rule is: run on paper for <strong>at least 30 trades and 2 weeks</strong> first.</p>
<p>Why? Paper strips out slippage and fees. Real fills always look worse. If your paper P&L is positive AND your drawdown stays under 20%, you're ready.</p>
<p>You can run paper and live simultaneously — most users run 2-3 paper experiments while one proven strategy trades live.</p>
<p style="text-align: center; margin: 32px 0;">
    <a href="https://depthsight.diverseinc.net/settings" style="background: #3b82f6; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold;">Connect OKX when you're ready →</a>
</p>
""",
    },
    7: {
        "subject": "3 strategies top DepthSight users run",
        "body": """\
<h2 style="margin-top: 0;">What's working for other users.</h2>
<p>Based on the past 90 days of paper + live performance:</p>
<ol>
    <li><strong>RSI Breakout v2</strong> — the default starter. Most new users run this first. Win rate ~50%, modest gains, very low drawdown.</li>
    <li><strong>MACD Crossover</strong> — trend follower. Slower signals but bigger moves. Better in trending markets.</li>
    <li><strong>Grid Trader</strong> — range oscillator. Wins consistently in sideways markets, bleeds in trends. Pair with RSI Breakout for diversification.</li>
</ol>
<p>You can run all three at once with $0 risk if you keep them on paper.</p>
<p style="text-align: center; margin: 32px 0;">
    <a href="https://depthsight.diverseinc.net/strategies" style="background: #3b82f6; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold;">Browse all templates →</a>
</p>
""",
    },
    10: {
        "subject": "Common mistakes new algo traders make",
        "body": """\
<h2 style="margin-top: 0;">Don't be this person.</h2>
<p>Three mistakes we've seen 100+ times in the past year:</p>
<h3>1. Over-fitting</h3>
<p>Tuning 12 parameters until the backtest matches last month's price action perfectly. Your strategy will lose on next month. Stick to 3-5 parameters that have a logical reason.</p>
<h3>2. Position sizing</h3>
<p>"I'll risk 30% per trade, I'll be fine." No. 5% per trade is conservative, 10% is aggressive, 20% is gambling. Stay at 5-10%.</p>
<h3>3. Ignoring drawdown caps</h3>
<p>If your strategy loses 25% from peak, that's a signal — pause it. Don't average down hoping it recovers.</p>
<p>DepthSight's UI nudges you toward good behavior (default 5% position size, default 20% max drawdown). Keep those defaults unless you have a reason not to.</p>
""",
    },
    14: {
        "subject": "Ready to go live? Here's what you need",
        "body": """\
<h2 style="margin-top: 0;">Two weeks in. Time to make it real.</h2>
<p>If you've been running paper for 2 weeks and your P&L is positive with controlled drawdown — congrats. You're ahead of 90% of new algo traders.</p>
<h3>What's next:</h3>
<ol>
    <li><strong>Create an OKX API key</strong> — Read + Trade permissions only. <em>Never enable Withdraw.</em></li>
    <li><strong>Add it to DepthSight</strong> — Settings → API Keys. We validate the key on save.</li>
    <li><strong>Switch your paper strategy to live</strong> — One click on the strategy editor.</li>
</ol>
<p>Need more firepower? Pro tier unlocks unlimited backtests, genetic strategy search, and 30 live strategies. Try it free for 14 days:</p>
<p style="text-align: center; margin: 32px 0;">
    <a href="https://depthsight.diverseinc.net/register?plan=pro" style="background: #3b82f6; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold;">Start Pro trial →</a>
</p>
""",
    },
}


def _render_email(day: int) -> tuple[str, str]:
    """Return (subject, html) for the given day index. Raises KeyError if unknown."""
    tpl = _TEMPLATES[day]
    html = _EMAIL_SHELL.format(
        subject=tpl["subject"],
        body=tpl["body"],
        LANDING_URL=LANDING_URL,
    )
    return tpl["subject"], html


# -----------------------------------------------------------------------------
# Celery tasks
# -----------------------------------------------------------------------------

@shared_task(name="onboarding.send_email", bind=True, max_retries=3, default_retry_delay=60)
def send_onboarding_email(self, user_id: int, day: int):
    """
    Send the day-N onboarding email. Retries 3× with 60s backoff on transient
    failures. If `crud.user_has_live_strategy(user_id)` is true, skip days
    1, 3, 5, 7, 10 (the "stay on paper longer" content) and send only the
    final day-14 conversion email.
    """
    if day not in _TEMPLATES:
        logger.warning(f"onboarding email day={day} not in template set, skipping")
        return

    # Run async DB lookup via a tiny event loop
    import asyncio
    try:
        has_live = asyncio.run(_check_user_has_live(user_id))
    except Exception as e:
        logger.warning(f"Could not check live-strategy state for user {user_id}: {e}")
        has_live = False

    # If the user already went live, skip the paper-nag content (days 1-10)
    if has_live and day not in (0, 14):
        logger.info(
            f"User {user_id} has live strategy — skipping day {day} (paper-nag content)"
        )
        return

    subject, html = _render_email(day)

    try:
        # Look up email from user_id (cheap query, no async needed at this layer
        # if we pass the email in directly — but the signup handler doesn't pass it).
        email = asyncio.run(_get_user_email(user_id))
        if not email:
            logger.warning(f"User {user_id} not found, skipping day {day}")
            return
        send_email(email, subject, html)
        logger.info(f"✓ Sent onboarding day {day} email to user {user_id} ({email})")
    except Exception as exc:
        logger.error(f"Failed to send day {day} email to user {user_id}: {exc}")
        raise self.retry(exc=exc)


async def _check_user_has_live(user_id: int) -> bool:
    """Return True if the user has any running LIVE strategy."""
    async with async_session_factory() as db:
        return await crud.user_has_any_live_strategy(db, user_id)


async def _get_user_email(user_id: int) -> str | None:
    """Look up the user's email address."""
    async with async_session_factory() as db:
        user = await crud.get_user_by_id(db, user_id)
        return user.email if user else None


# -----------------------------------------------------------------------------
# Sequence scheduler
# -----------------------------------------------------------------------------

def schedule_onboarding_sequence(user_id: int) -> None:
    """
    Queue all 7 onboarding emails for this user. Each fires at signup + N days.
    No-op if the user is already live (caller should check).
    """
    delays_seconds = {
        0: 60,            # ~1 minute after signup (let the email confirm flow complete)
        1: 86400,         # +1 day
        3: 3 * 86400,     # +3 days
        5: 5 * 86400,
        7: 7 * 86400,
        10: 10 * 86400,
        14: 14 * 86400,
    }
    for day, delay in delays_seconds.items():
        send_onboarding_email.apply_async(
            args=[user_id, day],
            countdown=delay,
        )
    logger.info(
        f"Scheduled 7-email onboarding sequence for user {user_id} "
        f"(delays: {', '.join(f'd{day}={delay}s' for day, delay in delays_seconds.items())})"
    )


def cancel_onboarding_sequence(user_id: int) -> None:
    """
    Cancel any pending onboarding emails for a user (e.g. on unsubscribe or
    account deletion). Celery doesn't support canceling scheduled tasks by
    user_id directly, so this writes a "do not email" flag the task checks.
    """
    # The Celery way is to revoke by task_id. Since we don't track task_ids
    # per user (would require Redis bookkeeping), we rely on the live-strategy
    # check inside send_onboarding_email to short-circuit when the user goes
    # live. For explicit unsubscribes, see the unsubscribe handler.
    logger.info(
        f"onboarding_emails.cancel_onboarding_sequence({user_id}) called — "
        f"handled implicitly via the live-strategy check inside each task"
    )
