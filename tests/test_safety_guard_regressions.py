"""Regression tests for two safety guards that mutation testing proved unprotected.

Each guard below was broken deliberately, one at a time, and the **entire** suite
stayed green. These tests pin the guards themselves rather than the fixtures that
happen to exercise them.

M1 -- ``bot_module/controller.py:2051-2058``
    A paper-only controller (``api_key_id is None``) must **reject** a live
    ``START_STRATEGY``. Flipping the guard open (``if command_mode != "paper":``
    became ``if False:``) changed nothing in the suite. Nothing asserted the
    rejection, only the acceptance -- so a user with no live API key could have
    had a live order routed.

M3 -- ``bot_module/strategy.py:6786``
    The proximity band is **inclusive** (``abs(last_price - level_price) <=
    proximity``). Changing it to ``<`` changed nothing, *including*
    ``test_return_to_level_parity``. The boundary was pinned only inside a test
    fixture, never in the engine, so the exact bug that cost the 2026-10-15
    session could return through a one-character engine change.

Both guards carry a **positive control**: each also asserts the legitimate case
still gets through. Without that, a handler that refused everything would pass.
"""

import asyncio
import logging
from datetime import datetime, timezone

import numpy as np
import pytest

if not hasattr(np, "NaN"):
    np.NaN = np.nan  # pragma: no cover - numpy<2 compatibility

from bot_module import controller as controller_module
from bot_module.controller import TradingController
from bot_module.strategy import VisualBuilderStrategy


# ---------------------------------------------------------------------------
# M3 -- strategy.py:6786 -- the proximity band is inclusive
# ---------------------------------------------------------------------------

LEVEL = 100.0
ATR = 5.0
# proximity defaults to 0.1 x ATR (strategy.py:6771). Derive it instead of
# hardcoding 0.5 so the boundary assertions do not depend on float rounding.
PROXIMITY = 0.1 * ATR


def _fresh_strategy() -> VisualBuilderStrategy:
    return VisualBuilderStrategy(
        params={
            "config": {
                "filters": {"id": "f1", "type": "AND", "children": []},
                "entryConditions": {"id": "e", "type": "AND", "children": []},
            },
            "enabled": True,
            "min_total_foundation_weight_threshold": 0.0,
        }
    )


def _probe(strategy, last_price):
    """Call the real engine entry point and return ``(result, details)``."""
    return strategy._check_condition_return_to_level(
        pair_info={
            "symbol": "BTC/USDT:USDT",
            "last_price": last_price,
            "atr": ATR,
            "timestamp_dt": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
        market_data={},
        params={
            "level_source": {"source": "constant", "value": LEVEL},
            "retest_type": "touch",
            "approach_direction": "any",
            "confirmation_time_sec": 0,
        },
        context={},
    )


def test_proximity_band_is_inclusive_at_the_exact_boundary():
    """A price exactly `proximity` away is INSIDE the band.

    This is the pin that was missing. `test_return_to_level_parity` placed the
    boundary inside its fixture; nothing pinned it in the engine.
    """
    strategy = _fresh_strategy()
    result, details = _probe(strategy, LEVEL + PROXIMITY)

    # Premise: the probe really did land on the boundary.
    assert details["proximity"] == pytest.approx(PROXIMITY)
    assert details["level_price"] == pytest.approx(LEVEL)
    assert details["last_price"] == pytest.approx(LEVEL + PROXIMITY)
    assert abs(details["last_price"] - details["level_price"]) == pytest.approx(
        PROXIMITY
    )

    # Truthiness, not `is True`: the value can be a numpy bool, and numpy
    # scalars are not Python bools (the same class of trap as np.int64 not
    # being an int).
    assert details["price_is_near_level"], (
        "the proximity band must be INCLUSIVE -- a price exactly `proximity` "
        "away is still 'near level' (strategy.py:6786 uses `<=`, not `<`)"
    )
    # And it must actually pass the near-level gate, not just report True.
    assert "Price is away from level" not in details


def test_proximity_band_excludes_a_price_just_outside():
    """The other side of the boundary, so `>=` cannot be 'fixed' either way."""
    strategy = _fresh_strategy()
    result, details = _probe(strategy, LEVEL + PROXIMITY * (1 + 1e-9))

    assert not details["price_is_near_level"]
    assert details["info"] == "Price is away from level"
    assert result is False


def test_proximity_band_includes_a_price_well_inside():
    """Positive control: the band is not vacuously empty."""
    strategy = _fresh_strategy()
    result, details = _probe(strategy, LEVEL)

    assert details["price_is_near_level"]
    assert "Price is away from level" not in details


# ---------------------------------------------------------------------------
# M1 -- controller.py:2051-2058 -- paper-only must reject a live start
# ---------------------------------------------------------------------------


def _controller(api_key_id):
    """A minimal TradingController.

    Uses ``__new__`` + set-state-by-hand, the pattern already established
    elsewhere in this suite (see the `data_pipeline_e2e AttributeError` entry in
    ``baseline_failures.json``). Only the attributes
    ``_handle_start_strategy_command`` touches before and immediately after the
    guard are set: ``user_id``, ``api_key_id``, ``api_key_name`` (read by the
    ``_log_scope`` property), ``instances_lock`` and
    ``running_strategy_instances``.
    """
    controller = TradingController.__new__(TradingController)
    controller.user_id = 1
    controller.api_key_id = api_key_id
    controller.api_key_name = "paper" if api_key_id is None else "okx-main"
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = {}
    return controller


def _start_payload(**overrides):
    payload = {
        "user_id": 1,
        "id": "0123456789abcdef0123456789abcdef",
        "config_data": {"strategy_name": "VisualBuilderStrategy"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def created_strategies(monkeypatch):
    """Record calls to ``create_strategy_instance``.

    The spy returns ``None`` so the handler logs and returns at
    ``controller.py:2156-2160`` instead of walking into real strategy
    construction. Whether the spy fired is the observable that separates "the
    guard let the command through" from "the handler rejected it".
    """
    calls = []

    def _spy(strategy_name, params=None):
        calls.append(strategy_name)
        return None

    monkeypatch.setattr(controller_module, "create_strategy_instance", _spy)
    return calls


async def test_paper_only_controller_rejects_a_live_start(created_strategies):
    """The live-money gap. A controller with no API key must not start live."""
    controller = _controller(api_key_id=None)

    await controller._handle_start_strategy_command(_start_payload(mode="live"))

    assert created_strategies == [], (
        "a paper-only controller (api_key_id is None) must reject a live "
        "START_STRATEGY at controller.py:2051-2058 -- otherwise a user with "
        "no live API key has a live order routed"
    )
    # Also assert the observable end state, not just the spy: a rejected
    # command must leave no instance registered.
    assert controller.running_strategy_instances == {}


async def test_paper_only_controller_logs_why_it_rejected_the_live_start(
    created_strategies, caplog
):
    """The rejection must be *visible*.

    Two reasons this matters. Operationally, this codebase has already lost time
    to log lines that could not be attributed to an actor -- that is what the
    `_log_scope` property at controller.py:688 exists for. Structurally, this is
    the only assertion that distinguishes "the paper-only guard fired" from
    "some other early return fired", which is otherwise ambiguous from the
    outside.
    """
    controller = _controller(api_key_id=None)

    with caplog.at_level(logging.INFO, logger="bot_module.controller"):
        await controller._handle_start_strategy_command(_start_payload(mode="live"))

    assert "Paper-only controller skipping non-paper command" in caplog.text
    assert "mode=live" in caplog.text


async def test_paper_only_controller_rejects_a_start_with_no_mode_field(
    created_strategies,
):
    """``payload.get("mode", "live")`` -- an absent mode means LIVE."""
    controller = _controller(api_key_id=None)

    await controller._handle_start_strategy_command(_start_payload())

    assert created_strategies == []
    assert controller.running_strategy_instances == {}


async def test_paper_only_controller_accepts_a_paper_start(created_strategies):
    """Positive control: the guard must not be 'always refuse everything'.

    This is what the 2026-09-22 fix (`44e7760`) was actually for -- a paper-only
    controller is the canonical handler for users without a live key.
    """
    controller = _controller(api_key_id=None)

    await controller._handle_start_strategy_command(_start_payload(mode="paper"))

    assert created_strategies == ["VisualBuilderStrategy"]


async def test_user_id_is_compared_as_a_string_on_both_sides(created_strategies):
    """Pin the `str()/str()` comparison at controller.py:2020.

    The comment above that line records a real shipped bug: a strict `!=`
    between the JSON-decoded payload value and the controller's own id dropped
    every START_STRATEGY whenever the types differed ("1" vs 1). With both
    sides being the same int here, this test cannot tell the correct comparison
    from the broken one -- so it uses a *string* on one side deliberately.
    """
    controller = _controller(api_key_id=None)  # user_id = 1 (int)
    payload = _start_payload(mode="paper")
    payload["user_id"] = "1"  # string, as a JSON round-trip can produce

    await controller._handle_start_strategy_command(payload)

    assert created_strategies == [
        "VisualBuilderStrategy"
    ], "a string payload user_id must still match an int controller user_id"


async def test_live_controller_with_a_matching_key_accepts_a_live_start(
    created_strategies,
):
    """Positive control for the other branch: the guard is paper-only specific."""
    controller = _controller(api_key_id=99)

    await controller._handle_start_strategy_command(
        _start_payload(mode="live", api_key_id=99)
    )

    assert created_strategies == ["VisualBuilderStrategy"]


async def test_live_controller_rejects_a_start_for_a_different_key(
    created_strategies,
):
    """The pre-existing key-id match must survive too."""
    controller = _controller(api_key_id=99)

    await controller._handle_start_strategy_command(
        _start_payload(mode="live", api_key_id=7)
    )

    assert created_strategies == []