"""A screener feed disabled BY CONFIGURATION must still raise the risk alarm.

WHY
---
`MAIN_APP_WS_ENABLED=false` is the branch that says "the feed is off". But the
RISK-CONTROL OUTAGE escalation lives INSIDE the reconnect-failure handler
(bot_module/data_consumer.py:3489), and this branch never creates the WS task
-- so that handler can never run and the alarm can NEVER fire.

`breakeven_on_regime_change` is implemented in `check_on_screener_update`
(bot_module/strategy.py:3070-3079), whose only production caller is
`_dynamic_symbol_selection_loop` (bot_module/controller.py:5213). That loop is
fed exclusively by this socket. So a disabled feed disarms that stop
permanently, and the old code said so in the WRONG direction: one WARNING
that claimed "Pinned-symbol and STATIC/FIXED strategies are unaffected".

These drive the REAL `DataConsumer.start()` branch with a simulated running
strategy that arms the option. The CRITICAL-vs-WARNING decision follows the
SAME predicate as the failure path -- `dynamic_possible or regime_at_risk` --
so the disabled branch is never louder than the established rule.
"""

import asyncio
import logging

import pytest

from bot_module import config as bot_config
from bot_module.data_consumer import DataConsumer

LOGGER_NAME = "bot_module.data_consumer"


class _Strategy:
    def __init__(self, breakeven):
        self.breakeven_on_regime_change = breakeven


class _Controller:
    def __init__(self, instances):
        self.running_strategy_instances = instances


def _consumer(instances):
    """A real DataConsumer, wired to a controller holding fake strategies."""
    return DataConsumer(
        loop=asyncio.get_event_loop(),
        controller=_Controller(instances),
        market_data_mode="direct",
    )


@pytest.fixture
def feed_disabled(monkeypatch):
    """The deployment the branch is about: screener mode on, WS loop off."""
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "MAIN_APP")
    monkeypatch.setattr(bot_config, "MAIN_APP_WS_ENABLED", False)
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", False)


def _critical_messages(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.CRITICAL and r.name == LOGGER_NAME
    ]


async def test_disabled_feed_with_a_regime_change_strategy_raises_critical(
    caplog, feed_disabled
):
    """THE CASE: the feed is off by config and a running strategy relies on it.

    Before the fix this produced a single WARNING. The RISK-CONTROL OUTAGE
    alarm could never fire from here, because the failure handler that owns it
    is never created.
    """
    dc = _consumer({"cfg-1": (_Strategy(True), "other")})

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        await dc.start()

    criticals = _critical_messages(caplog)
    assert criticals, (
        "A screener feed that is DISABLED BY CONFIGURATION while a running "
        "strategy has breakeven_on_regime_change=true must raise a CRITICAL. "
        "It raised nothing: "
        + repr([r.getMessage() for r in caplog.records if r.name == LOGGER_NAME])
    )
    joined = "\n".join(criticals)
    assert "RISK-CONTROL OUTAGE" in joined, (
        "the CRITICAL must carry the same RISK-CONTROL OUTAGE meaning as the "
        f"failure-path alarm. Got: {joined!r}"
    )
    assert "UNPROTECTED" in joined, (
        f"the CRITICAL must say the positions are UNPROTECTED. Got: {joined!r}"
    )
    assert "DISABLED BY CONFIGURATION" in joined, (
        "the CRITICAL must distinguish 'disabled by configuration' from "
        f"'merely down'. Got: {joined!r}"
    )


async def test_disabled_feed_without_a_regime_change_strategy_stays_a_warning(
    caplog, feed_disabled
):
    """No strategy depends on the feed, so there is nothing to alarm about.

    This is the established rule: the outage alarm only fires when something
    would actually be unprotected. Making it louder than that would train
    operators to ignore it.
    """
    dc = _consumer({"cfg-1": (_Strategy(False), "other")})

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        await dc.start()

    assert not _critical_messages(caplog), (
        "with no strategy relying on the feed there is no risk-control "
        "outage, so the branch must stay at WARNING. It raised: "
        f"{_critical_messages(caplog)!r}"
    )
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == LOGGER_NAME
    ]
    assert any("MAIN_APP_WS_ENABLED is false" in m for m in warnings), (
        f"the existing warning must survive. Got: {warnings!r}"
    )


async def test_disabled_feed_with_dynamic_allowed_raises_critical(
    caplog, feed_disabled, monkeypatch
):
    """The other arm of the established predicate: DYNAMIC is possible.

    A DYNAMIC strategy with the feed off by configuration starts, reports
    "running", and never trades -- exactly what the DYNAMIC guard at
    api/routes/strategies.py refuses. The bot must say so at CRITICAL too.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)
    dc = _consumer({"cfg-1": (_Strategy(False), "other")})

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        await dc.start()

    criticals = _critical_messages(caplog)
    assert criticals, (
        "DYNAMIC is possible and the feed is disabled by configuration, so the "
        "symbol source is down and the bot must say so at CRITICAL."
    )
    assert any(
        "DISABLED BY CONFIGURATION" in m for m in criticals
    ), f"got: {criticals!r}"


async def test_disabled_feed_without_a_controller_does_not_crash(
    caplog, feed_disabled
):
    """The controller may not exist yet at start-up.

    `_regime_change_controls_at_risk` is deliberately total: it cannot raise
    and it answers False when the controller is missing. A logger must never be
    able to break market data.
    """
    dc = DataConsumer(
        loop=asyncio.get_event_loop(),
        controller=None,
        market_data_mode="direct",
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        await dc.start()

    assert not _critical_messages(caplog), (
        "with no controller there is no known risk-control dependency, so the "
        "branch must stay at WARNING."
    )
    assert any(
        "MAIN_APP_WS_ENABLED is false" in r.getMessage()
        for r in caplog.records
        if r.name == LOGGER_NAME
    ), "the existing warning must still be logged"


async def test_an_enabled_feed_is_untouched_by_this_branch(caplog, monkeypatch):
    """Behaviour must be unchanged when the feed IS enabled.

    The new branch only exists inside `if not MAIN_APP_WS_ENABLED`. With the
    feed enabled the WS task is created and none of the disabled-feed logging
    may appear.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "MAIN_APP")
    monkeypatch.setattr(bot_config, "MAIN_APP_WS_ENABLED", True)
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", False)
    monkeypatch.setattr(
        bot_config, "MAIN_APP_WS_URL", "ws://127.0.0.1:1/never-connected"
    )

    dc = _consumer({"cfg-1": (_Strategy(True), "other")})

    # Keep the test offline: the real loop would open a socket.
    async def _noop_ws_loop():
        return None

    dc._main_app_ws_loop = _noop_ws_loop

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        await dc.start()
        await asyncio.sleep(0.01)

    messages = [
        r.getMessage() for r in caplog.records if r.name == LOGGER_NAME
    ]
    assert not any("MAIN_APP_WS_ENABLED is false" in m for m in messages), (
        f"the disabled-feed branch must not run when the feed is enabled: {messages!r}"
    )
    assert not any("RISK-CONTROL OUTAGE" in m for m in messages), (
        f"no risk-control outage may be claimed while the feed is enabled: {messages!r}"
    )
