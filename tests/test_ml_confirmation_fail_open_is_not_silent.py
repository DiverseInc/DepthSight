# File: tests/test_ml_confirmation_fail_open_is_not_silent.py
"""
A strategy that asks for ML confirmation must never look like it got one when
the model never ran.

THE DEFECT
----------
`_process_signal` evaluates live ML confirmation in two nested blocks:

    ml_confirmed_this_signal_live = True            # <- initialised optimistic
    if use_ml_confirmation_flag:                    # strategy asked for it
        if (runtime_enabled and extractor and pipeline):
            ...                                     # model actually runs
        else:
            logger.debug("... not enabled or components not ready. Skipping.")

The outer `else` was a DEBUG line, and it fell through leaving
`ml_confirmed_this_signal_live` at its initialised `True`. Two consequences:

  1. **Silent.** Every other fail-open in that same block is a WARNING that
     literally says "Allowing signal (fail-open)" -- missing klines, None from
     predict_proba_one, failed feature extraction, failed normalisation. This
     was the single fail-open that announced nothing, and at the deployed INFO
     level it was invisible.
  2. **A false audit record.** `signal.details["ml_confirmed_live"]` was
     stamped `True`, so the trade log affirmatively recorded that the model had
     approved a signal it never evaluated. A strategy configured with
     `use_ml_confirmation: true` was trading as though a risk control was
     gating it when nothing was checking anything.

This is NOT the same event as the sibling fail-opens. Those are "the model ran
and could not reach a verdict on this candle" -- transient. This one is "the
model never loaded at all" -- structural, and it persists for the life of the
process.

LIVE EVIDENCE (2026-10-07, deployment of 5d41335)
-------------------------------------------------
Every one of the 12 paper controllers logged at ERROR:

    [MLConfirmLive] Failed to load Live ML Confirmation model from
    data/offline_trained_model.joblib. Live ML Confirmation will be SKIPPED.

while `ML_CONFIRMATION_ENABLED` was true. Every controller therefore sat in the
exact state this test covers, permanently.

WHY THIS TEST SHAPE
-------------------
It calls the REAL `_process_signal` -- no copy of the branch under test -- on a
real `TradingController` with a real registered strategy instance, and asserts
on observable state: the WARNING actually emitted, and the `signal.details`
the trade log is built from. The signal is driven only as far as the end of the
ML block; `_get_market_info` is stubbed to raise a sentinel purely as a
ramp-stop, because everything this test claims has already happened by then.
"""

import asyncio
import logging
import time

import pytest

from bot_module.controller import (
    ActivePositionMap,
    LivePosition,
    SignalDirection,
    StrategySignal,
    TradingController,
)
from bot_module.paper_executor import PaperTradingExecutor
from bot_module.risk_manager import RiskManager

USER_ID = 10
SYMBOL = "BTCUSDT"
STRATEGY_NAME = "VisualBuilderStrategy"


class _StopHere(Exception):
    """Ramp-stop raised from the stubbed post-ML section.

    Deliberately NOT swallowed by `_process_signal`'s own handlers in a way that
    hides the assertions: the test catches it and then inspects state that was
    written before it was raised.
    """


class _FakeDataConsumer:
    async def get_kline_history(self, *a, **k):
        return None

    async def get_recent_trades(self, *a, **k):
        return None


class _RecordingTradeLogger:
    def __init__(self):
        self.events = []

    def log_event(self, event_type, data=None, **kw):
        self.events.append((event_type, data))


class _StubStrategy:
    """Only `.NAME` is read by `_process_signal` when locating a config."""

    NAME = STRATEGY_NAME


def _build_controller(use_ml_confirmation: bool):
    """A real TradingController carrying only the attribute surface this path reads."""
    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=None,
        data_consumer=_FakeDataConsumer(),
        redis_client=None,
    )

    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    controller.user_id = USER_ID
    controller.api_key_id = None
    controller.api_key_name = "paper"
    controller.executors = {"live": None, "paper": paper_executor}
    controller.consumer = _FakeDataConsumer()

    # --- throttling / locking that `_process_signal` touches before the ML block
    controller._processing_signal_lock = asyncio.Lock()
    controller._processing_signal_for_symbol = set()
    controller._recent_signals = {}
    controller._signal_throttle_period = 0
    controller._last_position_close_time_per_symbol = {}
    controller._symbol_cooldown_duration = 0
    controller._positions_dict_lock = asyncio.Lock()
    controller._active_positions = ActivePositionMap()
    controller._symbol_locks = {}

    # --- the running strategy the config lookup must find
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = {
        "cfg-abc": (
            _StubStrategy(),
            {
                "config": {"market_type": "futures"},
                "market_type": "futures",
                "mode": "paper",
                "use_ml_confirmation": use_ml_confirmation,
                "user_id": USER_ID,
            },
        )
    }

    # --- THE CONDITION UNDER TEST: model never loaded.
    # This mirrors production exactly -- ML_CONFIRMATION_ENABLED is true and
    # `data/offline_trained_model.joblib` is absent, so `start()` sets the
    # runtime flag False and leaves both components None.
    controller._ml_confirmation_enabled_live_runtime = False
    controller._ml_confirmation_feature_extractor_live = None
    controller._ml_confirmation_pipeline_live = None

    controller.trade_logger = _RecordingTradeLogger()
    controller.telegram_notifier = None
    controller.user_telegram_chat_id = None
    controller.rm = RiskManager(
        executor=None,
        paper_executor=paper_executor,
        user_id=USER_ID,
        db_session=None,
        user_settings={},
    )
    controller.rm.max_concurrent_trades = 100

    async def _stop(*a, **k):
        raise _StopHere()

    controller._get_market_info = _stop
    return controller


def _make_signal():
    return StrategySignal(
        strategy_name=STRATEGY_NAME,
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        stop_loss=90_000.0,
        take_profit=95_000.0,
        entry_price=92_000.0,
        trigger_price=92_000.0,
        details={"strategy_config_id": "cfg-abc"},
    )


def _run(controller, signal):
    """Drive the real method, stopping cleanly past the ML block.

    `_process_signal` wraps its whole body in a broad handler and logs
    "UNEXPECTED EXCEPTION" rather than re-raising, so the ramp-stop does not
    escape. That is fine -- everything this test asserts on was already written
    to `signal.details` and the log stream before the sentinel was raised, and
    that the sentinel was reached at all is itself proof the ML block ran.
    """
    pair_info = {"market_type": "futures", "last_price": 92_000.0}
    asyncio.get_event_loop().run_until_complete(
        controller._process_signal(signal, pair_info)
    )
    return signal


# --- the defect --------------------------------------------------------------


def test_ml_requested_but_model_missing_is_not_silent(caplog):
    """The whole point: this fail-open must not be DEBUG-only.

    Before the fix this branch emitted a DEBUG line, which is invisible at the
    deployed INFO level -- so a strategy trading with no ML confirmation
    produced no evidence at all.
    """
    controller = _build_controller(use_ml_confirmation=True)
    signal = _make_signal()

    with caplog.at_level(logging.INFO):
        _run(controller, signal)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, (
        "A strategy requested ML confirmation but the model was unavailable, "
        "and nothing was logged at WARNING. This fail-open is silent again."
    )

    joined = " ".join(r.getMessage() for r in warnings)
    assert "fail-open" in joined.lower(), (
        f"The WARNING must name the fail-open explicitly, so an operator "
        f"reading logs knows nothing gated the trade. Got: {joined!r}"
    )
    assert "ML confirmation was REQUESTED" in joined, (
        f"The WARNING must state that ML confirmation was requested but could "
        f"not run, not merely that it was 'skipped'. Got: {joined!r}"
    )


def test_ml_requested_but_model_missing_names_the_missing_component(caplog):
    """'not ready' is not actionable. The log must say WHAT is missing."""
    controller = _build_controller(use_ml_confirmation=True)
    signal = _make_signal()

    with caplog.at_level(logging.INFO):
        _run(controller, signal)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "_ml_confirmation_enabled_live_runtime" in joined, (
        "The WARNING must name the specific missing component so the operator "
        f"knows what to fix. Got: {joined!r}"
    )


def test_trade_log_does_not_claim_ml_approved_a_signal_it_never_saw():
    """The false audit record is the more damaging half of the defect.

    `ml_confirmed_live` is True both when the model approved the signal and when
    the model never ran, so on its own it cannot support the claim "ML cleared
    this trade". The new `ml_confirmation_evaluated` flag must be False here.
    """
    controller = _build_controller(use_ml_confirmation=True)
    signal = _make_signal()

    _run(controller, signal)

    assert signal.details.get("ml_confirmation_evaluated") is False, (
        "The model never ran, so the signal must be recorded as NOT evaluated. "
        f"Got: {signal.details.get('ml_confirmation_evaluated')!r}"
    )

    reason = signal.details.get("ml_confirmation_skipped_reason")
    assert reason, "The reason the model could not run must be recorded."
    assert "fail-open" in reason.lower(), (
        f"The recorded reason must name the fail-open. Got: {reason!r}"
    )


# --- guards: the fix must not have broken the working paths -------------------


def test_ml_not_requested_records_nothing_extra():
    """A strategy that never asked for ML must not be marked as a fail-open.

    Otherwise every ordinary trade in the system would carry a scary skipped
    reason, which is the same mistake as a guard that cries wolf on normal
    traffic.
    """
    controller = _build_controller(use_ml_confirmation=False)
    signal = _make_signal()

    _run(controller, signal)

    assert "ml_confirmation_skipped_reason" not in signal.details, (
        "ML was never requested for this strategy, so there is nothing to "
        f"report. Got: {signal.details.get('ml_confirmation_skipped_reason')!r}"
    )


def test_signal_not_rejected_by_absent_model_remains_fail_open():
    """Behaviour is deliberately UNCHANGED: the trade still proceeds.

    Flipping this to fail-closed is a business decision about risk appetite,
    not a bug fix, and would silently stop every ML-configured strategy from
    trading. This test pins the current behaviour so the logging fix cannot be
    mistaken for a behaviour change, and so the decision is made explicitly
    later rather than by accident.
    """
    controller = _build_controller(use_ml_confirmation=True)
    signal = _make_signal()

    _run(controller, signal)

    rejections = [
        e for e in controller.trade_logger.events
        if e[0] == "SIGNAL_REJECTED_ML_LIVE"
    ]
    assert not rejections, (
        "The absent model must not start rejecting trades -- that would halt "
        "every ML-configured strategy. Failing open is still the behaviour; "
        "only its visibility changed."
    )


# --- the diagnosability fix ---------------------------------------------------


def test_log_scope_identifies_the_controller():
    """All 12 paper controllers share `api_key_name='paper'`.

    That is what made five independent controllers look like one controller
    misbehaving on a 60s throttle. The prefix must identify the actor.
    """
    controller = _build_controller(use_ml_confirmation=True)
    assert controller._log_scope == "paper/u10", (
        f"Expected the controller to identify itself by user. Got: "
        f"{controller._log_scope!r}"
    )


# --- structural regression ----------------------------------------------------


def test_log_scope_property_did_not_swallow_the_rest_of_init():
    """REGRESSION: an earlier version of `_log_scope` was defined INSIDE `__init__`.

    It was placed immediately after the `logger.info("TradingController
    initialized.")` line, which pushed everything that followed -- the
    TelegramNotifier status logs and the symbol-cooldown log -- into the
    property's body, after its `return`. Python accepted it silently; the file
    parsed, every test still passed, and three startup log lines were simply
    never emitted again.

    That is the same defect class this whole file is about: a change that is
    invisible in every green test run and only visible in production as missing
    output. It was caught by the OpenCode adversarial review, not by a test.

    This asserts on the COMPILED function rather than the source text, so it
    survives reformatting and cannot be fooled by a string that merely appears
    somewhere in the file. If any statement is ever orphaned out of `__init__`
    again, this goes red.
    """
    init_consts = TradingController.__init__.__code__.co_consts
    assert any(
        isinstance(c, str) and "Symbol cooldown after close" in c
        for c in init_consts
    ), (
        "The symbol-cooldown log has left __init__. If a property or nested "
        "def was inserted into the constructor body, the statements after it "
        "become unreachable dead code that no test would otherwise catch."
    )
    assert any(
        isinstance(c, str) and "TelegramNotifier instance received" in c
        for c in init_consts
    ), (
        "The TelegramNotifier status log has left __init__, same failure mode."
    )


def test_log_scope_is_a_property_not_a_constructor_step():
    """`_log_scope` must be a property on the class, not code inside `__init__`."""
    assert isinstance(
        TradingController.__dict__.get("_log_scope"), property
    ), (
        "_log_scope must be declared as a @property at class level. If it is "
        "defined inside __init__ it becomes dead code after its return."
    )