# tests/test_seeded_templates_are_tradeable.py
"""
A seeded template that can never fire is worse than no template at all: the
user creates a strategy from it, the dashboard says "running", and nothing
ever trades. That is exactly the bug this file exists to prevent.

The 7 built-in templates used to ship a "blocks" array that
bot_module/strategy.py never read. The engine only reads "entryConditions",
"filters" and "initialization", so every strategy created from a seed had a
None entry root and logged "No entry conditions defined, no signal." forever.

These tests therefore drive the REAL engine (VisualBuilderStrategy via
check_signal_sync) with the REAL seeded config_data and assert that each
template can actually produce a StrategySignal when its conditions are met.

Shape assertions are not enough -- a config can look perfectly well-formed and
still be unsatisfiable (that is how the 50/200 EMA on 4h seed shipped). So the
tests below assert OUTCOMES.
"""

import numpy as np
import pandas as pd
import pytest

from api import crud
from bot_module import strategy as strategy_module
from bot_module.strategy import StrategySignal, VisualBuilderStrategy
from bot_module.strategy import SignalDirection


# --------------------------------------------------------------------------
# The real seeded templates, read from the production source (not re-typed
# here, so this test cannot drift away from what actually ships).
# --------------------------------------------------------------------------
def _load_seeded_templates():
    """
    Return the `templates` list the seeder actually builds.

    The literals contain a helper call (_open_long(...)), so ast.literal_eval
    cannot be used. Instead we execute the function's own source with a stub
    `self` and a stub db/coroutine layer, and read `templates` out of the
    closure-free namespace before the loop starts. Executing the real source
    (rather than re-typing the dicts here) is what keeps this test from
    drifting away from what actually ships.
    """
    import ast
    import inspect

    # inspect.getsource returns the raw (still-indented) source, so parse the
    # real module and pick the function node by name.
    mod = ast.parse(inspect.getsource(crud))
    fn = next(
        n
        for n in ast.walk(mod)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "seed_default_strategy_templates"
    )

    # Keep only the prologue: the docstring, SEED_VERSION, the _open_long
    # helper and the `templates = [...]` assignment.
    keep = []
    for stmt in fn.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            keep.append(stmt)  # docstring
            continue
        if isinstance(stmt, ast.Assign):
            target = stmt.targets[0]
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name in ("SEED_VERSION", "templates"):
                keep.append(stmt)
                if name == "templates":
                    break
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "_open_long":
            keep.append(stmt)

    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<seed>", "exec"), ns)
    return ns["templates"]


SEED_TEMPLATES = {t["slug"]: t for t in _load_seeded_templates()}

# Blank Canvas is *meant* to be empty -- it is the "build it yourself" canvas.
# It is exempt from the has-conditions rule, but not from the "must be a
# config the engine can load" rule.
EMPTY_BY_DESIGN = {"blank-canvas"}


@pytest.fixture(autouse=True)
def _register_visual(monkeypatch):
    monkeypatch.setitem(
        strategy_module.STRATEGIES, "VisualBuilderStrategy", VisualBuilderStrategy
    )
    monkeypatch.setattr(strategy_module.config, "MIN_TOTAL_FOUNDATION_WEIGHT_THRESHOLD", 0.0)


# --------------------------------------------------------------------------
# Market / pair fixtures (mirrors tests/test_visual_strategy.py)
# --------------------------------------------------------------------------
def _klines(n=300, base=100.0, seed=11):
    rng = np.random.default_rng(seed)
    now = pd.Timestamp("2024-01-10 12:00:00", tz="UTC")
    index = pd.to_datetime(
        [now - pd.Timedelta(minutes=i) for i in range(n - 1, -1, -1)]
    )
    df = pd.DataFrame(
        {
            "open": rng.uniform(base - 1, base, n),
            "high": rng.uniform(base, base + 1, n),
            "low": rng.uniform(base - 2, base - 1, n),
            "close": rng.uniform(base - 0.5, base + 0.5, n),
            "volume": rng.uniform(100, 200, n),
        },
        index=index,
    )
    return df


def _market_data():
    df = _klines()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    trades = pd.DataFrame(
        {"price": np.full(100, 100.0), "quantity": np.full(100, 1.0)},
        index=pd.date_range(end=df.index[-1], periods=100, freq="500ms", tz="UTC"),
    )
    return {
        "kline_1m": df.copy(),
        "kline_5m": df.resample("5min").agg(agg).dropna(),
        "kline_15m": df.resample("15min").agg(agg).dropna(),
        "kline_1h": df.resample("1h").agg(agg).dropna(),
        "kline_4h": df.resample("4h").agg(agg).dropna(),
        "depth_trading": {"bids": [], "asks": []},
        "aggTrade": trades,
    }


def _pair_info(**over):
    info = {
        "symbol": "BTCUSDT",
        "natr": 2.0,
        "relative_volume": 3.0,
        "atr": 1.0,
        "tick_size": 0.01,
        "last_price": 100.0,
        "open": 99.8,
        "high": 100.3,
        "low": 99.6,
        "close": 100.0,
        "current_candle_index": 59,
        "candle_timeframe": "1m",
        "RSI_14": 50,
        "ADX_14": 20.0,
        "BBL_20_2.0": 95.0,
        "BBU_20_2.0": 105.0,
        "BBB_20_2.0": 10.0,
    }
    info.update(over)
    return info


def _instance(cfg):
    params = {"config": dict(cfg), "enabled": True}
    inst = strategy_module.create_strategy_instance(
        strategy_name="VisualBuilderStrategy", params=params
    )
    assert inst is not None
    return inst


def _leaf_nodes(node, acc=None):
    """
    Return the real leaf (checker) nodes of a condition tree.

    Logic gates (AND/OR) are not leaves even when they have no children -- an
    empty AND root is a legitimate no-op gate, not an unresolvable node type.
    """
    acc = [] if acc is None else acc
    if not isinstance(node, dict):
        return acc
    if node.get("type") in ("AND", "OR"):
        for k in node.get("children") or []:
            _leaf_nodes(k, acc)
        return acc
    acc.append(node)
    return acc


# ==========================================================================
# 1. Schema: the engine's format, not the orphan "blocks" format
# ==========================================================================
def test_no_seeded_template_uses_the_orphan_blocks_key():
    """Regression: "blocks" is never read by the engine."""
    offenders = [s for s, t in SEED_TEMPLATES.items() if "blocks" in t["config_data"]]
    assert not offenders, f"templates still using unread 'blocks': {offenders}"


def test_every_seed_declares_the_keys_the_engine_reads():
    for slug, tpl in SEED_TEMPLATES.items():
        cfg = tpl["config_data"]
        for key in ("entryConditions", "filters"):
            assert key in cfg, f"{slug} is missing '{key}'"
        # An initialization block is what turns a passing gate into a trade, so
        # every template that is MEANT to trade must have one. Blank Canvas is
        # the exception: an empty gate plus an initialization block signals on
        # every candle, so shipping one would make it a position-opening
        # machine rather than a blank canvas.
        if slug not in EMPTY_BY_DESIGN:
            assert "initialization" in cfg, (
                f"{slug} has entry conditions but no 'initialization' block, so "
                f"a passing gate can never open a position"
            )


def test_seeds_declare_conditions_unless_empty_by_design():
    for slug, tpl in SEED_TEMPLATES.items():
        if slug in EMPTY_BY_DESIGN:
            continue
        root = tpl["config_data"].get("entryConditions") or {}
        assert _leaf_nodes(root), f"{slug} has no entry conditions -- it can never fire"


def test_every_leaf_node_type_has_a_registered_checker():
    """A node type with no checker is a no-op that silently passes the AND."""
    inst = _instance({"entryConditions": {"id": "e", "type": "AND", "children": []}})
    known = set(inst.condition_checkers.keys())

    for slug, tpl in SEED_TEMPLATES.items():
        cfg = tpl["config_data"]
        for root_key in ("filters", "entryConditions"):
            for leaf in _leaf_nodes(cfg.get(root_key)):
                assert leaf.get("type") in known, (
                    f"{slug}: node type {leaf.get('type')!r} has no registered checker "
                    f"-> it is a silent no-op"
                )


def test_seed_version_was_bumped_so_existing_rows_refresh():
    """SEED_VERSION gates the in-place refresh of already-seeded rows."""
    import inspect
    import re

    src = inspect.getsource(crud.seed_default_strategy_templates)
    m = re.search(r"SEED_VERSION\s*=\s*(\d+)", src)
    assert m, "SEED_VERSION not found"
    assert int(m.group(1)) >= 2, (
        "SEED_VERSION must be >= 2 so already-seeded rows get refreshed to the "
        "entryConditions format"
    )


# ==========================================================================
# 4. The empty-gate trap: vacuously TRUE, not silently dead
# ==========================================================================
def test_an_empty_entry_gate_passes_vacuously_and_would_trade_every_bar():
    """
    Documents WHY blank-canvas must not ship an initialization block.

    An AND node with no children is vacuously True, and the engine treats a
    present entryConditions root as "gate satisfied". So empty-gate +
    initialization == a signal on every single candle. This is the opposite
    failure of a missing key (which never signals), and it is the more
    dangerous one: it burns the account.
    """
    open_pos = {
        "id": "open_position",
        "type": "open_position",
        "params": {
            "direction": "LONG",
            "sl_type": "atr_multiplier",
            "sl_value": 1.5,
            "tp_type": "rr_multiplier",
            "tp_value": 2.0,
        },
    }
    empty_gate_cfg = {
        "filters": {"id": "f_root", "type": "AND", "children": []},
        "entryConditions": {"id": "e_root", "type": "AND", "children": []},
        "initialization": open_pos,
    }

    inst = _instance(empty_gate_cfg)
    md, pi = _market_data(), _pair_info()

    fired = 0
    runs = 5
    for _ in range(runs):
        signal, _, _ = inst.check_signal_sync(dict(pi), md, None)
        if isinstance(signal, StrategySignal):
            fired += 1

    assert fired == runs, (
        f"expected the empty gate to pass every time (that is the hazard), "
        f"got {fired}/{runs}"
    )


def test_controller_loud_guard_covers_absent_and_empty_gates():
    """
    The 'NO ENTRY CONDITIONS' error must fire for BOTH unusable shapes:
    a missing key (never signals) and a childless root (always signals).
    `{"children": []}` is truthy, so a plain `not cfg.get(...)` misses it.
    """
    import ast
    import inspect

    from bot_module import controller as controller_module

    mod = ast.parse(inspect.getsource(controller_module))
    fn = next(
        n
        for n in ast.walk(mod)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_handle_start_strategy_command"
    )
    body = ast.get_source_segment(inspect.getsource(controller_module), fn) or ""

    assert "_count_leaves" in body, (
        "the guard must count condition leaves, not just test key presence"
    )
    assert "_has_entry" in body, (
        "the guard must require at least one condition leaf"
    )


# ==========================================================================
# 2. History feasibility -- the class of bug that shipped 50/200 on 4h
# ==========================================================================
def _lookback_days(timeframe, min_candles=20, configured=3):
    """Mirror of the derivation in data_consumer._download_initial_kline_history_for_key."""
    import re

    m = re.match(r"^(\d+)([mhd])$", str(timeframe).strip().lower())
    if not m:
        return configured
    n, unit = int(m.group(1)), m.group(2)
    secs = n * 60 if unit == "m" else n * 3600 if unit == "h" else n * 86400
    needed = (min_candles * secs) / 86400.0
    return int(needed) + 1 if needed > configured else configured


def _candles_for(timeframe, days):
    import re

    m = re.match(r"^(\d+)([mhd])$", timeframe)
    n, unit = int(m.group(1)), m.group(2)
    secs = n * 60 if unit == "m" else n * 3600 if unit == "h" else n * 86400
    return int(days * 86400 / secs)


@pytest.mark.parametrize("slug", sorted(SEED_TEMPLATES))
def test_seed_indicator_periods_fit_the_history_depthsight_actually_loads(slug):
    """
    A seed must not ask for an indicator longer than the delivered history.

    4h yields ~24 candles after the 31f6969 lookback fix. A 200-period EMA
    needs 200. pandas_ta returns a DataFrame (not a Series) when rows < length,
    so evaluate_ma_cross_scalar raised and returned a silent False forever.
    """
    cfg = SEED_TEMPLATES[slug]["config_data"]
    tf = cfg.get("timeframe", "1h")
    available = _candles_for(tf, _lookback_days(tf))

    for root_key in ("filters", "entryConditions"):
        for leaf in _leaf_nodes(cfg.get(root_key)):
            params = leaf.get("params") or {}
            periods = [
                params.get("period"),
                params.get("fast_period"),
                params.get("slow_period"),
            ]
            worst = max([p for p in periods if isinstance(p, int)] or [0])
            assert worst + 5 <= available, (
                f"{slug} on {tf}: indicator period {worst} needs ~{worst + 5} candles "
                f"but only ~{available} are loaded -- unsatisfiable by construction"
            )


# ==========================================================================
# 3. Behaviour: drive the REAL engine and require an actual signal
# ==========================================================================
# Values that make each seeded node's own condition true.
#
# The engine reads the CURRENT indicator value from pair_info and the PREVIOUS
# one out of the candle frame at `current_candle_index - 1`
# (_get_previous_indicator_value), so cross-type conditions need both rows set.
# Writing only the last row of the frame -- which is what a naive fixture does --
# leaves prev=None and the cross silently evaluates False.
_FIRING_PAIR_INFO = {
    "rsi_entry": {"RSI_14": 80.0},   # cross_above 55 (prev row set below)
    "grid_buy": {"RSI_14": 10.0},    # lt 30
    "obi_entry": {"RSI_14": 80.0},   # gt 65
    "bb_entry": {"close": 90.0},     # below BBL_20_2.0 = 95
    "adx_filter": {"ADX_14": 40.0},  # gt 20
}

# prev-row value for cross conditions: must sit on the other side of the
# threshold from the current value.
_PREV_ROW = {
    "rsi_entry": ("RSI_14", 50.0),  # 50 <= 55, now 80 > 55 -> cross_above
}


@pytest.mark.parametrize("slug", sorted(SEED_TEMPLATES))
def test_seeded_template_produces_a_signal_when_its_conditions_are_met(slug):
    """
    The acceptance test the whole file exists for: instantiate the template as
    a real strategy, feed it market data satisfying its own conditions, and
    require a StrategySignal out.
    """
    cfg = dict(SEED_TEMPLATES[slug]["config_data"])
    inst = _instance(cfg)

    md = _market_data()
    pi = _pair_info()
    df = md["kline_1m"]
    cur = pi["current_candle_index"]

    for root_key in ("filters", "entryConditions"):
        for leaf in _leaf_nodes(cfg.get(root_key)):
            nid = leaf.get("id")
            pi.update(_FIRING_PAIR_INFO.get(nid, {}))

            if nid in _PREV_ROW:
                col, prev_val = _PREV_ROW[nid]
                df[col] = prev_val
                df.iloc[cur - 1, df.columns.get_loc(col)] = prev_val

            # ma_cross_condition reads EMA_n off the candle frame at
            # current_candle_index (curr) and one row back (prev).
            params = leaf.get("params") or {}
            fast, slow = params.get("fast_period"), params.get("slow_period")
            if fast and slow:
                df[f"EMA_{fast}"] = 100.0
                df[f"EMA_{slow}"] = 100.0
                # prev: fast <= slow.  curr: fast > slow.  => a golden cross.
                df.iloc[cur - 1, df.columns.get_loc(f"EMA_{fast}")] = 99.0
                df.iloc[cur - 1, df.columns.get_loc(f"EMA_{slow}")] = 101.0
                df.iloc[cur, df.columns.get_loc(f"EMA_{fast}")] = 102.0
                df.iloc[cur, df.columns.get_loc(f"EMA_{slow}")] = 101.0

    signal, weight, trace = inst.check_signal_sync(pi, md, None)

    if slug in EMPTY_BY_DESIGN:
        # Blank Canvas must be genuinely inert. An empty AND gate is
        # vacuously TRUE, so shipping it with an "initialization" block made
        # it open a position on every single candle (verified 10/10 bars).
        assert "initialization" not in cfg, (
            "blank-canvas must not ship an initialization block: combined with "
            "its empty entryConditions gate it signals on every candle"
        )
        assert signal is None, f"{slug} should not trade, but it signalled"
        return

    assert isinstance(signal, StrategySignal), (
        f"{slug} produced no signal even with its own conditions satisfied. "
        f"trace={trace}"
    )
    assert signal.direction == SignalDirection.LONG, (
        f"{slug} signalled {signal.direction}, expected LONG"
    )
    assert signal.stop_loss is not None and signal.stop_loss > 0, (
        f"{slug} signalled with no stop loss: {signal}"
    )
    assert signal.take_profit is not None and signal.take_profit > 0, (
        f"{slug} signalled with no take profit: {signal}"
    )
