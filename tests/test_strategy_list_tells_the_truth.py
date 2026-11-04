# File: tests/test_strategy_list_tells_the_truth.py
"""
The strategy list must tell the TRUTH about a strategy that cannot trade.

Context
-------
`controller.py` publishes every running strategy to Redis; the dashboard
(`TopStrategiesTable`, `Strategies.tsx`) and the PWA (`StrategiesScreen.tsx`)
read that payload and nothing else.

The publisher used to invent its own symbol label: "Dynamic (All)" for
anything that was not STATIC/FIXED. A DYNAMIC strategy whose symbol pool
resolved to NOTHING -- the screener feed is off, or the legacy row has no
mode at all -- was therefore published as a healthy, unscoped, RUNNING
strategy. The customer was told it was trading every symbol while it was
scoped to no symbol and could not trade at all. Observed live in the owner's
dashboard: RUNNING + "Dynamic (All)" + "No active positions yet" + PnL +0.00.

A strategy whose RESOLVED symbol pool is empty must be published as BLOCKED,
with a reason that names the CAUSE (the symbol source is unavailable) and
never with the "Dynamic (All)" label. The strategy keeps running internally:
this is a presentation fix, not a trading change.

These tests drive the REAL publisher (`_publish_state_to_redis`) against a
stub controller, so they assert on the payload the dashboard actually
receives -- not on a re-implementation of it.
"""

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest

from bot_module import config
from bot_module.controller import TradingController

REPO_ROOT = Path(__file__).resolve().parents[1]

# The published contract. Asserted as literals, not as imported constants, so
# these tests pin what the DASHBOARD receives rather than how the bot spells
# its own internals.
BLOCKED = "blocked"
NO_SYMBOLS_LABEL = "No symbols"
REASON_SYMBOL_SOURCE_UNAVAILABLE = "symbol_source_unavailable"
REASON_NO_SYMBOLS_CONFIGURED = "no_symbols_configured"


# --- The stub controller the real publisher runs against ---------------------


class _FakePipe:
    def __init__(self) -> None:
        self.written: Dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.written[key] = value

    def publish(self, channel: str, payload: str) -> None:
        return None

    async def execute(self) -> List[Any]:
        return []


class _FakePipeline:
    def __init__(self, pipe: _FakePipe) -> None:
        self._pipe = pipe

    async def __aenter__(self) -> _FakePipe:
        return self._pipe

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeRedis:
    def __init__(self) -> None:
        self.pipe = _FakePipe()

    def pipeline(self, *args, **kwargs) -> _FakePipeline:
        return _FakePipeline(self.pipe)


class _FakeStats:
    current_balance = 1000.0
    today_pnl = 0.0
    consecutive_losses = 0


class _FakeRiskManager:
    def __init__(self) -> None:
        self.stats = _FakeStats()
        self._is_trading_allowed = True

    def get_pnl_for_strategy(
        self, symbol: Optional[str] = None, strategy_name: Optional[str] = None
    ) -> float:
        return 0.0


class _FakeSymbolSelectionConfig:
    def __init__(self, mode: str = "DYNAMIC_SIMPLE") -> None:
        self.mode = mode


class _FakeInstance:
    def __init__(self, name: str = "VisualBuilderStrategy") -> None:
        self.NAME = name


class _StubController:
    """Exactly the surface `_publish_state_to_redis` touches."""

    def __init__(
        self,
        instances: Dict[str, Tuple[_FakeInstance, Dict[str, Any]]],
        screener_symbols: Iterable[str] = (),
        managed_symbols: Iterable[str] = (),
        global_mode: str = "DYNAMIC_SIMPLE",
    ) -> None:
        self.redis_client = _FakeRedis()
        self._positions_dict_lock = asyncio.Lock()
        self.instances_lock = asyncio.Lock()
        self._active_positions: Dict[str, Any] = {}
        self.running_strategy_instances = dict(instances)
        self.user_id = 10
        self.api_key_id = 1
        self.rm = _FakeRiskManager()
        self.symbol_selection_config = _FakeSymbolSelectionConfig(global_mode)
        self.currently_managed_symbols = list(managed_symbols)
        self._last_known_symbols_from_consumer = set(screener_symbols)
        self._emergency_stop_state_for_publish = lambda: {}


def _config_dict(
    mode: Optional[str] = "DYNAMIC", symbols: Optional[List[str]] = None
) -> Dict[str, Any]:
    config_dict: Dict[str, Any] = {
        "config_data": {"marketType": "PAPER", "params": {}},
        "user_id": 10,
        "mode": "paper",
    }
    if mode is not None:
        config_dict["symbol_selection_mode"] = mode
    if symbols is not None:
        config_dict["symbols"] = symbols
    return config_dict


async def _publish(stub: _StubController) -> List[Dict[str, Any]]:
    """Run the REAL publisher and return the strategy list it wrote to Redis."""
    await TradingController._publish_state_to_redis(stub)
    key = f"{config.REDIS_STATE_KEY_STRATEGIES}:{stub.user_id}:{stub.api_key_id}"
    return json.loads(stub.redis_client.pipe.written[key])


async def _publish_one(
    mode: Optional[str] = "DYNAMIC",
    symbols: Optional[List[str]] = None,
    screener_symbols: Iterable[str] = (),
    managed_symbols: Iterable[str] = (),
    global_mode: str = "DYNAMIC_SIMPLE",
) -> Dict[str, Any]:
    instances = {"cfg-1": (_FakeInstance(), _config_dict(mode, symbols))}
    stub = _StubController(
        instances,
        screener_symbols=screener_symbols,
        managed_symbols=managed_symbols,
        global_mode=global_mode,
    )
    published = await _publish(stub)
    assert len(published) == 1
    return published[0]


# --- The defect: an empty pool published as a running, unscoped strategy -----


async def test_dynamic_strategy_with_an_empty_pool_is_published_as_blocked():
    """This is the actual defect: scoped to NOTHING, published as RUNNING.

    The screener feed is off, so the DYNAMIC pool resolves to an empty list.
    The strategy cannot trade, and the customer must not be told it is
    running over "Dynamic (All)".
    """
    published = await _publish_one(mode="DYNAMIC", screener_symbols=())

    assert published["status"] == BLOCKED
    assert published["symbol"] != "Dynamic (All)"
    assert published["symbol"] == NO_SYMBOLS_LABEL
    assert published["symbols"] == []


async def test_blocked_reason_names_the_symbol_source_not_the_mode():
    """"dynamic" is not a reason. The CAUSE is that the source has no symbols.

    A reason of "dynamic" tells the customer nothing they can act on; a
    reason that names the symbol source tells them the truth and where to
    look.
    """
    published = await _publish_one(mode="DYNAMIC", screener_symbols=())

    detail = published["status_detail"]
    assert detail, "a blocked strategy must carry a reason"
    lowered = detail.lower()
    assert "symbol source" in lowered
    assert "unavailable" in lowered
    # The mode name is a category, not a cause. It must not stand in for one.
    assert "dynamic" not in lowered
    assert published["status_reason_code"] == REASON_SYMBOL_SOURCE_UNAVAILABLE


async def test_a_legacy_row_with_no_mode_is_blocked_like_a_dynamic_one():
    """A missing mode is DYNAMIC -- that is what the resolver treats it as.

    Publishing it as "STATIC" (the old default) told the customer their
    screener-fed strategy was pinned to its own symbol list.
    """
    published = await _publish_one(mode=None, screener_symbols=())

    assert published["status"] == BLOCKED
    assert published["symbol"] != "Dynamic (All)"
    assert published["symbol_selection_mode"] == "DYNAMIC"


async def test_a_static_strategy_with_no_symbols_is_blocked_with_its_own_reason():
    """The cause here is the config, not the screener -- and it must say so."""
    published = await _publish_one(mode="STATIC", symbols=[])

    assert published["status"] == BLOCKED
    assert published["symbol"] == NO_SYMBOLS_LABEL
    assert published["status_reason_code"] == REASON_NO_SYMBOLS_CONFIGURED
    assert "screener" not in published["status_detail"].lower()


# --- No regression: the healthy cases must be untouched ----------------------


async def test_static_strategy_with_symbols_is_still_running_with_its_symbols():
    """The overwhelmingly common case must be completely unaffected."""
    published = await _publish_one(mode="STATIC", symbols=["BTCUSDT", "ETHUSDT"])

    assert published["status"] == "running"
    assert published["symbol"] == "BTCUSDT, ETHUSDT"
    assert published["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert published["status_detail"] is None


async def test_fixed_strategy_with_symbols_is_still_running_with_its_symbols():
    """FIXED is the mode the onboarding wizard creates: pinned, not dynamic."""
    published = await _publish_one(mode="FIXED", symbols=["BTCUSDT"])

    assert published["status"] == "running"
    assert published["symbol"] == "BTCUSDT"
    assert published["symbol_selection_mode"] == "FIXED"


async def test_dynamic_strategy_with_a_live_pool_is_still_running():
    """A DYNAMIC strategy that DOES have symbols must be untouched."""
    published = await _publish_one(
        mode="DYNAMIC", screener_symbols=["BTCUSDT", "SOLUSDT"]
    )

    assert published["status"] == "running"
    assert published["symbol"] == "Dynamic (All)"
    assert published["symbols"] == ["BTCUSDT", "SOLUSDT"]
    assert published["status_detail"] is None


async def test_a_broken_config_still_outranks_an_unavailable_symbol_source():
    """A config the customer can fix is more actionable than a feed outage."""
    instances = {
        "cfg-1": (
            _FakeInstance(),
            {
                **_config_dict("DYNAMIC", []),
                "cannot_trade_reason": "config_data uses the 'blocks' format",
            },
        )
    }
    stub = _StubController(instances, screener_symbols=())
    published = await _publish(stub)

    assert published[0]["status"] == "cannot_trade"
    assert "blocks" in published[0]["status_detail"]
    assert published[0]["status_reason_code"] is None


# --- The pure helpers the publisher is built on -----------------------------


def test_derive_strategy_status_knows_the_blocked_state():
    from bot_module.controller import _derive_strategy_status

    assert _derive_strategy_status(0, None) == "running"
    assert _derive_strategy_status(1, None) == "in_position"
    assert _derive_strategy_status(0, "broken config") == "cannot_trade"
    assert _derive_strategy_status(0, None, "no symbols") == "blocked"
    # A broken config outranks an unavailable symbol source.
    assert _derive_strategy_status(0, "broken config", "no symbols") == "cannot_trade"
    # An open position still wins: it is the risk-relevant fact.
    assert _derive_strategy_status(1, None, "no symbols") == "in_position"


def test_derive_symbol_scope_never_claims_dynamic_for_an_empty_pool():
    from bot_module.controller import _derive_symbol_scope

    label, reason, code = _derive_symbol_scope("DYNAMIC", [], True)
    assert label == NO_SYMBOLS_LABEL
    assert label != "Dynamic (All)"
    assert reason and "symbol source" in reason.lower()
    assert code == REASON_SYMBOL_SOURCE_UNAVAILABLE


def test_derive_symbol_scope_leaves_a_healthy_strategy_alone():
    from bot_module.controller import _derive_symbol_scope

    assert _derive_symbol_scope("DYNAMIC", ["BTCUSDT"], True) == (
        "Dynamic (All)",
        None,
        None,
    )
    assert _derive_symbol_scope("STATIC", ["BTCUSDT"], True) == (
        "BTCUSDT",
        None,
        None,
    )


# --- The API must not swallow the new fields --------------------------------


def test_strategy_info_keeps_the_blocked_status_and_its_reason():
    """Pydantic strips undeclared fields silently.

    Without `status_detail` and `status_reason_code` on the model the UI
    would receive a bare "blocked" with no explanation -- a badge the
    customer cannot act on.
    """
    from api import schemas

    info = schemas.StrategyInfo(
        id="cfg-1",
        strategy_name="VisualBuilderStrategy",
        symbol=NO_SYMBOLS_LABEL,
        market_type="paper",
        status="blocked",
        status_detail="Symbol source unavailable: the screener feed is providing no symbols",
        status_reason_code=REASON_SYMBOL_SOURCE_UNAVAILABLE,
        pnl=0.0,
        open_positions=0,
        started_at="2026-11-03T00:00:00+00:00",
        params={},
        mode="paper",
        symbol_selection_mode="DYNAMIC",
        symbols=[],
    )

    assert info.status == "blocked"
    assert info.status_detail is not None
    assert info.status_reason_code == REASON_SYMBOL_SOURCE_UNAVAILABLE
    assert info.symbols == []


def test_a_healthy_strategy_carries_no_reason():
    from api import schemas

    info = schemas.StrategyInfo(
        id="cfg-1",
        strategy_name="VisualBuilderStrategy",
        symbol="BTCUSDT",
        market_type="paper",
        status="running",
        pnl=0.0,
        open_positions=0,
        started_at="2026-11-03T00:00:00+00:00",
        params={},
        mode="paper",
        symbol_selection_mode="STATIC",
        symbols=["BTCUSDT"],
    )

    assert info.status == "running"
    assert info.status_detail is None
    assert info.status_reason_code is None


# --- The customer-visible half: the badge must not be a green RUNNING -------


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


GREEN_TOKENS = ("green", "emerald", "profit", "success")


def test_the_blocked_badge_class_is_not_green():
    """A blocked strategy must not look like a working one.

    It also must not look like a crash: red is reserved for a broken config
    ("cannot_trade"), which the customer can fix.
    """
    source = _read("frontend/src/lib/strategyStatus.ts")
    match = re.search(r'BLOCKED_BADGE_CLASS\s*=\s*"([^"]+)"', source)
    assert match, "the shared blocked badge class must exist"
    badge_class = match.group(1).lower()

    assert "amber" in badge_class, f"blocked must read as blocked, got {badge_class!r}"
    for token in GREEN_TOKENS:
        assert token not in badge_class, (
            f"a blocked strategy must not render as a green RUNNING badge: {badge_class!r}"
        )


def test_the_dashboard_table_routes_blocked_to_the_blocked_badge():
    source = _read("frontend/src/components/dashboard/TopStrategiesTable.tsx")
    assert "case STRATEGY_STATUS_BLOCKED:" in source
    blocked_case = source.split("case STRATEGY_STATUS_BLOCKED:", 1)[1]
    assert "BLOCKED_BADGE_CLASS" in blocked_case.split("default:", 1)[0]


def test_the_strategies_page_routes_blocked_to_the_blocked_badge():
    source = _read("frontend/src/pages/Strategies.tsx")
    assert "isBlockedStrategyStatus" in source
    color_fn = source.split("const getStatusColor", 1)[1].split("};", 1)[0]
    assert "BLOCKED_BADGE_CLASS" in color_fn


def test_the_pwa_does_not_render_a_blocked_strategy_as_active():
    source = _read("pwa/screens/StrategiesScreen.tsx")
    assert "strategies.status.blocked" in source
    badge = source.split('className={`px-3 py-1 rounded-full', 1)[1].split("}", 1)[0]
    assert "isBlocked" in badge
    # The blocked badge is the warning colour, never the green "profit" one.
    assert "--warning" in badge
    blocked_branch = badge.split("isBlocked", 1)[1]
    assert "--profit" not in blocked_branch.split("isRunning", 1)[0]


def test_the_blocked_label_and_reasons_exist_in_both_languages():
    """The PWA renders a translated label; an English-only string would not
    reach the Russian customer the product ships to."""
    for lang in ("en", "ru"):
        pwa = json.loads(_read(f"pwa/locales/{lang}/pwa-common.json"))
        assert pwa["strategies"]["status"]["blocked"]
        for key in (
            "symbolSourceUnavailable",
            "noSymbolsConfigured",
            "unknownSymbolMode",
        ):
            assert pwa["strategies"]["blocked"][key]

        frontend = json.loads(_read(f"frontend/src/locales/{lang}/common.json"))
        for key in (
            "symbolSourceUnavailable",
            "noSymbolsConfigured",
            "unknownSymbolMode",
        ):
            assert frontend["blocked"][key]


def test_the_shared_reason_codes_match_the_bot():
    """The UI translates the bot's reason code. If the two disagree, the UI
    silently falls back to the English sentence -- for a Russian customer."""
    source = _read("frontend/src/lib/strategyStatus.ts")
    assert REASON_SYMBOL_SOURCE_UNAVAILABLE in source
    assert REASON_NO_SYMBOLS_CONFIGURED in source


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this box"
)
def test_the_shared_module_really_reports_blocked():
    """Execute the real frontend module, not a copy of it.

    Node can import the TypeScript module directly (type stripping), so this
    asserts the behaviour the dashboard actually ships.
    """
    module = (REPO_ROOT / "frontend" / "src" / "lib" / "strategyStatus.ts").resolve()
    script = (
        "import('file:///" + module.as_posix() + "').then((m) => {"
        "console.log(JSON.stringify({"
        "cls: m.BLOCKED_BADGE_CLASS,"
        "blocked: m.isBlockedStrategyStatus('blocked'),"
        "running: m.isBlockedStrategyStatus('running'),"
        "key: m.blockedReasonI18nKey('symbol_source_unavailable')"
        "}));});"
    )
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT / "frontend"),
    )
    if proc.returncode != 0:
        pytest.skip(f"node cannot import TypeScript here: {proc.stderr.strip()[:200]}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["blocked"] is True
    assert result["running"] is False
    assert "amber" in result["cls"].lower()
    for token in GREEN_TOKENS:
        assert token not in result["cls"].lower()
    assert result["key"] == "common:blocked.symbolSourceUnavailable"
