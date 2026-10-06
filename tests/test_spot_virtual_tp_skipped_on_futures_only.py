"""The spot-only TP path must not run per trade tick on a futures-only bot.

`_check_spot_virtual_tp_triggers` is called from the TICK handler on every
trade print. It hardcodes `market_type="spot"` and then returns False unless
the resolved executor is a spot executor -- so on a deployment with no spot
executor the answer is already decided, but the call still took a symbol lock,
scanned the position map and emitted:

    [_get_executor_for_symbol] Position not found for symbol BTCUSDT
    market=spot. Cannot determine mode.

at WARNING, once per tick, which buried the Critical Events panel.

These tests prove the short-circuit returns the SAME answer the old path did
(so no trading behaviour changes) while never reaching the noisy lookup, and
that it does NOT short-circuit on a real spot deployment.
"""

import ast
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = "bot_module/controller.py"


def _extract_method(name: str, namespace: Dict[str, Any]):
    """Compile a single method out of the controller, by AST.

    Decorators and annotations are stripped; only the body under test runs.
    """
    source = (REPO_ROOT / CONTROLLER).read_text(encoding="utf-8")
    tree = ast.parse(source, CONTROLLER)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            node.decorator_list = []
            node.returns = None
            args = node.args
            for arg in (
                list(args.posonlyargs)
                + list(args.args)
                + list(args.kwonlyargs)
                + [args.vararg, args.kwarg]
            ):
                if arg is not None:
                    arg.annotation = None
            module = ast.Module(body=[node], type_ignores=[])
            exec(compile(module, CONTROLLER, "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in {CONTROLLER}")


class _NullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Executor:
    def __init__(self, market_type):
        self.market_type = market_type


class _FakeController:
    """Just enough surface for the two methods under test."""

    def __init__(self, paper_market, live_market, market_executors=None, resolve=None):
        ns: Dict[str, Any] = {
            "Any": Any,
            "Optional": Optional,
            "List": List,
            "Tuple": Tuple,
            "__name__": "spot_tp_under_test",
        }
        is_spot = _extract_method("_executor_is_spot", ns)
        can_execute_spot = _extract_method("_can_execute_spot", ns)
        check = _extract_method("_check_spot_virtual_tp_triggers", ns)

        self._executor_is_spot = lambda executor: is_spot(executor)
        self._can_execute_spot = lambda: can_execute_spot(self)
        self._check = check.__get__(self)

        self.executors = {
            "paper": _Executor(paper_market) if paper_market else None,
            "live": _Executor(live_market) if live_market else None,
        }
        self.market_executors: Dict[str, Any] = market_executors or {}

        # Records whether the noisy lookup was ever reached.
        self.lookup_calls: List[Tuple[str, Any]] = []
        self.lock_calls: List[Tuple[str, Any]] = []
        self._resolve = resolve or (lambda symbol, market_type: None)

    async def _get_executor_for_symbol(self, symbol, market_type=None):
        self.lookup_calls.append((symbol, market_type))
        return self._resolve(symbol, market_type)

    def _get_lock_for_position(self, symbol, market_type=None):
        self.lock_calls.append((symbol, market_type))
        return _NullLock()

    def _active_position_get(self, symbol, market_type=None):
        return None

    def run(self, **kwargs):
        return asyncio.run(self._check(**kwargs))


# --------------------------------------------------------------------------
# The deployment this product actually runs.
# --------------------------------------------------------------------------


def test_paper_only_futures_account_cannot_execute_spot():
    """PaperTradingExecutor hardcodes market_type='futures_usdtm'."""
    ctrl = _FakeController(paper_market="futures_usdtm", live_market=None)
    assert ctrl._can_execute_spot() is False


def test_futures_only_deployment_skips_the_lookup_entirely():
    """The gate fires BEFORE the symbol lock and the spot lookup."""
    ctrl = _FakeController(paper_market="futures_usdtm", live_market=None)
    assert ctrl.run(symbol="BTCUSDT", last_price=85000.0) is False
    assert ctrl.lookup_calls == [], (
        f"Expected no spot lookup on a futures-only deployment, got {ctrl.lookup_calls}"
    )
    assert ctrl.lock_calls == [], "Expected no symbol lock to be taken per tick."


def test_gate_returns_the_same_answer_the_old_path_returned():
    """Equivalence: gating must not change the RESULT, only the wasted work.

    The old path resolved an executor and bailed on
    `not _executor_is_spot(executor)`. Here the resolver returns exactly what
    the real one would on this deployment -- the futures paper executor --
    and the old branch still yields False.
    """
    futures_paper = _Executor("futures_usdtm")
    ctrl = _FakeController(
        paper_market="futures_usdtm",
        live_market=None,
        resolve=lambda symbol, market_type: futures_paper,
    )
    # Simulate the pre-gate path by letting the gate pass, then confirming the
    # executor check produces the same answer the gate short-circuits to.
    ctrl._can_execute_spot = lambda: True
    ungated_result = ctrl.run(symbol="BTCUSDT", last_price=85000.0)
    assert ungated_result is False
    assert ctrl.lookup_calls == [("BTCUSDT", "spot")]

    gated = _FakeController(
        paper_market="futures_usdtm",
        live_market=None,
        resolve=lambda symbol, market_type: futures_paper,
    )
    assert gated.run(symbol="BTCUSDT", last_price=85000.0) is False
    assert gated.lookup_calls == []
    assert ungated_result == gated.run(symbol="BTCUSDT", last_price=85000.0)


# --------------------------------------------------------------------------
# Spot deployments must be completely unaffected.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "paper_market,live_market,market_executors",
    [
        ("spot", None, None),
        (None, "spot", None),
        ("futures_usdtm", "futures_usdtm", {"spot": _Executor("spot")}),
    ],
)
def test_spot_deployments_are_not_short_circuited(
    paper_market, live_market, market_executors
):
    ctrl = _FakeController(
        paper_market=paper_market,
        live_market=live_market,
        market_executors=market_executors,
    )
    assert ctrl._can_execute_spot() is True
    ctrl.run(symbol="BTCUSDT", last_price=85000.0)
    assert ctrl.lookup_calls == [("BTCUSDT", "spot")], (
        "A spot deployment must still perform the spot lookup -- the gate is "
        "only allowed to fire when no spot executor exists."
    )


def test_futures_market_executor_alone_does_not_enable_spot():
    """A futures executor in market_executors must not enable the spot path."""
    ctrl = _FakeController(
        paper_market="futures_usdtm",
        live_market="futures_usdtm",
        market_executors={"futures_usdtm": _Executor("futures_usdtm")},
    )
    assert ctrl._can_execute_spot() is False


def test_data_consumer_only_spot_executor_does_not_enable_spot():
    """THE REGRESSION THAT MATTERS.

    bot_runner builds `market_executors_for_data_consumer` with BOTH
    'futures_usdtm' and 'spot' for every paper-only controller, so a "spot"
    key is always present there. Those executors are market-data-only --
    created with empty credentials -- and cannot open a spot position.

    A gate that trusted market_executors would return True here, never fire,
    and the per-tick flood would continue unchanged. That is exactly what the
    first version did, and deploying it proved the flood still running.
    """
    data_only = {
        "futures_usdtm": _Executor("futures_usdtm"),
        "spot": _Executor("spot"),
    }
    ctrl = _FakeController(
        paper_market="futures_usdtm",
        live_market=None,
        market_executors=data_only,
    )
    assert ctrl._can_execute_spot() is False, (
        "A paper-only controller's data-consumer spot executor must not count "
        "as tradeable spot."
    )
    ctrl.run(symbol="BTCUSDT", last_price=85000.0)
    assert ctrl.lookup_calls == []
    assert ctrl.lock_calls == []
