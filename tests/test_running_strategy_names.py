"""Running strategies must display their saved config name, not the class name.

The bot publishes, per running strategy:
    id             = config_id          (controller.py: `"id": config_id`)
    strategy_name  = instance.NAME     (the strategy CLASS, e.g. "VisualBuilderStrategy")
    name           = <never published>  -> Pydantic defaults it to None

So `GET /api/v1/strategies` returned `name: null` and `strategy_name:
"VisualBuilderStrategy"` for EVERY strategy. The dashboard's Top Performing
table renders `strategy.name || strategy.strategy_name`, so it showed N
identical rows -- while the real names ("EMA 50/200 Golden Cross", "RSI
Breakout v2", ...) sat unused in StrategyConfig.

These tests drive the real route, extracted by AST, with the REAL Redis payload
shape the bot publishes.
"""

import ast
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTES = "api/routes/strategies.py"


def _Depends(dependency=None):  # noqa: N802
    return None


def _Query(default=None, *args, **kwargs):  # noqa: N802
    return default


def _extract_list_strategies():
    source = (REPO_ROOT / ROUTES).read_text(encoding="utf-8")
    tree = ast.parse(source, ROUTES)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "list_strategies":
            node.decorator_list = []
            node.returns = None
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                arg.annotation = None
            module = ast.Module(body=[node], type_ignores=[])
            fn_globals: Dict[str, Any] = {
                "__name__": "list_strategies_under_test",
                "Depends": _Depends,
                "Query": _Query,
                "List": List,
                "get_redis_client": lambda: None,
                "get_current_user": lambda: None,
                "get_db": lambda: None,
            }
            exec(compile(module, ROUTES, "exec"), fn_globals)
            return fn_globals["list_strategies"]
    raise AssertionError("list_strategies not found")


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Schemas:
    """Mirrors StrategyInfo closely enough to observe the joined name."""

    class StrategyInfo:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.__dict__.setdefault("name", None)

    class ApiResponseData:
        def __init__(self, **kw):
            self.__dict__.update(kw)


class _Redis:
    def __init__(self, payloads: Dict[str, str]):
        self.payloads = payloads

    async def get(self, key):
        return self.payloads.get(key)

    async def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.payloads if k.startswith(prefix)]

    async def mget(self, keys):
        return [self.payloads.get(k) for k in keys]


def _published(*, strategy_id, status="running"):
    """EXACT shape the bot publishes in controller.py strat_data."""
    return {
        "id": strategy_id,
        "strategy_name": "VisualBuilderStrategy",
        "symbol": "BTCUSDT",
        "market_type": "futures",
        "status": status,
        "pnl": 0.0,
        "open_positions": 0,
        "started_at": "2026-10-06T15:58:38.090088Z",
        "params": {},
        "user_id": 10,
        "api_key_id": None,
        "symbol_selection_mode": "STATIC",
        "mode": "paper",
    }


def _call(payloads, config_names, db_fails=False):
    endpoint = _extract_list_strategies()
    import api.routes.strategies as real_module

    async def get_configs(db, user_id):
        if db_fails:
            raise RuntimeError("db down")
        return [
            SimpleNamespace(id=cfg_id, name=name) for cfg_id, name in config_names.items()
        ]

    g = dict(real_module.__dict__)
    g["schemas"] = _Schemas
    g["logger"] = _Logger()
    g["crud"] = SimpleNamespace(get_strategy_configs_by_user=get_configs)
    g["bot_config"] = SimpleNamespace(REDIS_STATE_KEY_STRATEGIES="depthsight:state:strategies")

    fn = endpoint
    fn.__globals__.clear()
    fn.__globals__.update(g)

    async def main():
        return await fn(
            redis_client=_Redis(payloads),
            current_user=SimpleNamespace(id=10, username="alex_trader"),
            db=object(),
            mode="paper",
            api_key_id=None,
        )

    return asyncio.run(main())


KEY = "depthsight:state:strategies:10:None"


def test_running_strategies_get_their_real_config_names():
    payloads = {
        KEY: json.dumps(
            [
                _published(strategy_id="aaa"),
                _published(strategy_id="bbb", status="in_position"),
            ]
        )
    }
    data = _call(
        payloads,
        {"aaa": "EMA 50/200 Golden Cross", "bbb": "RSI Breakout v2"},
    )["data"]

    assert [s.name for s in data] == ["EMA 50/200 Golden Cross", "RSI Breakout v2"], (
        "Running strategies must surface their saved config name; the dashboard "
        f"renders `name || strategy_name` and strategy_name is the CLASS name. Got {[s.name for s in data]!r}"
    )


def test_strategy_name_still_available_as_the_class_name():
    payloads = {KEY: json.dumps([_published(strategy_id="aaa")])}
    data = _call(payloads, {"aaa": "My Strategy"})["data"]
    assert data[0].strategy_name == "VisualBuilderStrategy"
    assert data[0].name == "My Strategy"


def test_unnamed_config_falls_back_without_raising():
    """A strategy with no matching config keeps name=None (frontend falls back)."""
    payloads = {KEY: json.dumps([_published(strategy_id="zzz")])}
    data = _call(payloads, {})["data"]
    assert data[0].name is None
    assert data[0].strategy_name == "VisualBuilderStrategy"


def test_db_failure_does_not_break_the_running_strategy_list():
    """Redis-backed list must survive a DB failure -- names are best-effort."""
    payloads = {KEY: json.dumps([_published(strategy_id="aaa")])}
    data = _call(payloads, {"aaa": "Should Not Matter"}, db_fails=True)["data"]
    assert len(data) == 1
    assert data[0].id == "aaa"


def test_config_with_empty_name_is_not_used():
    payloads = {KEY: json.dumps([_published(strategy_id="aaa")])}
    data = _call(payloads, {"aaa": ""})["data"]
    assert data[0].name is None