"""The portfolio API must report the risk state the trading gate actually reads.

THE DEFECT
----------
`GET /api/v1/portfolio` reports three things about the account's risk state:
`is_trading_allowed`, `consecutive_losses` and (now) the reason trading is
blocked. Before this change:

1. `consecutive_losses` was a hardcoded `0` in the PAPER branch and in the
   LIVE-EXCHANGE branch, and in the LIVE-REDIS-FALLBACK branch it was read
   from a dict the aggregation never populated. So the API said "0
   consecutive losses" while the controller's RiskManager was blocked at the
   consecutive-loss limit (10/10).
2. The gate's REASON was never published. `RiskManager.get_trading_disabled_reason()`
   existed and was even used for the rejection log line, but the controller
   never put it in the published payload, so the API could say "trading is
   blocked" and never say why.
3. The LIVE-REDIS-FALLBACK branch aggregated `depthsight:state:portfolio:{id}:*`
   with NO mode filter, so a PAPER controller's published equity became the
   LIVE balance. `bot_runner.py` starts a paper controller for every user who
   does not have one, so a ":None" key sitting next to the live keys is the
   NORMAL case -- not an edge case.

All three are REPORTING defects. Nothing here loosens a risk control: the
counter is read, never written, and the gate's own decision is untouched.

These tests drive the REAL endpoint function (extracted by AST and bound to
the real module globals, so they cannot drift from production) and the REAL
RiskManager, and they assert OUTCOMES rather than the shape of the fix.
"""

import ast
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTES = "api/routes/portfolio.py"
CONTROLLER = "bot_module/controller.py"

USER_ID = 10
PORTFOLIO_KEY = f"depthsight:state:portfolio:{USER_ID}"


# ---------------------------------------------------------------------------
# The real endpoint, extracted by AST and bound to the real module globals
# ---------------------------------------------------------------------------


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
            # The parameter DEFAULTS (Depends(...), Query(...)) are evaluated
            # when the def statement executes, so these names must resolve.
            fn_globals: Dict[str, Any] = {
                "__name__": "portfolio_under_test",
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


class _FakeRedis:
    """The bits of Redis the portfolio endpoint reads."""

    def __init__(self, payloads: Dict[str, str]):
        self.payloads = payloads

    async def keys(self, pattern: str):
        prefix = pattern.rstrip("*")
        return [k for k in self.payloads if k.startswith(prefix)]

    async def mget(self, keys):
        return [self.payloads.get(k) for k in keys]

    async def get(self, key):
        return self.payloads.get(key)


class _FakeDB:
    def __init__(self, pnl: float = 0.0):
        self._pnl = pnl

    async def execute(self, *a, **k):
        return SimpleNamespace(scalar=lambda: self._pnl)

    async def commit(self):
        pass


class _Crud:
    """The crud functions the endpoint touches, per branch."""

    def __init__(self, active_keys=None, paper_balance: float = 10000.0):
        self._active_keys = active_keys or []
        self._paper_balance = paper_balance

    async def get_active_api_keys_for_user(self, db, user_id=None):
        if not self._active_keys:
            # What the endpoint sees for an account with no API key: the
            # exchange branch raises, and the request lands in the Redis
            # fallback.
            raise ValueError("User has no active API keys configured.")
        return self._active_keys

    async def get_api_key_by_id(self, db, user_id=None, key_id=None):
        for key in self._active_keys:
            if key.id == key_id:
                return key
        return None

    async def get_paper_wallet(self, db, user_id=None):
        return [SimpleNamespace(asset="USDT", balance=self._paper_balance)]

    async def init_or_reset_paper_wallet(self, db, user_id=None):
        return await self.get_paper_wallet(db, user_id=user_id)


def _call(
    mode: str = "paper",
    payloads: Dict[str, str] | None = None,
    api_key_id=None,
    crud: _Crud | None = None,
    db: _FakeDB | None = None,
):
    """Call the REAL endpoint function and return its response."""
    endpoint = _extract_endpoint()

    # Bind against the REAL module globals, overriding only what the branch
    # under test actually touches, so every other dependency is genuine.
    import api.routes.portfolio as real_module

    fn_globals = dict(real_module.__dict__)
    fn_globals["crud"] = crud or _Crud()
    fn_globals["datetime"] = datetime
    fn_globals["timezone"] = timezone
    fn_globals["json"] = json
    fn_globals["bot_config"] = SimpleNamespace(
        REDIS_STATE_KEY_PORTFOLIO="depthsight:state:portfolio"
    )

    fn = endpoint
    fn.__globals__.clear()
    fn.__globals__.update(fn_globals)

    async def main():
        return await fn(
            redis_client=_FakeRedis(payloads or {}),
            current_user=SimpleNamespace(id=USER_ID, username="alex_trader"),
            http_session=None,
            db=db or _FakeDB(),
            mode=mode,
            api_key_id=api_key_id,
            market_type="all",
        )

    return asyncio.run(main())


def _published(
    equity: float = 2645.55,
    allowed: bool = True,
    losses: int = 0,
    reason: str | None = None,
    unrealized: float = 0.0,
) -> str:
    """A portfolio payload in the shape the controller really publishes."""
    return json.dumps(
        {
            "user_id": USER_ID,
            "total_wallet_balance": equity,
            "total_unrealized_pnl": unrealized,
            "total_equity": equity,
            "today_pnl": 0.0,
            "consecutive_losses": losses,
            "is_trading_allowed": allowed,
            "trading_disabled_reason": reason,
        }
    )


# ---------------------------------------------------------------------------
# The real controller payload, extracted from the real source
# ---------------------------------------------------------------------------


def _extract_publish_statements():
    """Compile the REAL statements that build the published portfolio payload.

    Extracting them (rather than copying them) means this test cannot pass
    while the controller publishes something else. The statements that compute
    the reason are extracted WITH the payload, so the RiskManager's own
    accessor is what is exercised -- not a stand-in for it.
    """
    source = (REPO_ROOT / CONTROLLER).read_text(encoding="utf-8")
    tree = ast.parse(source, CONTROLLER)
    wanted = {
        "_gate_open",
        "_reason_getter",
        "_disabled_reason",
        "_candidate",
        "portfolio_status_data",
    }

    def _stmt_ok(stmt) -> bool:
        """Only assignments to the wanted names (and their `if` blocks)."""
        if isinstance(stmt, ast.Assign):
            return all(
                isinstance(t, ast.Name) and t.id in wanted for t in stmt.targets
            )
        if isinstance(stmt, ast.If):
            return not stmt.orelse and all(_stmt_ok(s) for s in stmt.body)
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "_publish_state_to_redis"
        ):
            body = [stmt for stmt in node.body if _stmt_ok(stmt)]
            if not any(
                isinstance(s, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "portfolio_status_data"
                    for t in s.targets
                )
                for s in body
            ):
                raise AssertionError("portfolio_status_data was not extracted")
            module = ast.Module(body=body, type_ignores=[])
            ast.fix_missing_locations(module)
            return compile(module, CONTROLLER, "exec")
    raise AssertionError("_publish_state_to_redis not found in controller.py")


def _real_risk_manager():
    """A real RiskManager with its heavy collaborators stubbed out."""
    from bot_module.risk_manager import RiskManager

    return RiskManager(
        executor=None,
        paper_executor=None,
        user_id=USER_ID,
        db_session=None,
        user_settings={},
        api_key_name="paper",
    )


def _risk_manager_blocked_at_the_loss_limit():
    """A real RiskManager blocked by the REAL consecutive-loss limit."""
    rm = _real_risk_manager()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0
    rm.stats.today_pnl = 0.0
    rm.stats.consecutive_losses = 10
    rm.max_consecutive_losses = 10
    rm.max_drawdown_threshold = 0.99
    rm.daily_max_loss_threshold = 0.99
    rm.min_balance_threshold = 5.0
    rm._is_trading_allowed = True
    rm._check_risk_limits()
    return rm


def _publish_namespace(rm) -> dict:
    """The namespace the real publish statements run in."""
    from bot_module.controller import TradingController

    controller = TradingController.__new__(TradingController)
    controller.user_id = USER_ID
    controller.rm = rm

    return {
        "self": controller,
        "datetime": datetime,
        "timezone": timezone,
        "wallet_balance": 1000.0,
        "total_unrealized_pnl": 12.5,
        "equity": 1012.5,
        "positions_to_publish": [],
        "strategies_to_publish": [],
    }


def _controller_payload(rm) -> dict:
    """The payload the REAL controller publishes, for the REAL RiskManager."""
    namespace = _publish_namespace(rm)
    exec(_extract_publish_statements(), namespace)  # noqa: S102 - the repo's own source
    return namespace["portfolio_status_data"]


# ---------------------------------------------------------------------------
# Part 1 -- the controller publishes the risk state
# ---------------------------------------------------------------------------


def test_the_controller_publishes_the_consecutive_loss_counter():
    """The real counter must reach the published payload."""
    rm = _risk_manager_blocked_at_the_loss_limit()

    payload = _controller_payload(rm)

    assert payload["consecutive_losses"] == 10, (
        f"the controller published {payload['consecutive_losses']!r} instead of "
        "the RiskManager's real counter"
    )


def test_the_controller_publishes_the_reason_the_gate_is_closed():
    """The gate's reason must reach the published payload.

    Without this the API can report that trading is blocked but never WHY --
    the same blind spot the emergency-stop latch had.
    """
    rm = _risk_manager_blocked_at_the_loss_limit()

    payload = _controller_payload(rm)

    assert "trading_disabled_reason" in payload, (
        "the controller does not publish the gate's reason, so the API can "
        "never name the cause of a block"
    )
    assert payload["trading_disabled_reason"], (
        "the gate is closed but no reason was published"
    )
    assert "consec losses" in payload["trading_disabled_reason"].lower()
    assert payload["is_trading_allowed"] is False


def test_the_controller_publishes_no_reason_when_nothing_is_blocked():
    """A healthy controller must not report a block."""
    rm = _real_risk_manager()
    rm._is_trading_allowed = True

    payload = _controller_payload(rm)

    assert payload["is_trading_allowed"] is True
    assert payload.get("trading_disabled_reason") is None


def test_the_controller_publishes_no_reason_while_the_gate_is_open():
    """A STALE or SYMBOL-LEVEL reason must not be published.

    The RiskManager overwrites `_trading_disabled_reason` on ANY refusal --
    including a PER-SYMBOL blacklist hit -- and never clears it when the
    block lifts. Publishing it next to an OPEN gate would claim "this is why
    you cannot trade" about an account that CAN trade, which is the same
    confusion this whole change exists to remove.
    """
    rm = _real_risk_manager()
    rm._is_trading_allowed = True
    # A stale refusal reason: the account traded again after the block.
    rm._trading_disabled_reason = (
        "symbol ETHUSDT is BLACKLISTED until 2099-01-01T00:00:00+00:00 "
        "(reason: operator blacklisted after a bad trade)"
    )

    payload = _controller_payload(rm)

    assert payload["is_trading_allowed"] is True
    assert payload["trading_disabled_reason"] is None, (
        "the account can trade -- a stale or symbol-level reason must not be "
        "published as if it were why the account is blocked"
    )


def test_the_controller_publishes_the_emergency_stop_reason():
    """A blocked controller DOES publish the reason."""
    rm = _real_risk_manager()
    rm.trigger_emergency_stop("operator hit the kill switch")

    payload = _controller_payload(rm)

    assert payload["is_trading_allowed"] is False
    assert payload["trading_disabled_reason"] == (
        "EMERGENCY STOP active (operator hit the kill switch)"
    )


def test_a_non_string_reason_never_reaches_the_published_payload():
    """json.dumps must not explode on a mock RiskManager.

    `self.rm` is a real RiskManager in production but a mock in some tests.
    A Mock's `get_trading_disabled_reason` is a callable child mock, so
    calling it returns a Mock -- which json.dumps cannot serialize. The whole
    portfolio payload would then fail to publish, taking the dashboard down
    with it.
    """
    from unittest.mock import MagicMock

    rm = SimpleNamespace(
        stats=SimpleNamespace(
            current_balance=1000.0, today_pnl=0.0, consecutive_losses=0
        ),
        _is_trading_allowed=False,
        get_trading_disabled_reason=MagicMock(return_value=MagicMock()),
    )

    payload = _controller_payload(rm)

    assert payload["trading_disabled_reason"] is None, (
        "a reason that is not a string must never be published"
    )
    # The whole payload must still serialize -- this is what the controller
    # does with it.
    json.dumps(payload)


def test_the_published_payload_survives_a_risk_manager_without_the_accessor():
    """The publish must not explode on a mock RiskManager.

    `self.rm` is a real RiskManager in production but a mock in some tests,
    and a Mock's attribute is truthy.
    """
    from bot_module.controller import TradingController

    controller = TradingController.__new__(TradingController)
    controller.user_id = USER_ID
    controller.rm = SimpleNamespace(
        stats=SimpleNamespace(
            current_balance=1000.0, today_pnl=0.0, consecutive_losses=3
        ),
        _is_trading_allowed=True,
    )

    namespace = _publish_namespace(controller.rm)
    namespace["self"] = controller
    exec(_extract_publish_statements(), namespace)  # noqa: S102 - the repo's own source
    payload = namespace["portfolio_status_data"]

    assert payload["consecutive_losses"] == 3
    assert payload.get("trading_disabled_reason") is None


# ---------------------------------------------------------------------------
# Part 2 -- the PAPER branch reports the real risk state
# ---------------------------------------------------------------------------


def test_paper_branch_reports_the_real_consecutive_loss_count():
    """THE PROOF: a blocked paper account reported "0 consecutive losses"."""
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(
            equity=2645.55, allowed=False, losses=10, reason="Max consec losses (10) >= limit (10)"
        )
    }

    data = _call(mode="paper", payloads=payloads)["data"]

    assert data.consecutive_losses == 10, (
        f"the controller published 10 consecutive losses and the API reported "
        f"{data.consecutive_losses!r} -- the loss counter was a hardcoded 0"
    )


def test_paper_branch_reports_the_reason_trading_is_blocked():
    """The gate's reason must reach the API response."""
    reason = "global risk limit breached: Max consec losses (10) >= limit (10)"
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(
            equity=2645.55, allowed=False, losses=10, reason=reason
        )
    }

    data = _call(mode="paper", payloads=payloads)["data"]

    assert data.is_trading_allowed is False
    assert data.trading_disabled_reason == reason, (
        "the API reported a blocked account without saying why"
    )


def test_paper_branch_reports_no_reason_when_nothing_is_blocked():
    """Nothing blocked -> nothing to report."""
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(equity=2645.55, allowed=True, losses=0)
    }

    data = _call(mode="paper", payloads=payloads)["data"]

    assert data.is_trading_allowed is True
    assert data.trading_disabled_reason is None


def test_paper_branch_reports_the_highest_counter_of_the_paper_controllers():
    """The counter is PER CONTROLLER; the account is blocked if ANY is over."""
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(equity=2645.55, allowed=True, losses=3),
        f"{PORTFOLIO_KEY}:paper-2": _published(equity=100.0, allowed=False, losses=10),
    }

    data = _call(mode="paper", payloads=payloads)["data"]

    assert data.consecutive_losses == 10, (
        f"the worst controller's counter was {10} and the API reported "
        f"{data.consecutive_losses!r}"
    )
    assert data.is_trading_allowed is False


def test_paper_branch_joins_distinct_blocked_reasons():
    """Two blocked controllers, two causes: neither may be lost."""
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(
            equity=2645.55, allowed=False, losses=10, reason="Max consec losses (10) >= limit (10)"
        ),
        f"{PORTFOLIO_KEY}:paper-2": _published(
            equity=100.0, allowed=False, losses=1, reason="EMERGENCY STOP active (operator stop)"
        ),
        f"{PORTFOLIO_KEY}:paper-3": _published(
            equity=100.0, allowed=False, losses=1, reason="Max consec losses (10) >= limit (10)"
        ),
    }

    data = _call(mode="paper", payloads=payloads)["data"]

    assert data.trading_disabled_reason == (
        "Max consec losses (10) >= limit (10); EMERGENCY STOP active (operator stop)"
    ), f"both causes must survive, got {data.trading_disabled_reason!r}"


def test_paper_branch_ignores_a_stale_reason_on_an_allowed_controller():
    """The RiskManager keeps the last refusal reason after the gate re-opens.

    Reporting it next to "trading allowed" would make a healthy account look
    broken.
    """
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(
            equity=2645.55,
            allowed=True,
            losses=0,
            reason="global risk limit breached: Max consec losses (10) >= limit (10)",
        )
    }

    data = _call(mode="paper", payloads=payloads)["data"]

    assert data.is_trading_allowed is True
    assert data.trading_disabled_reason is None, (
        "a controller that is ALLOWED must not report a block reason"
    )


# ---------------------------------------------------------------------------
# Part 3 -- the LIVE Redis fallback is mode-scoped
# ---------------------------------------------------------------------------


def test_live_fallback_excludes_the_paper_account_equity():
    """THE PROOF: ?mode=live reported the paper account's equity.

    `bot_runner.py` starts a paper controller for every user who does not
    have one, so a ":None" key sits next to the live keys for EVERY user
    with API keys. The fallback aggregated it, so the live balance was the
    paper account's equity.
    """
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(equity=2645.55, allowed=True),
        f"{PORTFOLIO_KEY}:1": _published(equity=1000.0, allowed=True),
    }

    data = _call(mode="live", payloads=payloads)["data"]

    assert data.balance == 1000.0, (
        f"the live balance was {data.balance!r} -- the paper controller's "
        "equity was aggregated into the live response"
    )


def test_live_fallback_reports_the_live_consecutive_loss_count():
    """The counter must survive the fallback's aggregation."""
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(equity=2645.55, allowed=True, losses=10),
        f"{PORTFOLIO_KEY}:1": _published(equity=1000.0, allowed=False, losses=10),
    }

    data = _call(mode="live", payloads=payloads)["data"]

    assert data.consecutive_losses == 10, (
        f"the API reported {data.consecutive_losses!r} consecutive losses while "
        "the controller published 10"
    )


def test_live_fallback_reports_the_reason_trading_is_blocked():
    """The gate's reason must reach the API response."""
    reason = "global risk limit breached: Max consec losses (10) >= limit (10)"
    payloads = {
        f"{PORTFOLIO_KEY}:1": _published(
            equity=1000.0, allowed=False, losses=10, reason=reason
        )
    }

    data = _call(mode="live", payloads=payloads)["data"]

    assert data.is_trading_allowed is False
    assert data.trading_disabled_reason == reason


def test_a_paper_counter_never_overrides_the_live_figure():
    """The max is computed WITHIN a mode, never across modes."""
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(equity=2645.55, allowed=True, losses=2),
        f"{PORTFOLIO_KEY}:1": _published(equity=1000.0, allowed=True, losses=7),
    }

    data = _call(mode="live", payloads=payloads)["data"]

    assert data.consecutive_losses == 7, (
        f"the live figure was 7 and the API reported {data.consecutive_losses!r}"
    )


def test_the_counter_and_the_reason_may_describe_different_controllers():
    """Pinned, so the documented asymmetry cannot drift silently.

    Controller `:1` is blocked by the emergency stop at 0 losses; controller
    `:2` is allowed at 9 losses. The counter answers "how close is this
    account to the loss limit" (9); the reason answers "why can this account
    not trade right now" (the emergency stop). Reporting 0 -- the blocked
    controller's counter -- would understate the account's exposure to the
    limit.
    """
    payloads = {
        f"{PORTFOLIO_KEY}:1": _published(
            equity=1000.0,
            allowed=False,
            losses=0,
            reason="EMERGENCY STOP active (operator hit the kill switch)",
        ),
        f"{PORTFOLIO_KEY}:2": _published(equity=1000.0, allowed=True, losses=9),
    }

    data = _call(mode="live", payloads=payloads)["data"]

    assert data.consecutive_losses == 9, (
        "the counter is the MAX across the population -- it measures how close "
        "the account is to the loss limit, not which controller is blocked"
    )
    assert data.is_trading_allowed is False
    assert data.trading_disabled_reason == (
        "EMERGENCY STOP active (operator hit the kill switch)"
    )


def test_live_fallback_reports_no_reason_when_nothing_is_blocked():
    """Nothing blocked -> nothing to report."""
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(equity=2645.55, allowed=True),
        f"{PORTFOLIO_KEY}:1": _published(equity=1000.0, allowed=True),
    }

    data = _call(mode="live", payloads=payloads)["data"]

    assert data.is_trading_allowed is True
    assert data.trading_disabled_reason is None


def test_live_fallback_404s_when_only_a_paper_controller_is_publishing():
    """No live state -> the honest answer is "not in cache", not $0.00.

    The authoritative live balance comes from the exchange, and this fallback
    only runs when that fetch FAILED. A zero balance would misreport the
    user's real money.
    """
    payloads = {
        f"{PORTFOLIO_KEY}:None": _published(equity=2645.55, allowed=False, losses=10)
    }

    with pytest.raises(HTTPException) as excinfo:
        _call(mode="live", payloads=payloads)

    assert excinfo.value.status_code == 404, (
        "a live request with no live controller must 404, not report the "
        "paper account's balance as the live one"
    )


def test_live_fallback_surfaces_the_published_unrealized_pnl():
    """The published payload carries it; the mapping dropped it.

    The value was in hand and was discarded, so a cached portfolio reported
    no open-position PnL at all.
    """
    payloads = {
        f"{PORTFOLIO_KEY}:1": _published(equity=1000.0, allowed=True, unrealized=48.29)
    }

    data = _call(mode="live", payloads=payloads)["data"]

    assert data.total_unrealized_pnl == 48.29, (
        f"the published unrealized PnL was 48.29 and the API reported "
        f"{data.total_unrealized_pnl!r}"
    )


def test_live_fallback_for_a_specific_api_key_surfaces_the_unrealized_pnl():
    """The single-key branch took the WHOLE payload, then dropped the PnL."""
    payloads = {
        f"{PORTFOLIO_KEY}:7": _published(equity=1000.0, allowed=True, unrealized=12.5)
    }

    data = _call(mode="live", payloads=payloads, api_key_id=7)["data"]

    assert data.total_unrealized_pnl == 12.5, (
        f"the published unrealized PnL was 12.5 and the API reported "
        f"{data.total_unrealized_pnl!r}"
    )


def test_live_fallback_for_a_specific_api_key_reports_the_real_counter():
    """The single-key branch inherits the published counter."""
    payloads = {
        f"{PORTFOLIO_KEY}:7": _published(
            equity=1000.0, allowed=False, losses=10, reason="Max consec losses (10) >= limit (10)"
        )
    }

    data = _call(mode="live", payloads=payloads, api_key_id=7)["data"]

    assert data.consecutive_losses == 10
    assert data.is_trading_allowed is False
    assert data.trading_disabled_reason == "Max consec losses (10) >= limit (10)"


# ---------------------------------------------------------------------------
# Part 4 -- the LIVE-EXCHANGE branch reports the real risk state
# ---------------------------------------------------------------------------


def _live_exchange_payloads():
    return {
        f"{PORTFOLIO_KEY}:None": _published(
            equity=2645.55, allowed=True, losses=10, reason="a paper-only reason"
        ),
        f"{PORTFOLIO_KEY}:1": _published(
            equity=1000.0,
            allowed=False,
            losses=4,
            reason="global risk limit breached: Max consec losses (4) >= limit (4)",
        ),
    }


async def _fake_fetch(**kwargs):
    """A stand-in for the exchange call: one account, one market."""
    from api import schemas as api_schemas

    return api_schemas.AccountBalance(
        api_key_id=kwargs["key_obj"].id,
        api_key_name=kwargs["key_obj"].name,
        exchange="binance",
        market_type=kwargs["market_type"],
        balance=1000.0,
        available_balance=1000.0,
        unrealized_pnl=0.0,
        margin_used=0.0,
        total_equity=1000.0,
        assets=[],
    )


def test_live_exchange_branch_reports_the_live_risk_state(monkeypatch):
    """The exchange knows the money; only the controller knows the gate.

    The balances come from the exchange for this user's API keys, so the
    risk state must describe that same population -- the paper controller's
    counter and reason belong to a different account.
    """
    import api.depthsight_api as depthsight_api

    monkeypatch.setattr(depthsight_api, "fetch_api_key_market_balance", _fake_fetch)

    key = SimpleNamespace(id=1, name="key-1", exchange="binance")
    crud = _Crud(active_keys=[key])

    data = _call(
        mode="live",
        payloads=_live_exchange_payloads(),
        crud=crud,
    )["data"]

    assert data.balance == 2000.0, (
        "the exchange balances were not used -- the request fell into the "
        "Redis fallback"
    )
    assert data.consecutive_losses == 4, (
        f"the live controller published 4 consecutive losses and the API "
        f"reported {data.consecutive_losses!r}"
    )
    assert data.trading_disabled_reason == (
        "global risk limit breached: Max consec losses (4) >= limit (4)"
    ), "the paper controller's reason must not be reported for a live account"
    assert data.is_trading_allowed is False


# ---------------------------------------------------------------------------
# Part 5 -- the real controller payload, end to end through the API
# ---------------------------------------------------------------------------


def test_the_published_controller_payload_reaches_the_api_response():
    """The whole chain: RiskManager -> controller payload -> API response."""
    rm = _risk_manager_blocked_at_the_loss_limit()
    payload = _controller_payload(rm)

    redis_payload = {f"{PORTFOLIO_KEY}:1": json.dumps(payload)}

    data = _call(mode="live", payloads=redis_payload)["data"]

    assert data.consecutive_losses == 10, (
        f"the real controller published {payload['consecutive_losses']} and the "
        f"API reported {data.consecutive_losses!r}"
    )
    assert data.is_trading_allowed is False
    assert data.trading_disabled_reason == payload["trading_disabled_reason"]
    assert "consec losses" in data.trading_disabled_reason.lower()
