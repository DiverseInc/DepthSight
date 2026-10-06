# File: tests/test_untradeable_strategy_is_not_reported_running.py
"""
A strategy the engine already knows can never signal must not be published
as a healthy "running" one.

Context
-------
`_handle_start_strategy_command` detects, at instance creation, that a
VisualBuilderStrategy has no usable `entryConditions` -- either the key is
absent, or it is present with no children. Both were logged as a loud
`NO ENTRY CONDITIONS` ERROR and then... nothing. The instance stayed in the
running pool and the publisher derived:

    "status": "in_position" if open_positions > 0 else "running"

so a strategy that structurally cannot ever signal rendered on the dashboard
as a working one, indefinitely. The condition was known to the bot and never
left the logs.

These tests pin the truth table AND the response-schema passthrough, because
the second one is a silent-failure trap: `StrategyInfo` has no
`extra="allow"`, so if `status_detail` were not declared on the model, Pydantic
would drop it without error and the UI would receive a bare `cannot_trade` with
no explanation attached.
"""

import pytest

from bot_module.controller import _derive_strategy_status


# --- Truth table -------------------------------------------------------------


def test_healthy_strategy_with_no_positions_is_running():
    """The overwhelmingly common case must be completely unaffected."""
    assert _derive_strategy_status(0, None) == "running"


def test_strategy_with_positions_is_in_position():
    assert _derive_strategy_status(1, None) == "in_position"


def test_unusable_strategy_with_no_positions_is_cannot_trade():
    """This is the actual defect: an untradeable strategy said 'running'."""
    assert _derive_strategy_status(0, "no entryConditions key") == "cannot_trade"


def test_open_position_wins_over_cannot_trade():
    """Risk-relevant fact must never be hidden by a config warning.

    An untradeable strategy cannot open a position, so this combination
    should not occur in practice. If it somehow does, the open position is
    the thing the user must see.
    """
    assert _derive_strategy_status(1, "no entryConditions key") == "in_position"


def test_empty_string_reason_does_not_mark_cannot_trade():
    """A falsy reason means 'no reason recorded', i.e. a healthy strategy.

    Guards against a producer that writes `""` and silently flipping every
    strategy on the dashboard to cannot_trade.
    """
    assert _derive_strategy_status(0, "") == "running"
    assert _derive_strategy_status(0, None) == "running"


def test_whitespace_only_reason_is_still_treated_as_present():
    """Documents the truthiness rule this helper relies on."""
    # A whitespace reason is truthy, so it DOES mark cannot_trade. If the
    # producer ever emits "  ", it will surface rather than hide.
    assert _derive_strategy_status(0, "  ") == "cannot_trade"


# --- Response-schema passthrough ---------------------------------------------


def _real_published_payload(**overrides):
    """The exact dict shape `controller.py` writes to Redis.

    Built from the publisher, not from a guess: every key below is one the
    publisher actually emits, plus `name` which `list_strategies` joins in.
    """
    payload = {
        "id": "e898f74e-000f-4fb3-acc1-c4fc46177533",
        "strategy_name": "VisualBuilderStrategy",  # the CLASS, never a display name
        "symbol": "BTCUSDT",
        "market_type": "futures",
        "status": "cannot_trade",
        "status_detail": (
            "config_data uses the 'blocks' format, which the trading engine does "
            "not read; it only understands 'entryConditions'."
        ),
        "pnl": 0.0,
        "open_positions": 0,
        "started_at": "2026-10-06T18:00:00+00:00",
        "params": {},
        "user_id": 10,
        "api_key_id": None,
        "symbol_selection_mode": "STATIC",
        "mode": "paper",
        "name": "EMA 50/200 Golden Cross",
    }
    payload.update(overrides)
    return payload


def test_strategy_info_keeps_status_detail():
    """The declared field must survive validation.

    Without `status_detail` on the model, Pydantic strips it silently and the
    UI gets `cannot_trade` with no explanation -- a red badge the user cannot
    act on.
    """
    from api import schemas

    info = schemas.StrategyInfo(**_real_published_payload())
    assert info.status == "cannot_trade"
    assert info.status_detail is not None
    assert "blocks" in info.status_detail


def test_strategy_info_status_detail_is_none_for_a_healthy_strategy():
    """Normal strategies must not gain a spurious reason."""
    from api import schemas

    info = schemas.StrategyInfo(
        **_real_published_payload(status="running", status_detail=None)
    )
    assert info.status == "running"
    assert info.status_detail is None


def test_strategy_info_accepts_a_payload_without_the_new_key():
    """A strategy published by an OLDER bot has no `status_detail` key at all.

    The API must not 500 on those while the fleet rolls forward.
    """
    from api import schemas

    payload = _real_published_payload(status="running")
    payload.pop("status_detail")
    info = schemas.StrategyInfo(**payload)
    assert info.status == "running"
    assert info.status_detail is None


def test_strategy_info_preserves_the_joined_display_name():
    """Regression guard for e81e2b5: the name join must survive the same path."""
    from api import schemas

    info = schemas.StrategyInfo(**_real_published_payload())
    assert info.name == "EMA 50/200 Golden Cross"
    # And the class name is still there; the UI prefers `name`.
    assert info.strategy_name == "VisualBuilderStrategy"