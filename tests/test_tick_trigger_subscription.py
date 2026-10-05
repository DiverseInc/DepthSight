# tests/test_tick_trigger_subscription.py
"""
A tick-triggered strategy must actually be fed ticks.

`_select_applicable_instances` routes `on_tick` and `on_condition_met` ONLY
against TICK events. TICK events are emitted by exactly one place in the data
consumer -- its aggTrade branch -- so a tick-triggered strategy is only ever
evaluated if `aggTrade` is subscribed.

`aggTrade` used to be subscribed for one reason only: ML confirmation. Nothing
subscribed it because a trigger needed ticks. The result was a class of
strategy that the editor actively offers ("On Condition Met (Intra-candle)",
a plan-gated first-class trigger) which could be started, would report
status="running", and would then never be evaluated a single time.

Observed live 2026-10-05 on strategy 6fcf5c10 (entryTrigger.type =
"on_condition_met", use_ml_confirmation = false): added to the running pool,
"Entry conditions present" logged at start, and absent from every
`instances=[...]` group in the signal-check log.
"""

import ast
import inspect

from bot_module import controller as controller_module


def _fn_source(name: str) -> str:
    mod = ast.parse(inspect.getsource(controller_module))
    fn = next(
        n
        for n in ast.walk(mod)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == name
    )
    return ast.get_source_segment(inspect.getsource(controller_module), fn) or ""


def test_subscription_builder_subscribes_aggtrade_for_tick_triggers():
    body = _fn_source("_update_monitored_symbols")
    assert '_trigger_type in ("on_tick", "on_condition_met")' in body, (
        "the subscription builder must branch on tick-driven triggers"
    )
    assert 'all_required_data_types[symbol_market_key].add(\n                                    "aggTrade"' in body or (
        '"aggTrade"' in body
    ), "a tick-triggered strategy must cause aggTrade to be subscribed"


def test_candle_close_branch_is_preserved():
    """The existing kline subscription for candle-close triggers must remain."""
    body = _fn_source("_update_monitored_symbols")
    assert 'if _trigger_type == "on_candle_close":' in body
    assert 'f"kline_{_matcher_tf}"' in body


def test_matcher_routes_tick_triggers_only_to_tick_events():
    """
    Documents the constraint the subscription must satisfy: these two trigger
    types are matched ONLY against TICK events, so no kline subscription can
    ever feed them.
    """
    body = _fn_source("_select_applicable_instances")
    assert 'event["type"] == "TICK"' in body
    assert '"on_condition_met"' in body
    assert '"on_tick"' in body
    # ...and they are explicitly excluded from the CANDLE_CLOSE branch.
    assert 'trigger_type == "on_candle_close"' in body