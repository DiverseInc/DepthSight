"""Paper-mode portfolio must report the open position's unrealized PnL.

`GET /api/v1/portfolio?mode=paper` has a dedicated early-return branch that
sums the paper WALLET and nothing else. The wallet holds realized cash only, so
`total_unrealized_pnl` was never populated and fell through to the schema
default of 0.0 -- every paper account showed "$0.00 unrealized" while holding an
open position worth hundreds. The bot publishes `total_unrealized_pnl` in
`depthsight:state:portfolio:{user_id}:{api_key_id}`; this branch simply never
read it.

The frontend compounded it: PortfolioOverview.tsx rendered `today_pnl` (which is
REALIZED pnl for the day) into the "Unrealized PnL" tile and computed
Equity = balance + today_pnl.

These tests drive the REAL endpoint function, extracted by AST and bound to the
real module globals, so they cannot drift from production.
"""

import ast
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTES = "api/routes/portfolio.py"


def _Depends(dependency=None):  # noqa: N802 - mirrors fastapi.Depends
    return None


def _Query(default=None, *args, **kwargs):  # noqa: N802 - mirrors fastapi.Query
    return default


def _extract_endpoint():
    source = (REPO_ROOT / ROUTES).read_text(encoding="utf-8")
    tree = ast.parse(source, ROUTES)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_portfolio_status":
            node.decorator_list = []
            node.returns = None
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                arg.annotation = None
            module = ast.Module(body=[node], type_ignores=[])
            # Stripping annotations is not enough: the parameter DEFAULTS
            # (Depends(get_redis_client), Query("live", ...)) are evaluated when
            # the def statement executes, so these names must resolve here too.
            fn_globals: Dict[str, Any] = {
                "__name__": "paper_portfolio_under_test",
                "Depends": _Depends,
                "Query": _Query,
                "HttpSessDep": None,
                "get_redis_client": lambda: None,
                "get_current_user": lambda: None,
                "get_db": lambda: None,
                "MARKET_TYPE_ALL": "all",
            }
            exec(compile(module, ROUTES, "exec"), fn_globals)
            return fn_globals["get_portfolio_status"]
    raise AssertionError("get_portfolio_status not found in portfolio.py")


class _Schemas:
    """PortfolioStatus with the real schema's defaults, so an omitted field
    is observable as 0.0 rather than raising."""

    class PortfolioStatus:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.__dict__.setdefault("total_unrealized_pnl", 0.0)
            self.__dict__.setdefault("total_available", 0.0)
            self.__dict__.setdefault("total_margin_used", 0.0)
            self.__dict__.setdefault("market_breakdown", [])
            self.__dict__.setdefault("market_type", "all")


class _FakeRedis:
    def __init__(self, payloads: Dict[str, str]):
        self.payloads = payloads

    async def keys(self, pattern: str):
        prefix = pattern.rstrip("*")
        return [k for k in self.payloads if k.startswith(prefix)]

    async def mget(self, keys):
        return [self.payloads.get(k) for k in keys]


class _FakeDB:
    def __init__(self, pnl_value):
        self._pnl = pnl_value

    async def execute(self, *a, **k):
        result = SimpleNamespace()
        result.scalar = lambda: self._pnl
        return result

    async def commit(self):
        pass


def _call(redis_payloads, wallet_balance=9995.50497923637, pnl=0.0):
    endpoint = _extract_endpoint()
    wallet = [SimpleNamespace(asset="USDT", balance=wallet_balance)]

    async def get_paper_wallet(*a, **k):
        return wallet

    async def get_or_init(*a, **k):
        return wallet

    # Bind against the REAL module globals, overriding only what the paper
    # branch actually touches, so every other dependency is genuine.
    import api.routes.portfolio as real_module

    fn_globals = dict(real_module.__dict__)
    fn_globals["crud"] = SimpleNamespace(
        get_paper_wallet=get_paper_wallet,
        init_or_reset_paper_wallet=get_or_init,
    )
    fn_globals["schemas"] = _Schemas
    fn_globals["bot_config"] = SimpleNamespace(
        REDIS_STATE_KEY_PORTFOLIO="depthsight:state:portfolio"
    )
    fn_globals["datetime"] = datetime
    fn_globals["timezone"] = timezone
    fn_globals["json"] = json

    fn = endpoint
    fn.__globals__.clear()
    fn.__globals__.update(fn_globals)

    async def main():
        return await fn(
            redis_client=_FakeRedis(redis_payloads),
            current_user=SimpleNamespace(id=10, username="alex_trader"),
            http_session=None,
            db=_FakeDB(pnl),
            mode="paper",
            api_key_id=None,
            market_type="all",
        )

    return asyncio.run(main())


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_paper_portfolio_reports_unrealized_pnl_from_bot_state():
    # NOTE: the real bot payload has NO "mode" field. An earlier version of
    # this fix filtered on state["mode"] == "paper" and therefore skipped every
    # key, reporting 0.0 -- it shipped broken and was caught by measuring the
    # live response. The discriminator is the key suffix, not the value.
    published = {
        "depthsight:state:portfolio:10:None": json.dumps(
            {
                "user_id": 10,
                "total_wallet_balance": 9995.50497923637,
                "total_unrealized_pnl": 48.2925,
                "total_equity": 10043.7975,
                "today_pnl": 0.0,
            }
        )
    }
    data = _call(published)["data"]

    assert data.total_unrealized_pnl == 48.29, (
        "Paper portfolio must forward the bot's published unrealized PnL. "
        f"Got {data.total_unrealized_pnl!r}."
    )
    # Balance semantics must NOT change -- it stays realized wallet cash.
    assert abs(data.balance - 9995.50497923637) < 1e-9


def test_paper_portfolio_reports_a_blocked_paper_account():
    """Paper-mode requests must be able to report trading as switched OFF.

    Found by two reviewers after the multi-controller aggregation was fixed:
    `?mode=paper` takes a dedicated early-return branch that returned BEFORE
    that aggregation ran, and it hardcoded `is_trading_allowed=True`. So the
    one population this whole fix is about -- a paper account whose signals are
    being rejected by the risk gate -- still could not be shown as blocked.
    """
    published = {
        "depthsight:state:portfolio:10:None": json.dumps(
            {
                "user_id": 10,
                "total_wallet_balance": 2645.55,
                "total_unrealized_pnl": 0.0,
                "total_equity": 2645.55,
                "today_pnl": 0.0,
                "is_trading_allowed": False,
            }
        )
    }
    data = _call(published)["data"]

    assert data.is_trading_allowed is False, (
        "the paper branch hardcoded True, so a paper account blocked by the "
        "risk gate was indistinguishable from a healthy one"
    )


def test_paper_portfolio_reports_allowed_when_every_controller_is_allowed():
    """Positive control: the fix is not 'always report blocked'."""
    published = {
        "depthsight:state:portfolio:10:None": json.dumps(
            {
                "user_id": 10,
                "total_wallet_balance": 2645.55,
                "total_unrealized_pnl": 0.0,
                "total_equity": 2645.55,
                "today_pnl": 0.0,
                "is_trading_allowed": True,
            }
        )
    }
    data = _call(published)["data"]

    assert data.is_trading_allowed is True


def test_paper_portfolio_treats_one_blocked_controller_as_blocked():
    published = {
        "depthsight:state:portfolio:10:None": json.dumps(
            {"total_equity": 2645.55, "is_trading_allowed": True}
        ),
        "depthsight:state:portfolio:10:paper-2": json.dumps(
            {"total_equity": 100.0, "is_trading_allowed": False}
        ),
    }
    data = _call(published)["data"]

    assert data.is_trading_allowed is False


def test_paper_portfolio_ignores_live_keys_by_numeric_api_key_id():
    """Live controllers publish a numeric api_key_id suffix; paper has none."""
    published = {
        "depthsight:state:portfolio:10:None": json.dumps(
            {"user_id": 10, "total_unrealized_pnl": 30.0}
        ),
        "depthsight:state:portfolio:10:5": json.dumps(
            {"user_id": 10, "total_unrealized_pnl": 999.0}
        ),
        "depthsight:state:portfolio:10:9": json.dumps(
            {"user_id": 10, "total_unrealized_pnl": 12.5}
        ),
    }
    data = _call(published, wallet_balance=10000.0)["data"]
    assert data.total_unrealized_pnl == 30.0, (
        "Must sum only paper keys (non-numeric api_key_id suffix) and never "
        "double-count live controllers."
    )


def test_paper_portfolio_aggregates_multiple_paper_keys():
    published = {
        "depthsight:state:portfolio:10:None": json.dumps(
            {"user_id": 10, "total_unrealized_pnl": 30.0}
        ),
        "depthsight:state:portfolio:10:paper_a": json.dumps(
            {"user_id": 10, "total_unrealized_pnl": 12.5}
        ),
    }
    data = _call(published, wallet_balance=10000.0)["data"]
    assert data.total_unrealized_pnl == 42.5


def test_paper_portfolio_survives_missing_or_corrupt_bot_state():
    """A stopped or mid-write bot must not 500 the dashboard."""
    assert _call({}, wallet_balance=10000.0)["data"].total_unrealized_pnl == 0.0
    corrupt = {"depthsight:state:portfolio:10:None": "not-json"}
    assert _call(corrupt, wallet_balance=10000.0)["data"].total_unrealized_pnl == 0.0


def test_paper_portfolio_with_no_open_positions_still_reports_zero():
    """Must not invent a number when the bot reports none."""
    published = {
        "depthsight:state:portfolio:10:None": json.dumps(
            {"user_id": 10, "mode": "paper", "total_unrealized_pnl": 0}
        )
    }
    assert _call(published, wallet_balance=10000.0)["data"].total_unrealized_pnl == 0.0