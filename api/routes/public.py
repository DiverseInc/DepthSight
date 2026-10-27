import logging
from typing import Any, Dict

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud, models, schemas
from ..database import get_db
from ..demo_seed import not_demo_accounts
from ..og_image_generator import generate_og_image
from ..plans import build_plan_catalog
from ..redis_client import get_redis_client


logger = logging.getLogger(__name__)

public_router = APIRouter(tags=["Public"])


@public_router.get("/api/v1/public/plans")
async def get_public_plans():
    """Public plan catalog for the marketing pricing page.

    Deliberately unauthenticated: the landing page is a public route and has
    to be able to render prices before anyone logs in.

    It returns the SAME catalog the authenticated billing page uses
    (`build_plan_catalog`), so the marketing page cannot advertise a tier
    that differs from the tier the backend actually grants.
    """
    return build_plan_catalog()



@public_router.get(
    "/api/v1/shared/{public_slug}",
    response_model=schemas.ApiResponseData[schemas.SharedBacktestData],
)
async def get_shared_backtest_data(
    public_slug: str, db: AsyncSession = Depends(get_db)
):
    shared_backtest = await crud.get_shared_backtest_by_slug(
        db, public_slug=public_slug
    )
    if not shared_backtest or not shared_backtest.backtest_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found or access is closed.",
        )

    run = shared_backtest.backtest_run
    run_params = run.parameters_json or {}
    strategy_config = run_params.get("config") if isinstance(run_params, dict) else None
    if not isinstance(strategy_config, dict):
        strategy_config = None
    strategy_display_name = (
        run_params.get("name")
        or run_params.get("strategy_display_name")
        or (strategy_config or {}).get("name")
        or run.strategy_name
    )

    response_data = {
        "strategyName": run.strategy_name
        if shared_backtest.is_strategy_name_public
        else "Private Strategy",
        "symbol": run.symbol,
        "period": {"start": run.start_date, "end": run.end_date},
        "kpis": run.kpi_results_json or {},
        "equityCurve": run.equity_curve_json or [],
        "parameters": run.parameters_json
        if shared_backtest.are_parameters_public
        else None,
        "strategyConfig": run.parameters_json.get("config")
        if shared_backtest.are_parameters_public and run.parameters_json
        else None,
    }

    if shared_backtest.is_strategy_name_public:
        response_data["strategyName"] = strategy_display_name
    if shared_backtest.are_parameters_public:
        response_data["parameters"] = run_params
        response_data["strategyConfig"] = strategy_config

    return {"data": response_data}


@public_router.get("/r/{ref_code}")
async def affiliate_redirector(
    ref_code: str, request: Request, redis: redis.Redis = Depends(get_redis_client)
):
    effective_ref = ref_code
    if ref_code == "register":
        effective_ref = request.query_params.get("ref", "register")

    logger.info(
        "AFFILIATE REDIRECT: Received request for ref_code='%s', effective_ref='%s'",
        ref_code,
        effective_ref,
    )
    await crud.increment_referral_clicks(
        redis_client=redis, referral_code=effective_ref
    )

    return RedirectResponse(f"/register?ref={effective_ref}", status_code=302)


@public_router.get("/og-image/{public_slug}.png")
async def get_og_image(public_slug: str, db: AsyncSession = Depends(get_db)):
    data_response = await get_shared_backtest_data(public_slug, db)
    shared_data = data_response["data"]

    try:
        image_bytes = await generate_og_image(
            schemas.SharedBacktestData.model_validate(shared_data)
        )
        return Response(content=image_bytes, media_type="image/png")
    except Exception as e:
        logger.error(
            "Failed to generate OG image for slug %s: %s", public_slug, e, exc_info=True
        )
        raise HTTPException(status_code=500, detail="Could not generate report image.")


@public_router.get("/render-shared/{public_slug}", response_class=HTMLResponse)
async def render_shared_backtest(
    public_slug: str, request: Request, db: AsyncSession = Depends(get_db)
):
    try:
        data_response = await get_shared_backtest_data(public_slug, db)
        data = data_response["data"]

        strategy_name = data.get("strategyName", "Trading Strategy")
        symbol = data.get("symbol", "Asset")
        kpis = data.get("kpis", {})
        pnl = kpis.get("total_pnl", 0)
        pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f} USD"
        win_rate = kpis.get("win_rate", 0)

        title = f"DepthSight Report: {strategy_name} on {symbol}"
        description = f"Performance: {pnl_str} PNL | Win Rate: {win_rate:.1f}%. View detailed backtest analytics on DepthSight AI Platform."

        host = request.headers.get("host", "depthsight.diverseinc.net")
        scheme = request.headers.get("x-forwarded-proto", "https")
        abs_base_url = f"{scheme}://{host}"

        image_url = f"{abs_base_url}/og-image/{public_slug}.png"
        page_url = f"{abs_base_url}/s/{public_slug}"

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <meta name="description" content="{description}" />
    
    <!-- Open Graph / Facebook -->
    <meta property="og:type" content="website">
    <meta property="og:url" content="{page_url}">
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{description}">
    <meta property="og:image" content="{image_url}">
    <meta property="og:image:width" content="1200">
    <meta property="og:image:height" content="630">

    <!-- Twitter -->
    <meta property="twitter:card" content="summary_large_image">
    <meta property="twitter:url" content="{page_url}">
    <meta property="twitter:title" content="{title}">
    <meta property="twitter:description" content="{description}">
    <meta property="twitter:image" content="{image_url}">

    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    
    <!-- Redirect for regular users if they get here directly -->
    <script>
        window.location.href = "/s/{public_slug}";
    </script>
</head>
<body style="background-color: #0d1117; color: white; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
    <div style="text-align: center; padding: 20px;">
        <h1 style="color: #58a6ff;">{strategy_name}</h1>
        <p style="font-size: 1.2em;">{symbol} | {pnl_str}</p>
        <p style="color: #8b949e;">Loading report...</p>
        <img src="{image_url}" alt="Equity Curve" style="max-width: 100%; border-radius: 8px; margin-top: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.5);">
    </div>
</body>
</html>"""
        return HTMLResponse(content=html_content)

    except Exception as e:
        logger.error("Error rendering shared page for bot: %s", e, exc_info=True)
        raise HTTPException(status_code=404, detail="Report not found")


# --- Public screener feed ---

# The two metrics a published result can be ranked by. These are the same
# categories the in-app leaderboard uses, so the public feed and the
# authenticated leaderboard can never disagree about a number.
PUBLIC_RESULT_CATEGORIES = ("sharpe_ratio", "net_pnl_percent")
PUBLIC_RESULT_PERIODS = ("weekly", "monthly", "all_time")


def _public_strategy_name(run, shared_backtest) -> str:
    """The strategy name exactly as the PUBLIC share page shows it.

    Mirrors `get_shared_backtest_data`: an owner who did not publish their
    strategy name gets "Private Strategy", never the real one.
    """
    if not shared_backtest.is_strategy_name_public:
        return "Private Strategy"

    run_params = run.parameters_json or {}
    if not isinstance(run_params, dict):
        return run.strategy_name
    strategy_config = run_params.get("config")
    if not isinstance(strategy_config, dict):
        strategy_config = None
    return (
        run_params.get("name")
        or run_params.get("strategy_display_name")
        or (strategy_config or {}).get("name")
        or run.strategy_name
    )


def _public_result_metrics(run) -> Dict[str, Any]:
    """The published backtest KPIs -- the same ones the public share page shows.

    A metric the backtest never recorded stays None, so the page can print
    "not reported" instead of a 0.00 the run never produced.
    """
    kpis = run.kpi_results_json or {}
    if not isinstance(kpis, dict):
        kpis = {}

    total_pnl = kpis.get("total_pnl")
    initial_balance = run.initial_balance
    net_pnl_percent = None
    if (
        isinstance(total_pnl, (int, float))
        and not isinstance(total_pnl, bool)
        and isinstance(initial_balance, (int, float))
        and not isinstance(initial_balance, bool)
        and initial_balance
    ):
        net_pnl_percent = (total_pnl / initial_balance) * 100

    trades = kpis.get("trades")
    if trades is None:
        trades = kpis.get("total_trades")

    return {
        "sharpeRatio": kpis.get("sharpe_ratio"),
        "netPnlPercent": net_pnl_percent,
        "netPnl": total_pnl,
        "winRate": kpis.get("win_rate"),
        "maxDrawdown": kpis.get("max_drawdown"),
        "trades": trades,
    }


@public_router.get(
    "/api/v1/public/published-results",
    response_model=schemas.ApiResponseData[schemas.PublicPublishedResults],
)
async def get_public_published_results(
    category: str = Query(
        "sharpe_ratio",
        json_schema_extra={"enum": list(PUBLIC_RESULT_CATEGORIES)},
    ),
    period: str = Query(
        "all_time",
        json_schema_extra={"enum": list(PUBLIC_RESULT_PERIODS)},
    ),
    limit: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Public feed of the backtest results users published for comparison.

    WHAT THIS IS
    ------------
    The public screener page (a no-login page) needs to show a visitor
    something real rather than a promise. The only backtest results this
    platform publishes on purpose are the ones a user ticked "publish to
    leaderboard" for AND whose public link is still switched on. Those are
    listed here, without the owner's identity.

    WHAT THIS IS NOT
    ----------------
    It is not a window onto anybody's account. The payload carries no user
    id, no username, no e-mail, no balance, no equity, no XP, no positions
    and no API keys -- only the backtest metrics that are already public on
    the owner's own report page (/s/{slug}). A backtest that was never
    published, or whose public link the owner has switched off, is not
    listed at all, and a seeded demo account is excluded by the same rule
    the public counters already use.

    The published `rank` is the position in the list this endpoint returns,
    NOT the stored leaderboard rank: the stored one counts every entry ever
    written, including the seeded demo accounts, so it describes a list the
    visitor cannot see.
    """
    if category not in PUBLIC_RESULT_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category must be one of: " + ", ".join(PUBLIC_RESULT_CATEGORIES),
        )
    if period not in PUBLIC_RESULT_PERIODS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="period must be one of: " + ", ".join(PUBLIC_RESULT_PERIODS),
        )

    leaderboard_period = models.LeaderboardPeriod(period)

    def _joined_select():
        """The one joined query both the list and its count are built from, so
        the two can never describe different sets."""
        return (
            select(
                models.LeaderboardEntry,
                models.BacktestRun,
                models.SharedBacktest,
            )
            .join(
                models.BacktestRun,
                models.LeaderboardEntry.backtest_run_id == models.BacktestRun.id,
            )
            .join(
                models.SharedBacktest,
                models.LeaderboardEntry.shared_backtest_slug
                == models.SharedBacktest.public_slug,
            )
            .join(models.User, models.LeaderboardEntry.user_id == models.User.id)
        )

    filters = (
        models.LeaderboardEntry.period == leaderboard_period,
        models.LeaderboardEntry.category == category,
        # The owner must still have the public link switched on.
        models.SharedBacktest.is_active.is_(True),
        # Never advertise a run that did not finish.
        models.BacktestRun.status == "COMPLETED",
        # A seeded demo account is not a customer: its fabricated results must
        # never be advertised to a prospect.
        not_demo_accounts(models.User),
    )

    # How many published results this ranking covers, over the SAME filters.
    total = (
        await db.execute(
            _joined_select()
            .with_only_columns(func.count(models.LeaderboardEntry.id))
            .where(*filters)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            _joined_select()
            .where(*filters)
            .order_by(
                desc(models.LeaderboardEntry.score),
                # A deterministic tie-break: two equal scores must not swap
                # places between two calls, or a visitor's card would change
                # position on every page load. The public slug is unique.
                models.SharedBacktest.public_slug,
            )
            .limit(limit)
        )
    ).all()

    # The published rank is the position in THIS list, not the stored global
    # rank: the stored one counts every entry ever written, including the
    # seeded demo accounts, so it describes a list the visitor cannot see.
    results = [
        schemas.PublicPublishedResult(
            slug=shared_backtest.public_slug,
            strategy_name=_public_strategy_name(run, shared_backtest),
            symbol=run.symbol,
            period={"start": run.start_date, "end": run.end_date},
            category=entry.category,
            score=entry.score,
            rank=position,
            metrics=_public_result_metrics(run),
            has_published_backtest=True,
        )
        for position, (entry, run, shared_backtest) in enumerate(rows, start=1)
    ]

    return {
        "data": {
            "category": category,
            "period": period,
            "basis": "backtest",
            "total": total,
            "results": results,
        }
    }

