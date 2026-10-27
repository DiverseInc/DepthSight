# ruff: noqa: E402
# bot_module/risk_manager.py
import logging
import math
import time
import asyncio
import json
from datetime import timedelta, datetime, time as dt_time, timezone
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from collections import defaultdict, deque
from sqlalchemy.ext.asyncio import AsyncSession

from bot_module import config
from bot_module.strategy import StrategySignal, SignalDirection
from bot_module.exchanges import ExchangeExecutor
from bot_module.paper_executor import PaperTradingExecutor
from bot_module.runtime_dependencies import crud
import redis.asyncio as redis_asyncio

logger = logging.getLogger("bot_module.risk_manager")

# The trading-guard state -- the consecutive-loss streak and the day's PnL --
# is persisted to Redis so that a restart cannot silently un-block a
# correctly blocked account. These constants bound the Redis round trip: a
# hung Redis must never stall the controller's periodic loop, and a lost
# write must never stall a trade handler.
RISK_STATE_PERSIST_TIMEOUT_SECONDS = 5.0

# The version of the persisted payload. Bump it when the shape changes in a
# way that an older reader would misread.
#
# v2 adds `start_of_day_day_str`: the UTC day the `start_of_day_balance`
# belongs to. v1 recorded only `day_str` -- the day the STREAK and the day's
# PnL belong to -- so the reader validated one day's worth of facts and then
# applied a quantity scoped to a DIFFERENT day. See
# `_apply_persisted_guard_state`.
RISK_STATE_VERSION = 2

# Why the gate is closed while the guard state is UNVERIFIABLE.
#
# This is an INFRASTRUCTURE fault, not an operator decision: unlike the
# emergency stop it is released automatically as soon as the state can be
# read again (see RiskManager._reverify_guard_state), so a Redis blip costs
# availability for as long as Redis is down and not a human decision.
RISK_GUARD_UNVERIFIED_REASON = (
    "risk guard state could not be verified at start-up (Redis unreachable) "
    "- trading disabled until the state can be read"
)

# Why the gate is closed while the day's opening balance is UNKNOWN.
#
# The daily-loss and max-drawdown limits are a RATIO against the day's opening
# balance. With no opening balance the ratio cannot be computed at all, and the
# previous code treated "cannot be computed" as "not breached" -- a fail-OPEN
# inside a function whose own comment demands failing closed on unknown state.
RISK_GUARD_BASE_UNKNOWN_REASON = (
    "the opening balance for the current UTC day is unknown "
    "- trading disabled until it can be established"
)

# Why the gate is closed when the persisted guard state has VANISHED for an
# account that has traded before.
#
# A missing key is NOT a first boot: a first boot has never had a guard state to
# lose. See `_read_persisted_guard_state` for the discriminator that separates
# the two cases.
RISK_GUARD_STATE_LOST_REASON = (
    "the persisted trading-guard state is missing for an account that has "
    "traded before - treated as an unknown state, trading disabled"
)


@dataclass
class TradeStats:
    start_of_day_balance: float = 0.0
    # The UTC day `start_of_day_balance` was captured on, and "" when it is not
    # known. This is a SEPARATE field from `last_known_day_str` on purpose: the
    # two describe different quantities (the opening balance and the loss
    # streak / day's PnL) and only one of them used to travel with the payload.
    start_of_day_day_str: str = ""
    current_balance: float = 0.0
    today_pnl: float = 0.0
    consecutive_losses: int = 0
    last_trade_time: float = 0.0
    current_trading_day_start_ts: float = 0.0
    last_known_day_str: str = ""


@dataclass
class SymbolStrategyPerformanceStats:
    trade_results_buffer: deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=config.STRATEGY_SYMBOL_ROLLING_WINDOW_SIZE)
    )
    current_pnl_sum_usd: float = 0.0
    sum_initial_risk_usd_in_window: float = 0.0
    current_wins_in_window: int = 0
    current_trades_in_window: int = 0
    current_consecutive_losses: int = 0
    current_consecutive_wins_for_recovery: int = 0
    current_risk_multiplier_index: int = 0
    last_penalty_timestamp: float = 0.0
    total_trades_for_assessment: int = 0
    total_pnl_usd: float = 0.0


from bot_module.datatypes import BasePosition


def ensure_dict(obj) -> Dict[str, Any]:
    """Utility for guaranteed conversion of an object (Pydantic, JSON, etc.) into a dictionary."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        # For Pydantic v2, use mode='json' to ensure that types like datetime
        # are converted to strings (JSON-serializable).
        try:
            return obj.model_dump(mode="json")
        except TypeError:
            # Fallback if mode='json' is not supported (unlikely in modern Pydantic v2)
            return obj.model_dump()
    if hasattr(obj, "dict"):
        # Pydantic v1 fallback
        return obj.dict()
    if isinstance(obj, str):
        try:
            import json

            return json.loads(obj)
        except Exception:
            return {}
    return {}


class RiskManager:
    def __init__(
        self,
        executor: ExchangeExecutor,
        paper_executor: "PaperTradingExecutor",
        user_id: Optional[int],
        db_session: Optional[AsyncSession],
        user_settings: Dict[str, Any],
        api_key_name: Optional[str] = None,
    ):
        self.executor = executor
        self.paper_executor = paper_executor
        self.user_id = user_id
        self.db_session = db_session
        self.api_key_name = api_key_name
        # The api key this controller owns, used to scope the persisted
        # trading-guard state. The controller fills it in (see
        # TradingController.__init__), because the RiskManager is constructed
        # before the controller exists.
        #
        # The api_key_NAME is deliberately NOT the scope: names are not
        # unique per user (the api_keys table has no unique constraint on
        # (user_id, name)), so two controllers of the same user could share a
        # name -- and would then share one guard key, so one controller's
        # write could erase the other controller's loss streak. The api key
        # ID is unique and stable across a rename.
        self.api_key_id: Optional[int] = None
        self.stats = TradeStats()
        # THE PAPER SANDBOX. Its counters are a DIFFERENT set of numbers from
        # the live ones above, on purpose: a loss taken on simulated money
        # must never be able to close the gate on real money.
        #
        # Upstream DepthSight keeps the two in separate `TradeStats` objects
        # (upstream `bot_module/risk_manager.py:294`). This fork had
        # collapsed them into one, so a PAPER loss was added to the LIVE
        # `today_pnl` / `consecutive_losses` and `_check_risk_limits()` then
        # closed the LIVE gate on it -- real money blocked by a simulated
        # loss. Everything that touches a counter must now ask which sandbox
        # it is touching (see `_mode_stats`).
        self.paper_stats = TradeStats()
        self.trade_history: List[Dict[str, Any]] = []

        self.telegram_notifier: Optional[Any] = None
        self.loop_from_controller: Optional[asyncio.AbstractEventLoop] = None
        # Extract telegram chat ID from settings if available
        notif_settings = ensure_dict(user_settings.get("notifications"))
        self.user_telegram_chat_id: Optional[str] = notif_settings.get("telegramChatId")

        # Extract telegram chat ID from settings if available
        notif_settings = ensure_dict(user_settings.get("notifications"))
        self.user_telegram_chat_id: Optional[str] = notif_settings.get("telegramChatId")

        # SETTINGS FOR LIVE TRADING (REAL ACCOUNT)
        rm_settings = ensure_dict(user_settings.get("risk_management"))

        self.live_risk_per_trade: float = (
            float(rm_settings.get("riskPerTradePercent") or 0.0) / 100.0
        )
        self.live_max_stop_distance_pct: float = (
            float(rm_settings.get("maxStopDistancePct") or 5.0) / 100.0
        )

        # SETTINGS FOR PAPER/BACKTEST TRADING
        # Use backtest_risk_management, and if it's missing, fall back to live settings
        paper_rm_settings = ensure_dict(
            user_settings.get("backtest_risk_management") or rm_settings
        )

        self.paper_risk_per_trade: float = (
            float(paper_rm_settings.get("riskPerTradePercent") or 1.0) / 100.0
        )
        self.paper_max_stop_distance_pct: float = (
            float(paper_rm_settings.get("maxStopDistancePct") or 5.0) / 100.0
        )

        # Global limits from user settings with a check for None
        def get_fval(d, k, def_val):
            val = d.get(k)
            return float(val) if val is not None else float(def_val)

        def get_ival(d, k, def_val):
            val = d.get(k)
            return int(val) if val is not None else int(def_val)

        self.max_drawdown_threshold: float = (
            get_fval(rm_settings, "maxDrawdown", config.DEFAULT_MAX_DRAWDOWN_PERCENT)
            / 100.0
        )
        self.daily_max_loss_threshold: float = (
            get_fval(
                rm_settings,
                "dailyMaxLossPercent",
                config.DEFAULT_DAILY_MAX_LOSS_PERCENT,
            )
            / 100.0
        )
        self.risk_per_trade: float = (
            get_fval(
                rm_settings,
                "riskPerTradePercent",
                config.DEFAULT_RISK_PER_TRADE_PERCENT,
            )
            / 100.0
        )
        self.max_consecutive_losses: int = get_ival(
            rm_settings, "maxConsecutiveLosses", config.DEFAULT_MAX_CONSECUTIVE_LOSSES
        )

        # THE PAPER SANDBOX'S OWN LIMITS (upstream `risk_manager.py:192`,
        # `:200`, `:208`). Read from the PAPER settings, and DEFAULTING TO THE
        # LIVE VALUES, so the paper gate is never LOOSER than the live one:
        # a user who has configured no paper-specific limits gets exactly the
        # live limits applied to the paper sandbox.
        self.paper_max_drawdown_threshold: float = (
            get_fval(
                paper_rm_settings,
                "maxDrawdown",
                self.max_drawdown_threshold * 100.0,
            )
            / 100.0
        )
        self.paper_daily_max_loss_threshold: float = (
            get_fval(
                paper_rm_settings,
                "dailyMaxLossPercent",
                self.daily_max_loss_threshold * 100.0,
            )
            / 100.0
        )
        self.paper_max_consecutive_losses: int = get_ival(
            paper_rm_settings,
            "maxConsecutiveLosses",
            self.max_consecutive_losses,
        )

        # Maximum number of simultaneous trades
        self.max_concurrent_trades: int = get_ival(
            rm_settings,
            "maxConcurrentTrades",
            getattr(config, "DEFAULT_MAX_CONCURRENT_TRADES", 5),
        )

        self.min_balance_threshold: float = (
            config.MIN_BALANCE_THRESHOLD_USD
        )  # Remains global
        # Cache for blacklist notifications to avoid spamming (symbol -> timestamp)
        self.last_blacklist_notification: Dict[str, float] = {}

        self.min_rr_ratio: float = get_fval(
            rm_settings, "minRrRatio", getattr(config, "RISK_MANAGER_MIN_RR_RATIO", 3.0)
        )
        self.max_stop_distance_pct: float = (
            get_fval(
                rm_settings,
                "maxStopDistancePct",
                config.RISK_MANAGER_MAX_STOP_DISTANCE_PCT,
            )
            / 100.0
        )

        # Adaptive risk manager settings from user settings
        self._strategy_symbol_adjustment_enabled: bool = rm_settings.get(
            "strategySymbolAdjustmentEnabled", False
        )
        self._strategy_symbol_window_size: int = get_ival(
            rm_settings, "strategySymbolWindowSize", 20
        )
        self._strategy_symbol_min_trades_assess: int = get_ival(
            rm_settings, "strategySymbolMinTradesForAssessment", 10
        )
        self._strategy_symbol_pnl_thresh_pct: float = (
            get_fval(rm_settings, "strategySymbolPnlThresholdPct", -10.0) / 100.0
        )
        self._strategy_symbol_wr_thresh_pct: float = get_fval(
            rm_settings, "strategySymbolWinRateThresholdPct", 30.0
        )
        self._strategy_symbol_max_consec_loss: int = get_ival(
            rm_settings, "strategySymbolMaxConsecutiveLosses", 5
        )
        self._strategy_symbol_rec_consec_wins: int = get_ival(
            rm_settings, "strategySymbolRecoveryConsecutiveWins", 3
        )
        self._strategy_symbol_rec_pnl_thresh_pct: float = (
            get_fval(rm_settings, "strategySymbolRecoveryPnlThresholdPct", 50.0) / 100.0
        )
        self._strategy_symbol_cooldown_penalty_sec: int = get_ival(
            rm_settings, "strategySymbolCooldownAfterPenaltySeconds", 3600
        )

        # Risk multipliers remain in the global config, not configured by the user
        self._strategy_symbol_risk_multipliers: List[float] = getattr(
            config, "STRATEGY_SYMBOL_RISK_MULTIPLIERS", [0.5, 0.75, 1.0, 1.25, 1.5]
        )
        self._s_s_default_multiplier_value: float = getattr(
            config, "STRATEGY_SYMBOL_DEFAULT_RISK_MULTIPLIER_VALUE", 1.0
        )

        try:
            self._s_s_default_risk_idx = self._strategy_symbol_risk_multipliers.index(
                self._s_s_default_multiplier_value
            )
        except (ValueError, AttributeError):
            logger.error(
                f"Default risk multiplier {self._s_s_default_multiplier_value} issue. Using middle index or 0."
            )
            self._s_s_default_risk_idx = (
                len(self._strategy_symbol_risk_multipliers) // 2
                if self._strategy_symbol_risk_multipliers
                else 0
            )

        self._is_trading_allowed = True
        # THE PAPER SANDBOX'S OWN GATE (upstream `risk_manager.py:295`).
        # Deliberately separate from `_is_trading_allowed`: the two sandboxes
        # are judged independently, so a breach in one never closes the
        # other's gate.
        self._paper_is_trading_allowed = True
        # Has the PAPER wallet's balance been read in THIS process? Until it
        # has, the paper limits cannot be judged at all: the balance is
        # UNKNOWN, not zero. Evaluating them against an unknown balance
        # closes the paper gate for the wrong reason, and since the
        # controller consults the paper gate BEFORE the balance is refreshed,
        # that would stop paper trading until the next restart.
        self._paper_balance_known = False
        # Why the PAPER gate is off, when it is off for a reason other than
        # the emergency stop. The live equivalent is `_global_gate_disable_reason`.
        self._paper_global_gate_disable_reason: Optional[str] = None
        # Latching emergency stop. Deliberately separate from
        # _is_trading_allowed, which both the daily-stat reset and
        # _check_risk_limits() legitimately flip back to True on their own.
        # This latch is only cleared by an explicit clear_emergency_stop().
        self._emergency_stop_active = False
        self._emergency_stop_reason: Optional[str] = None
        # Was this latch engaged by an OPERATOR (the EMERGENCY_STOP command, or
        # a stop restored from Redis) or by an INFRASTRUCTURE failure (the
        # start-up fail-closed branches, where Redis could not be read and the
        # controller therefore could not prove the latch was clear)?
        #
        # The two are NOT the same thing and must not share semantics. An
        # operator stop is a decision and may only be released deliberately. A
        # start-up verification failure is a Redis blip: it says nothing about
        # what the operator wanted, and latching it as a kill switch silently
        # stops all trading until the next restart. See
        # TradingController._maybe_release_unverified_emergency_stop().
        self._emergency_stop_operator_initiated: bool = False
        # True when the TRADING-GUARD state (the loss streak, the day's PnL)
        # could NOT be read at start-up.
        #
        # While this is set the in-memory counters are NOT trustworthy: the
        # process was born with a zero streak because the persisted state
        # could not be fetched, and a zero streak reads as "no losses" -- the
        # exact state that un-blocks a correctly blocked account. The gate
        # therefore stays CLOSED until the state can be read again
        # (RiskManager._reverify_guard_state).
        self._guard_state_unverified: bool = False
        # True once a persisted trading-guard state has been APPLIED to this
        # process. `initialize_balance()` re-baselines the trading day, and
        # doing that after the restore would wipe the restored loss streak --
        # so the re-baseline is skipped while this is set. It makes the
        # guarantee hold regardless of the order in which the controller
        # calls the two, instead of relying on one particular order.
        self._guard_state_restored: bool = False
        # True when the persisted guard state has VANISHED for a controller that
        # has traded before (see `_guard_seen_redis_key`). This is deliberately
        # NOT `_guard_state_unverified`: that latch means "Redis could not be
        # read" and releases itself on the next successful read, whereas a lost
        # state can never be read back -- it is gone. Re-reading would keep
        # finding nothing and the auto-release would quietly re-open the gate
        # on an account whose loss streak is still unknown.
        #
        # So this latch heals at the UTC day rollover instead, which is the one
        # moment the day's PnL and streak legitimately become unknowable without
        # mattering: the next day starts them at zero by definition. No human
        # decision is required, so this does not need to be an operator's.
        self._guard_state_lost: bool = False
        # The UTC day the state was lost on. The latch may only be released on
        # a STRICTLY LATER day, and a fresh process always sees its first day
        # key change (its in-memory `last_known_day_str` starts empty), so
        # without this the latch would release on the very same start that set
        # it and never mean anything.
        self._guard_state_lost_day_str: str = ""
        # Has this controller EVER traded? True / False / None (unknown).
        #
        # It decides whether the live balance may be installed as today's
        # OPENING balance. A balance that has never moved cannot contain
        # today's PnL, so for an account that has NEVER traded it IS the
        # opening balance. A MID-DAY balance DOES contain today's PnL, so
        # installing it measures the day's loss against a denominator that
        # already contains it -- the gate reads LOW and under-trips.
        #
        # It is resolved from the persisted state (a guard payload, or the
        # 'has traded before' marker) BEFORE the first balance read, because
        # after the read the balance may already have been captured.
        # `None` means UNKNOWN and must never be read as "never traded".
        self._account_has_traded_before: Optional[bool] = None
        # Whether the question above has been answered yet. A failed read
        # leaves the answer unknown and is retried on the next attempt
        # rather than cached as "never traded".
        self._account_history_resolved: bool = False
        # HOW the opening balance in force was obtained:
        #   "rollover"   -- captured at a genuine UTC day transition, where the
        #                    balance genuinely IS the opening balance;
        #   "first_boot" -- captured for an account that has never traded, so
        #                    its balance cannot contain today's PnL;
        #   "restored"   -- restored from a persisted payload that says the
        #                    base belongs to today;
        #   None         -- NOT captured by this process (set directly, or
        #                    unknown): the base is UNBACKED, so a payload from a
        #                    previous day invalidates it.
        #
        # It matters because a previous-day payload proves the account has
        # traded, which makes a live-balance base suspect -- but NOT a base
        # that was captured at the rollover (the balance was the opening one
        # by definition) or as a first boot (the balance cannot contain PnL
        # that was never earned). Invalidating those would block a healthy
        # account for a whole day.
        self._start_of_day_base_provenance: Optional[str] = None
        # Serialises the read-snapshot + write of the guard state, so a
        # periodic tick and a trade handler cannot interleave and leave the
        # OLDER snapshot on disk last.
        self._risk_state_write_lock = asyncio.Lock()
        # WHY the trading gate is closed, in words an operator can act on.
        #
        # `is_symbol_trading_allowed()` used to return a bare False, so a
        # rejection was logged as "Signal REJECTED by rm.is_symbol_trading_allowed
        # (general block)" -- four completely different causes, one
        # indistinguishable message. On 2026-10-19 that cost a full trading day:
        # the gate was closed by the emergency-stop latch and the operator was
        # told "general block".
        #
        # This holds the reason for the MOST RECENT refusal. It is not cleared
        # on success, so a concurrent successful check cannot blank the reason
        # a blocked caller is about to log.
        self._trading_disabled_reason: Optional[str] = None
        # Why the GLOBAL flag (`_is_trading_allowed`) is off, when it is off for
        # a reason other than the emergency stop (e.g. "Max drawdown (12.34%) >=
        # limit (10.00%)"). The risk limits are computed in _check_risk_limits()
        # and recorded here so the gate can name the number that tripped.
        self._global_gate_disable_reason: Optional[str] = None
        self._balance_lock = asyncio.Lock()
        self._reset_time_utc = dt_time(0, 1, 0, tzinfo=timezone.utc)

        # defaultdict will use the updated _strategy_symbol_window_size
        self._symbol_strategy_performance: Dict[
            Tuple[str, str], SymbolStrategyPerformanceStats
        ] = defaultdict(
            lambda: SymbolStrategyPerformanceStats(
                trade_results_buffer=deque(maxlen=self._strategy_symbol_window_size),
                current_risk_multiplier_index=self._s_s_default_risk_idx,
            )
        )

        # AUTO-BLACKLIST: Tracking consecutive stops by symbols
        # Store a list of stop timestamps (to support within_period)
        self._symbol_stop_timestamps: Dict[str, List[float]] = {}

        # Caching auto-blacklist rules from settings
        rm_settings = user_settings.get("risk_management", {}) if user_settings else {}
        blacklist_settings = rm_settings.get("blacklist", {}) if rm_settings else {}
        self._auto_blacklist_rules: List[Dict] = (
            blacklist_settings.get("autoRules", []) if blacklist_settings else []
        )
        logger.info(
            f"RiskManager initialized with {len(self._auto_blacklist_rules)} auto-blacklist rules"
        )

        # Redis (no changes)
        # The key namespace is assigned BEFORE the client is created, so it
        # exists even when the client cannot be: the guard-state code paths
        # must always be able to NAME the key they failed on.
        #
        # This is the DEDICATED guard-state key, NOT
        # REDIS_STATE_KEY_PORTFOLIO. The portfolio key is the published view
        # of the account and PR #25 fixed the API's reading of it, so its
        # shape must not change.
        self.redis_state_key = config.REDIS_STATE_KEY_RISK
        try:
            self.redis_client = redis_asyncio.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_DB,
                username=config.REDIS_USERNAME,
                password=config.REDIS_PASSWORD,
                decode_responses=True,
            )
            logger.info("RiskManager initialized Redis client for state publishing.")
        except Exception as e:
            logger.error(f"Failed to initialize Redis client in RiskManager: {e}")
            self.redis_client = None

        logger.info(f"RiskManager initialized for user_id: {self.user_id}.")
        if self._strategy_symbol_adjustment_enabled:
            logger.info("Strategy-Symbol dynamic risk adjustment ENABLED.")

    # ------------------------------------------------------------------
    # TRADING-MODE SELECTORS.
    #
    # Ported by hand from upstream DepthSight (`bot_module/risk_manager.py`
    # `:351`-`:382`): upstream already separated the paper sandbox from the
    # live one, and this fork had lost that separation. Everything that
    # reads or writes a counter, a threshold or a gate flag goes through
    # these helpers, so there is exactly ONE place that decides which
    # sandbox a value belongs to.
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_mode(mode: Optional[str]) -> str:
        """Trading-mode gate selector: anything but an explicit 'paper' is live."""
        return "paper" if str(mode or "live").lower() == "paper" else "live"

    def _mode_stats(self, mode: Optional[str]) -> TradeStats:
        """The stats object that owns this mode's counters.

        Upstream `risk_manager.py:356`-`:357`. The PAPER sandbox's counters
        live in `self.paper_stats`; the LIVE ones in `self.stats`.
        """
        return (
            self.paper_stats
            if self._normalize_mode(mode) == "paper"
            else self.stats
        )

    def _mode_thresholds(self, mode: Optional[str]) -> Tuple[float, float, int]:
        """(max_drawdown, daily_max_loss, max_consecutive_losses) for the mode.

        Upstream `risk_manager.py:359`-`:370`. The paper sandbox has its own
        limits (defaulting to the live ones), so the two sandboxes are never
        measured against each other's thresholds.
        """
        if self._normalize_mode(mode) == "paper":
            return (
                self.paper_max_drawdown_threshold,
                self.paper_daily_max_loss_threshold,
                self.paper_max_consecutive_losses,
            )
        return (
            self.max_drawdown_threshold,
            self.daily_max_loss_threshold,
            self.max_consecutive_losses,
        )

    def _is_mode_trading_allowed(self, mode: Optional[str]) -> bool:
        """Is TRADING allowed in this mode? The two gates are independent."""
        if self._normalize_mode(mode) == "paper":
            return self._paper_is_trading_allowed
        return self._is_trading_allowed

    def _mode_disable_reason(self, mode: Optional[str]) -> Optional[str]:
        """Why this mode's gate is off, when it is off."""
        if self._normalize_mode(mode) == "paper":
            return self._paper_global_gate_disable_reason
        return self._global_gate_disable_reason

    def apply_user_settings(self, user_settings: Dict[str, Any]) -> None:
        """Applies fresh user settings in-place without recreating RiskManager."""
        settings = ensure_dict(user_settings)
        notif_settings = ensure_dict(settings.get("notifications"))
        rm_settings = ensure_dict(settings.get("risk_management"))
        paper_rm_settings = ensure_dict(
            settings.get("backtest_risk_management") or rm_settings
        )

        def _get_any(source: Dict[str, Any], *keys: str, default=None):
            for key in keys:
                if key in source and source.get(key) is not None:
                    return source.get(key)
            return default

        def _get_float(source: Dict[str, Any], *keys: str, default: float) -> float:
            value = _get_any(source, *keys, default=default)
            return float(value) if value is not None else float(default)

        def _get_int(source: Dict[str, Any], *keys: str, default: int) -> int:
            value = _get_any(source, *keys, default=default)
            return int(value) if value is not None else int(default)

        prev_window_size = getattr(self, "_strategy_symbol_window_size", None)

        if "telegramChatId" in notif_settings or "telegram_chat_id" in notif_settings:
            self.user_telegram_chat_id = _get_any(
                notif_settings, "telegramChatId", "telegram_chat_id"
            )

        self.live_risk_per_trade = (
            _get_float(
                rm_settings,
                "riskPerTradePercent",
                "risk_per_trade_percent",
                default=1.0,
            )
            / 100.0
        )
        self.live_max_stop_distance_pct = (
            _get_float(
                rm_settings, "maxStopDistancePct", "max_stop_distance_pct", default=5.0
            )
            / 100.0
        )

        self.paper_risk_per_trade = (
            _get_float(
                paper_rm_settings,
                "riskPerTradePercent",
                "risk_per_trade_percent",
                default=1.0,
            )
            / 100.0
        )
        self.paper_max_stop_distance_pct = (
            _get_float(
                paper_rm_settings,
                "maxStopDistancePct",
                "max_stop_distance_pct",
                default=5.0,
            )
            / 100.0
        )

        self.max_drawdown_threshold = (
            _get_float(
                rm_settings,
                "maxDrawdown",
                "max_drawdown",
                default=config.DEFAULT_MAX_DRAWDOWN_PERCENT,
            )
            / 100.0
        )
        self.daily_max_loss_threshold = (
            _get_float(
                rm_settings,
                "dailyMaxLossPercent",
                "daily_max_loss_percent",
                default=config.DEFAULT_DAILY_MAX_LOSS_PERCENT,
            )
            / 100.0
        )
        self.risk_per_trade = (
            _get_float(
                rm_settings,
                "riskPerTradePercent",
                "risk_per_trade_percent",
                default=config.DEFAULT_RISK_PER_TRADE_PERCENT,
            )
            / 100.0
        )
        self.max_consecutive_losses = _get_int(
            rm_settings,
            "maxConsecutiveLosses",
            "max_consecutive_losses",
            default=config.DEFAULT_MAX_CONSECUTIVE_LOSSES,
        )
        # The PAPER sandbox's own limits, refreshed with the rest of the
        # settings and DEFAULTING TO THE LIVE VALUES, so a settings refresh
        # can never leave the paper gate looser than the live one.
        self.paper_max_drawdown_threshold = (
            _get_float(
                paper_rm_settings,
                "maxDrawdown",
                "max_drawdown",
                default=self.max_drawdown_threshold * 100.0,
            )
            / 100.0
        )
        self.paper_daily_max_loss_threshold = (
            _get_float(
                paper_rm_settings,
                "dailyMaxLossPercent",
                "daily_max_loss_percent",
                default=self.daily_max_loss_threshold * 100.0,
            )
            / 100.0
        )
        self.paper_max_consecutive_losses = _get_int(
            paper_rm_settings,
            "maxConsecutiveLosses",
            "max_consecutive_losses",
            default=self.max_consecutive_losses,
        )
        self.max_concurrent_trades = _get_int(
            rm_settings,
            "maxConcurrentTrades",
            "max_concurrent_trades",
            default=getattr(config, "DEFAULT_MAX_CONCURRENT_TRADES", 5),
        )
        self.min_rr_ratio = _get_float(
            rm_settings,
            "minRrRatio",
            "min_rr_ratio",
            default=getattr(config, "RISK_MANAGER_MIN_RR_RATIO", 3.0),
        )
        self.max_stop_distance_pct = (
            _get_float(
                rm_settings,
                "maxStopDistancePct",
                "max_stop_distance_pct",
                default=config.RISK_MANAGER_MAX_STOP_DISTANCE_PCT,
            )
            / 100.0
        )

        self._strategy_symbol_adjustment_enabled = bool(
            _get_any(
                rm_settings,
                "strategySymbolAdjustmentEnabled",
                "strategy_symbol_adjustment_enabled",
                default=False,
            )
        )
        self._strategy_symbol_window_size = _get_int(
            rm_settings,
            "strategySymbolWindowSize",
            "strategy_symbol_window_size",
            default=20,
        )
        self._strategy_symbol_min_trades_assess = _get_int(
            rm_settings,
            "strategySymbolMinTradesForAssessment",
            "strategy_symbol_min_trades_for_assessment",
            default=10,
        )
        self._strategy_symbol_pnl_thresh_pct = (
            _get_float(
                rm_settings,
                "strategySymbolPnlThresholdPct",
                "strategy_symbol_pnl_threshold_pct",
                default=-10.0,
            )
            / 100.0
        )
        self._strategy_symbol_wr_thresh_pct = _get_float(
            rm_settings,
            "strategySymbolWinRateThresholdPct",
            "strategy_symbol_win_rate_threshold_pct",
            default=30.0,
        )
        self._strategy_symbol_max_consec_loss = _get_int(
            rm_settings,
            "strategySymbolMaxConsecutiveLosses",
            "strategy_symbol_max_consecutive_losses",
            default=5,
        )
        self._strategy_symbol_rec_consec_wins = _get_int(
            rm_settings,
            "strategySymbolRecoveryConsecutiveWins",
            "strategy_symbol_recovery_consecutive_wins",
            default=3,
        )
        self._strategy_symbol_rec_pnl_thresh_pct = (
            _get_float(
                rm_settings,
                "strategySymbolRecoveryPnlThresholdPct",
                "strategy_symbol_recovery_pnl_threshold_pct",
                default=50.0,
            )
            / 100.0
        )
        self._strategy_symbol_cooldown_penalty_sec = _get_int(
            rm_settings,
            "strategySymbolCooldownAfterPenaltySeconds",
            "strategy_symbol_cooldown_after_penalty_seconds",
            default=3600,
        )

        blacklist_settings = ensure_dict(_get_any(rm_settings, "blacklist", default={}))
        auto_blacklist_rules = _get_any(
            blacklist_settings, "autoRules", "auto_rules", default=[]
        )
        self._auto_blacklist_rules = (
            list(auto_blacklist_rules) if isinstance(auto_blacklist_rules, list) else []
        )

        if hasattr(self, "_symbol_strategy_performance"):
            default_risk_idx = getattr(self, "_s_s_default_risk_idx", 0)
            self._symbol_strategy_performance.default_factory = lambda: (
                SymbolStrategyPerformanceStats(
                    trade_results_buffer=deque(
                        maxlen=self._strategy_symbol_window_size
                    ),
                    current_risk_multiplier_index=default_risk_idx,
                )
            )
            if (
                prev_window_size
                and prev_window_size != self._strategy_symbol_window_size
            ):
                for stats in self._symbol_strategy_performance.values():
                    resized_buffer = deque(
                        list(stats.trade_results_buffer)[
                            -self._strategy_symbol_window_size :
                        ],
                        maxlen=self._strategy_symbol_window_size,
                    )
                    stats.trade_results_buffer = resized_buffer
                    stats.current_pnl_sum_usd = sum(pnl for pnl, _ in resized_buffer)
                    stats.sum_initial_risk_usd_in_window = sum(
                        risk for _, risk in resized_buffer
                    )
                    stats.current_wins_in_window = sum(
                        1 for pnl, _ in resized_buffer if pnl > 0
                    )
                    stats.current_trades_in_window = len(resized_buffer)

        logger.info(
            "RiskManager settings refreshed for user_id=%s: max_concurrent_trades=%s, risk_per_trade=%.4f",
            self.user_id,
            self.max_concurrent_trades,
            self.risk_per_trade,
        )

    async def initialize(self):
        """Asynchronously loads state from the DB and initializes the balance."""
        logger.info(f"Async initializing RiskManager for user_id: {self.user_id}...")
        # Clear state in memory before loading to ensure a clean start
        self._symbol_strategy_performance.clear()
        await self._load_performance_from_db()
        await self.initialize_balance()
        logger.info(f"Async initialization complete for user_id: {self.user_id}.")

    async def _initialize_paper_balance(self) -> None:
        """Start-up for a keyless PAPER account (no live executor).

        The paper wallet's balance is the account's balance, the paper
        counters are the account's counters and the PAPER gate is the
        account's gate. The live start-up machinery is NOT run here: it
        exists to install the LIVE opening balance and to re-baseline the
        LIVE day, and neither has anything to do with paper money.
        """
        success_fetch = await self.update_balance(mode="paper")
        if not success_fetch:
            logger.error(
                "Failed to initialize the PAPER balance. Paper trading is disabled."
            )
            self._paper_is_trading_allowed = False
            self._paper_global_gate_disable_reason = (
                "paper balance could not be fetched at start-up — "
                "paper trading disabled"
            )
            return
        self._check_and_reset_paper_daily_stats()
        self._anchor_paper_start_of_day_base(self.paper_stats.current_balance)
        self._check_risk_limits(mode="paper")

    async def initialize_balance(self):
        logger.info("Initializing balance for RiskManager...")
        # A keyless PAPER account has no live executor (bot_runner.py:563
        # passes `executor=None, paper_executor=paper_executor`), so the
        # paper executor IS its account. Its balance, its counters and its
        # gate belong to the PAPER sandbox: the LIVE start-up machinery
        # below installs the LIVE opening balance and re-baselines the LIVE
        # day, and running it against paper money would put the paper
        # wallet's balance into the live sandbox's base.
        if self.executor is None:
            await self._initialize_paper_balance()
            return
        # BEFORE the first balance read: whether this account has EVER traded
        # decides whether the balance we are about to read may be installed as
        # today's OPENING balance. Resolving it after the read would be too
        # late -- the balance may already have been captured.
        await self._resolve_has_traded_before()
        success_fetch = await self.update_balance(mode="live")

        if success_fetch and self.stats.current_balance > 0:
            # The logic for resetting daily statistics remains the same, as it does not depend on the user
            # It is important that we no longer check _state_loaded_successfully, as loading from the DB is now a standard step
            (
                current_trading_day_start_ts_float_actual,
                current_trading_day_str_utc_actual,
            ) = self._get_current_day_start_info()

            logger.info(
                f"Initializing for a new trading day scenario. ActualCurrentDay: {current_trading_day_str_utc_actual}"
            )

            guard_state_unverifiable = bool(
                getattr(self, "_guard_state_unverified", False)
            )
            guard_state_restored_today = bool(
                getattr(self, "_guard_state_restored", False)
                and self.stats.last_known_day_str == current_trading_day_str_utc_actual
            )
            # A LOST state is unknown in exactly the same way an unreadable
            # one is: the counters are not zero, they are UNKNOWN. They must
            # not be re-baselined, and the gate must not be re-opened.
            guard_state_lost = bool(getattr(self, "_guard_state_lost", False))

            # The opening balance is re-derived on its OWN condition: it must
            # belong to the day we are initializing FOR. It is deliberately NOT
            # gated on the restore having happened, because the two are
            # different questions and coupling them is what let a base captured
            # on one day stay in force for the life of the account.
            #
            # `_apply_persisted_guard_state` restores the base only when the
            # payload says which day the base belongs to and that day is today,
            # so this block is normally a no-op after a same-day restore -- and
            # it is what CORRECTS the base when the restored one was stale.
            base_is_current_day = (
                self.stats.start_of_day_day_str == current_trading_day_str_utc_actual
                and self.stats.start_of_day_balance > 0
            )
            if not base_is_current_day:
                # TODAY'S OPENING BALANCE IS UNKNOWN -- AND IT MUST NOT BE
                # GUESSED.
                #
                # `update_balance()` runs BEFORE this block, so the UTC
                # rollover has already had its chance: if it fired, it wrote
                # and stamped today's base and `base_is_current_day` is
                # True. Reaching this branch therefore means EITHER the
                # rollover did not fire -- we are mid-day, and today's opening
                # balance is genuinely unknown (a base captured on another day
                # was refused by `_apply_persisted_guard_state`, or there was
                # no base at all) -- OR this process observed NO UTC day
                # transition at all: its first observation of the day is not a
                # transition, so it never had a moment at which the balance
                # was known to be the opening one. Both are the same fact from
                # the gate's point of view: today's opening balance is UNKNOWN.
                #
                # The live balance is a MID-DAY balance: it already contains
                # whatever the account won or lost today. Writing it as
                # today's OPENING balance restates the day's loss against a
                # denominator that already contains it, which reads LOW and
                # under-trips the daily-loss gate -- the same shape of defect
                # as a base captured on another day, wearing a different hat.
                #
                # So the base is left UNUSABLE and the gate fails closed with
                # `RISK_GUARD_BASE_UNKNOWN_REASON`. Today's opening balance
                # becomes knowable at the true UTC rollover, where the
                # balance genuinely IS the opening balance.
                logger.error(
                    "[RiskGuard] Today's opening balance is UNKNOWN for %s and is "
                    "NOT guessed: the live balance ($%.2f) is a MID-DAY balance "
                    "that already contains today's PnL, so using it as the "
                    "opening balance would measure the day's loss against a "
                    "denominator that contains it and read LOW. The daily-loss "
                    "and drawdown gates fail CLOSED until the UTC rollover "
                    "captures the real opening balance.",
                    current_trading_day_str_utc_actual,
                    self.stats.current_balance,
                )
                # Leave the base unusable: a base captured on another day must
                # not linger where something could read it, and an absent
                # base must not be filled with a guess. `_daily_loss_base_is_usable()`
                # therefore returns False and `_check_risk_limits()` closes the
                # gate with `RISK_GUARD_BASE_UNKNOWN_REASON`.
                self.stats.start_of_day_balance = 0.0
                self.stats.start_of_day_day_str = ""

            if (
                guard_state_unverifiable
                or guard_state_restored_today
                or guard_state_lost
            ):
                # The trading-guard state was restored from Redis on this very
                # start, OR it could not be read at all. Re-baselining the
                # trading day here would WIPE the restored loss streak and the
                # day's PnL -- which is the defect the restore exists to fix,
                # reintroduced one layer down. And in the UNVERIFIED case it
                # would re-open the gate, which is exactly what the
                # fail-closed rule forbids.
                #
                # This branch runs on EVERY successful initialize_balance()
                # call, not only on a real day change, and the controller
                # calls initialize_balance() on the same start as the
                # restore. The guard state therefore has to survive it --
                # making the invariant hold regardless of the call order
                # rather than relying on one.
                #
                # The gate is NOT re-opened either: it must reflect the
                # restored streak. update_balance() above already
                # re-evaluated the limits against the fresh balance.
                #
                # Note that this protects the COUNTERS, not the base: the base
                # is settled above, on its own day condition, so a stale base
                # cannot survive here by riding on a restored streak.
                logger.info(
                    f"Trading day NOT re-baselined: the trading-guard state "
                    f"(unverifiable={guard_state_unverifiable}, "
                    f"restored_today={guard_state_restored_today}) "
                    f"must not be reset for {current_trading_day_str_utc_actual}. "
                    f"Start of day balance stays at "
                    f"${self.stats.start_of_day_balance:.2f}."
                )
            else:
                self.stats.today_pnl = 0.0
                self.stats.consecutive_losses = 0
                self.stats.current_trading_day_start_ts = (
                    current_trading_day_start_ts_float_actual
                )
                self.stats.last_known_day_str = current_trading_day_str_utc_actual
                # Never re-open the global gate while the emergency-stop latch is
                # engaged. That latch is the operator's kill switch and is released
                # only by clear_emergency_stop(). Entries were already refused
                # correctly (the latch is checked first), but this flag is what the
                # controller publishes and the API now surfaces, so leaving it True
                # would report "trading allowed" during a kill switch.
                if (
                    not self._emergency_stop_active
                    and not self._guard_state_is_unknown()
                ):
                    self._is_trading_allowed = True
                    self._global_gate_disable_reason = None

                logger.info(
                    f"Daily stats reset for new day {current_trading_day_str_utc_actual}. Start of day balance: ${self.stats.start_of_day_balance:.2f}."
                )
        else:
            logger.error(
                f"Failed to initialize balance (FetchSuccess={success_fetch}, BalanceFromExecutor={self.stats.current_balance}). Trading may be disabled."
            )
            self._is_trading_allowed = False
            self._global_gate_disable_reason = (
                "balance could not be fetched at start-up — trading disabled"
            )
            self._trading_disabled_reason = self._global_gate_disable_reason

        if success_fetch:
            self._check_risk_limits()
        else:
            self._is_trading_allowed = False
            self._global_gate_disable_reason = (
                "balance could not be fetched at start-up — trading disabled"
            )
            self._trading_disabled_reason = self._global_gate_disable_reason
            logger.warning(
                "[InitializeBalance] Trading disabled: failed to fetch balance."
            )

    async def update_balance(self, mode: Optional[str] = None) -> bool:
        """Updates the current USDT balance from the executor (thread-safe).

        `mode` says which sandbox the balance belongs to. It defaults to
        the sandbox of the BALANCE SOURCE: a keyless PAPER account has no
        live executor, so the paper executor IS its account and its balance
        belongs to the paper sandbox. A dual-mode account has a live
        executor, so its balance is the LIVE balance.
        """
        log_prefix = "[RiskManager:UpdateBalance]"
        if mode is None:
            mode = "live" if self.executor is not None else "paper"
        mode = self._normalize_mode(mode)
        stats = self._mode_stats(mode)
        async with self._balance_lock:
            try:
                # A paper-only account (no API key) is constructed with
                # executor=None BY DESIGN -- bot_runner.py:563 passes
                # `executor=None, paper_executor=paper_executor`.
                #
                # Reading ONLY self.executor meant every keyless paper account
                # raised "'NoneType' object has no attribute 'get_account_balance'"
                # at startup, so success_fetch was False, so
                # initialize_balance() set _is_trading_allowed = False -- and
                # nothing ever set it back. Every subsequent signal was dropped
                # with "Signal REJECTED by rm.is_symbol_trading_allowed (general
                # block)" once per candle. That is the whole "it says trade is
                # running but balance stays at 10,000" report: trading was
                # disabled globally before the first bar.
                #
                # The paper executor is the real balance source for those
                # accounts; it was being passed in and then ignored.
                #
                # The source follows the SANDBOX: the live sandbox's balance
                # comes from the live executor and the paper sandbox's from
                # the paper executor. With the default mode this is exactly
                # the old rule -- a keyless paper account has no live
                # executor, so its mode is "paper" and the paper executor is
                # its account.
                source = self.executor if mode == "live" else self.paper_executor
                if source is None:
                    logger.error(
                        f"{log_prefix} No {'live' if mode == 'live' else 'paper'} "
                        f"executor available for user_id={self.user_id}; cannot "
                        f"fetch balance."
                    )
                    return False

                balances = await source.get_account_balance()  # GET /account
                if not balances or isinstance(balances, dict) and balances.get("error"):
                    logger.error(
                        f"{log_prefix} Failed to fetch balances from executor: {balances}"
                    )
                    return False  # Failed to get balances

                usdt_balance_data = balances.get("USDT")
                if usdt_balance_data:
                    try:
                        free_bal = float(usdt_balance_data.get("free", 0))
                        locked_bal = float(usdt_balance_data.get("locked", 0))
                        current_total_usdt = free_bal + locked_bal
                    except (ValueError, TypeError) as e:
                        logger.error(
                            f"{log_prefix} Error converting USDT balance data to float: {e}. Data: {usdt_balance_data}"
                        )
                        return False

                    # Non-finite balances must never reach stats. NaN compares
                    # False against every limit below (min balance, the >90%
                    # drop guard, drawdown), so it would sail through all of
                    # them AND poison shared state for the other balance paths.
                    if not math.isfinite(current_total_usdt):
                        logger.error(
                            f"{log_prefix} Non-finite USDT balance from executor "
                            f"({current_total_usdt!r}). Ignoring this update."
                        )
                        return False

                    # Check for abnormally low balance if the previous one was significantly higher
                    # Suppose the balance should not drop by more than 90% in one call if it was not close to zero
                    if (
                        stats.current_balance > 10
                        and current_total_usdt < stats.current_balance * 0.1
                        and current_total_usdt < self.min_balance_threshold / 2
                    ):
                        logger.warning(
                            f"{log_prefix} Detected anomalously low new balance (${current_total_usdt:.2f}) compared to previous (${stats.current_balance:.2f}). "
                            f"This might be a temporary API issue. NOT UPDATING BALANCE THIS TIME."
                        )
                        # Retry logic can be added here or the update can simply be skipped
                        return False  # Return False to signal a possible data issue

                    if (
                        abs(current_total_usdt - stats.current_balance) > 1e-9
                    ):  # 1e-9 for float comparison
                        logger.info(
                            f"{log_prefix} Balance updated: Previous=${stats.current_balance:.2f}, New=${current_total_usdt:.2f} (Free: {free_bal:.2f}, Locked: {locked_bal:.2f})"
                        )
                        stats.current_balance = current_total_usdt
                        if mode == "paper":
                            # The paper sandbox's own opening balance: the
                            # first balance observed for the day IS the
                            # paper wallet's opening balance (upstream
                            # anchors it the same way, upstream
                            # `bot_module/risk_manager.py:1698`).
                            self._paper_balance_known = True
                            self._anchor_paper_start_of_day_base(
                                current_total_usdt
                            )
                    else:
                        logger.debug(
                            f"{log_prefix} Balance unchanged: ${stats.current_balance:.2f}"
                        )

                    # A successful balance read is the only trustworthy evidence
                    # available about whether the global gate should be open, so
                    # re-validate it here rather than only at startup.
                    #
                    # `initialize_balance()` already does this once
                    # (`if success_fetch: self._check_risk_limits()`), but that
                    # is the ONLY place it happened on the paper path. Combined
                    # with `_check_and_reset_daily_stats()` -- which is likewise
                    # only called from `update_trade_result()` and the LIVE
                    # branch of `assess_signal()` -- a paper account that has not
                    # closed a trade never re-evaluated the gate again.
                    #
                    # So a single transient fetch failure at bot start latched
                    # `_is_trading_allowed = False` for the entire life of the
                    # process: the controller's own gate rejects the signal
                    # before `assess_signal()` is reached, so nothing could heal
                    # it. Observed live 2026-10-18 as one
                    # "Signal REJECTED by rm.is_symbol_trading_allowed (general
                    # block)" per minute against a perfectly healthy $2,645.55
                    # paper balance.
                    #
                    # This also closes the mirror hole: without it, an account
                    # that genuinely fell below `min_balance_threshold` was
                    # never re-checked either, so the breach went unseen until
                    # the next trade closed.
                    #
                    # The DAILY ROLLOVER belongs on this same trigger, not
                    # only on a trade close.
                    #
                    # `_check_and_reset_daily_stats()` was reachable from
                    # `update_trade_result()` and from the LIVE branch of
                    # `assess_signal()` -- but NOT from the balance update,
                    # in EITHER mode. The LIVE branch of `assess_signal()`
                    # called it on every signal, so a live account rolled
                    # over at its next signal. The PAPER branch of
                    # `assess_signal()` returns before the live branch's
                    # call, so on the paper path the rollover ran ONLY when a
                    # position closed -- and the daily-loss guard closes the
                    # gate precisely when no position can be opened, so no
                    # close can fire, and the rollover never came. The paper
                    # bot stayed deadlocked (gate shut, no trade, no
                    # rollover) until the process restarted.
                    #
                    # So the gap was the same in both modes -- the rollover
                    # did not ride the balance update -- but only the PAPER
                    # path could DEADLOCK on it, because the live path had a
                    # second, independent trigger. This is NOT a
                    # permissiveness change on the live path: the live
                    # branch of `assess_signal()` already performed the same
                    # daily reset, the rollover is idempotent within a UTC
                    # day, and the emergency-stop latch still blocks the
                    # gate across it.
                    #
                    # It must run BEFORE `_check_risk_limits()` below. The
                    # reset clears the day-scoped counters and re-opens the
                    # gate; the limit check that follows re-detects a breach
                    # that is still in force (a balance below the minimum, for
                    # one) and closes the gate again. The reverse order would
                    # judge the new day against the old day's counters and
                    # leave the gate open past a real breach.
                    #
                    # It is idempotent within a UTC day: the body only runs
                    # when the day key has changed, so running it on this
                    # timer cannot clear a counter inside the same day. The
                    # emergency-stop latch is still checked before the gate is
                    # re-opened.
                    if mode == "paper":
                        self._check_and_reset_paper_daily_stats()
                    else:
                        self._check_and_reset_daily_stats()
                    self._check_risk_limits(mode=mode)
                    return True
                else:
                    logger.warning(
                        f"{log_prefix} USDT balance data not found in account info from executor. Current known balance: ${stats.current_balance:.2f}"
                    )
                    # If there is no USDT but there are other assets, this could also be a problem for a USDT bot.
                    # If self.stats.current_balance is already 0 or very small, then this might be normal.
                    return False  # If there is no USDT, consider the update failed for the USDT bot
            except Exception as e:
                logger.error(
                    f"{log_prefix} Unexpected error updating balance: {e}",
                    exc_info=True,
                )
                return False

    def _get_current_day_start_info(self) -> Tuple[float, str]:
        now_utc = datetime.now(timezone.utc)
        current_day_str = now_utc.strftime("%Y-%m-%d")
        reset_time_today_utc = datetime.combine(now_utc.date(), self._reset_time_utc)
        if now_utc < reset_time_today_utc:
            start_of_trading_day_dt = datetime.combine(
                now_utc.date() - timedelta(days=1), self._reset_time_utc
            )
        else:
            start_of_trading_day_dt = reset_time_today_utc
        return start_of_trading_day_dt.timestamp(), current_day_str

    def _write_start_of_day_base(self, balance: float, day_str: str) -> bool:
        """The ONE writer of the day's opening balance.

        The balance and the UTC day it belongs to are written TOGETHER, so
        the two can never drift apart: a base whose stamp says one day and
        whose amount was captured on another is a base measured against the
        wrong denominator, which reads LOW and under-trips the daily-loss
        gate.

        A base that already belongs to `day_str` is NEVER overwritten.
        Today's opening balance is a fact about the start of the day, and
        restating it from a mid-session balance would silently bake the
        losses already taken today into the denominator -- the exact shape
        of the defect this writer exists to prevent.

        Its ONLY caller is the UTC rollover, which is the one moment the
        number is known to BE the opening balance. Nothing else may write it:
        a balance that is merely CURRENT is a mid-day balance, and it is not
        the day's opening balance.

        A base of exactly 0 is "unknown", not "a real opening balance of
        zero": `_daily_loss_base_is_usable()` treats it as unusable and the
        gate fails closed. That is the safe direction (it never re-opens the
        gate), and it is consistent with the min-balance check, which blocks
        a zero-balance account anyway.

        Returns True when the base was written.
        """
        # A stats object with NO day stamp means the base is not known to
        # belong to today -- that is "unknown", not "already today's", so the
        # guard must not block the write. The attribute is read defensively:
        # the guard-state test doubles model a stats object without it.
        if (
            getattr(self.stats, "start_of_day_day_str", None) == day_str
            and self.stats.start_of_day_balance > 1e-9
        ):
            return False

        self.stats.start_of_day_balance = float(balance)
        self.stats.start_of_day_day_str = day_str
        return True

    async def _resolve_has_traded_before(self) -> Optional[bool]:
        """Has this controller EVER traded?  True / False / None (unknown).

        Two independent pieces of evidence, either of which proves history:

        * a persisted guard payload -- it only exists after the controller
          saved state, which it does on every balance update and on every
          trade result;
        * the 'has traded before' marker -- written under its own key and
          never overwritten.

        `None` means the question could NOT be answered (no Redis client, or
        the read failed). An unknown must never be read as "never traded":
        that is exactly how a MID-DAY balance gets installed as the opening
        balance.

        Failing closed on `None` does NOT brick a new account: a brand-new
        account has neither a payload nor a marker, which is `False`, not
        `None`.
        """
        if getattr(self, "_account_history_resolved", False):
            return getattr(self, "_account_has_traded_before", None)
        if getattr(self, "redis_client", None) is None:
            # No persistence layer at all: there is no persisted state that
            # could prove the account has traded, and none that could
            # contradict a capture either. The answer stays UNKNOWN.
            self._account_has_traded_before = None
            self._account_history_resolved = False
            logger.info(
                "[RiskGuard] History check for user_id=%s: no persistence "
                "layer, so has_traded_before is UNKNOWN (None).",
                self.user_id,
            )
            return None
        payload, error = await self._read_persisted_guard_state()
        if error is not None:
            self._account_has_traded_before = None
            # Not cached: the next attempt retries the read.
            self._account_history_resolved = False
        elif payload is not None:
            self._account_has_traded_before = True
            self._account_history_resolved = True
        else:
            self._account_has_traded_before = await self._has_traded_before()
            self._account_history_resolved = (
                self._account_has_traded_before is not None
            )
        logger.info(
            "[RiskGuard] History check for user_id=%s: has_traded_before=%s "
            "(None = unknown).",
            self.user_id,
            self._account_has_traded_before,
        )
        return self._account_has_traded_before

    def _capture_first_boot_base(self, day_str: str) -> bool:
        """Install the live balance as today's opening balance -- ONLY WHEN NOTHING CONTRADICTS IT.

        The live balance may be today's OPENING balance only when it cannot
        contain today's PnL, or when nothing can prove that it does:

        * the account has NEVER traded -- there is no trade to have won or
          lost anything, so the balance IS the opening balance. This is what
          makes a brand-new account tradable ("fail closed on unknown" must
          not become "brick every new account");
        * the persisted state could not be READ -- the capture is then a
          GUESS, so it is marked UNBACKED and a later previous-day payload
          invalidates it. (The unreadable-state latch holds the gate closed
          in production until the state can be read.)

        The account HAS traded -- a persisted payload exists, or the 'has
        traded before' marker does -- is the case the review panel measured:
        its live balance is a MID-DAY balance that already contains today's
        PnL, and installing it measures the day's loss against a denominator
        that contains it. The panel measured a gate reading 0.0000% where the
        truth was 4.4188%, with trading still ALLOWED. So the capture is
        REFUSED there.
        """
        if getattr(self, "_account_has_traded_before", None) is True:
            # The account has traded: the live balance is a mid-day balance.
            return False
        provenance = (
            "first_boot"
            if getattr(self, "_account_has_traded_before", None) is False
            else "unknown_history"
        )
        if self.stats.current_balance <= 0:
            return False
        if (
            getattr(self.stats, "start_of_day_day_str", None) == day_str
            and self.stats.start_of_day_balance > 1e-9
        ):
            # A base for today is already in force. Today's opening balance is
            # a fact about the start of the day; it is never restated.
            return False
        logger.info(
            "[RiskGuard] Capturing the opening balance $%.8f for UTC day %s "
            "(%s).",
            self.stats.current_balance,
            day_str,
            "the account has never traded, so its balance cannot contain "
            "today's PnL and IS the opening balance"
            if provenance == "first_boot"
            else "the persisted state could not be read, so this capture is a "
            "GUESS and is marked unbacked",
        )
        if self._write_start_of_day_base(self.stats.current_balance, day_str):
            self._start_of_day_base_provenance = provenance
        return True

    def _opening_base_is_unbacked(self) -> bool:
        """Is the opening balance in force a GUESS rather than a number we vouch for?

        A base is BACKED when this process can say WHERE it came from:

        * ``rollover``   -- captured at a genuine UTC day transition, where the
          balance genuinely WAS the opening balance;
        * ``first_boot`` -- the account had never traded, so its balance cannot
          contain today's PnL;
        * ``restored``   -- a payload says this base belongs to today.

        A base is UNBACKED when nothing vouches for it:

        * ``unknown_history`` -- the capture happened because the persisted
          state could not be READ, so the balance may already contain today's
          PnL. It is a guess, and a later payload that proves the account has
          traded must be able to drop it;
        * ``None`` -- no provenance at all (a stats object populated directly).

        Only an UNBACKED base is dropped when the persisted state arrives
        later. Dropping a BACKED one would throw away the day's real opening
        balance and leave the account with no denominator at all.
        """
        provenance = getattr(self, "_start_of_day_base_provenance", None)
        return provenance is None or provenance == "unknown_history"

    def _check_and_reset_daily_stats(self):
        current_start_ts_float, current_day_str_utc = self._get_current_day_start_info()
        if self.stats.last_known_day_str != current_day_str_utc:
            logger.info(
                f"New trading day detected (UTC): {current_day_str_utc}. Previous day: {self.stats.last_known_day_str}"
            )
            # A base may be captured ONLY on a genuine UTC day transition --
            # one this process OBSERVED, i.e. one where it already knew the
            # previous day. An EMPTY `last_known_day_str` means "this process
            # has no known day yet", NOT "the day changed": on a fresh process
            # the live balance is a MID-DAY balance that already contains
            # today's PnL, and installing it as the opening base measures the
            # day's loss against a denominator that contains it -- the same
            # defect as a base captured on another day, wearing a different
            # hat. The base stays UNKNOWN here and the gate fails closed.
            if self.stats.last_known_day_str:
                if self._write_start_of_day_base(
                    self.stats.current_balance, current_day_str_utc
                ):
                    # A genuine UTC transition: the balance WAS the opening
                    # balance at that moment.
                    self._start_of_day_base_provenance = "rollover"
            else:
                logger.error(
                    "[RiskManager:DailyReset] No previous UTC day is known to "
                    f"this process (it started on {current_day_str_utc}), so "
                    "the live balance cannot be told apart from a MID-DAY "
                    "balance. NO opening balance is captured: the base stays "
                    "UNKNOWN and the daily-loss gate fails CLOSED until the "
                    "persisted state proves this account has never traded (a "
                    "first boot) or a real UTC day transition is observed."
                )
            # A lost guard state heals at the UTC rollover and nowhere else.
            # It cannot heal on a re-read (there is nothing left to read), and
            # it must not require an operator: a new UTC day starts the day's
            # PnL and the loss streak at zero by definition, so whatever was
            # lost belonged to the day that just ended.
            if getattr(self, "_guard_state_lost", False):
                # Only a STRICTLY LATER day releases this latch. A fresh
                # process sees its first day-key change on its very first
                # `_check_and_reset_daily_stats()` (its in-memory
                # `last_known_day_str` starts empty), so releasing on any day
                # change would clear the latch on the same start that set it.
                # Day strings are `YYYY-MM-DD`, so lexicographic order is
                # chronological order.
                if getattr(self, "_guard_state_lost_day_str", "") and (
                    current_day_str_utc > self._guard_state_lost_day_str
                ):
                    logger.critical(
                        "[RiskGuard] The trading-guard state was lost on UTC day "
                        f"{self._guard_state_lost_day_str}; the rollover to "
                        f"{current_day_str_utc} resets the day's PnL and the "
                        "loss streak to zero, so the account is no longer "
                        "trading on unknown numbers. Trading is released for "
                        "the new day."
                    )
                    self._guard_state_lost = False
                    self._guard_state_lost_day_str = ""
                else:
                    logger.critical(
                        "[RiskGuard] The trading-guard state is lost and UTC day "
                        f"{current_day_str_utc} is not past "
                        f"{self._guard_state_lost_day_str or 'unknown'}, so "
                        "today's PnL and loss streak are still unknown. "
                        "Trading stays DISABLED until the day rolls over."
                    )
            self.stats.today_pnl = 0.0
            self.stats.consecutive_losses = 0
            self.stats.current_trading_day_start_ts = current_start_ts_float
            self.stats.last_known_day_str = current_day_str_utc
            if not self._is_trading_allowed:
                logger.info(
                    "Global risk limits have been reset. Resuming trading capability for a new day."
                )
            # A new UTC day is not an operator action, so it must not clear a
            # kill switch the operator pulled. Entries stay refused either way
            # (the latch is checked first), but this flag is published to Redis
            # and surfaced by the API, so flipping it here would report
            # "trading allowed" from midnight onward. Found by two reviewers:
            # the _check_risk_limits() re-enable was guarded, this one was not.
            # A new UTC day does not re-open a gate that is held closed
            # because the state is UNKNOWN. The day's counters legitimately
            # restart at zero, but "unknown" is not "within limits", and the
            # lost-state branch above promises exactly that:
            # "Trading stays DISABLED until the day rolls over". Only a real
            # limit evaluation may re-open the gate.
            if not self._emergency_stop_active and not self._guard_state_is_unknown():
                self._is_trading_allowed = True
                self._global_gate_disable_reason = None
            logger.info(
                f"Balance at the start of a new day set: ${self.stats.start_of_day_balance:.2f}"
            )
            if self._strategy_symbol_adjustment_enabled:
                logger.info(
                    "Trading day change. Cooldowns for 'strategy-symbol' pairs may be revised."
                )

        # THE CAPTURE THAT MAY RUN WITHOUT A DAY CHANGE. The live balance may
        # be today's OPENING balance only when nothing proves otherwise (see
        # `_capture_first_boot_base`). It is attempted on every balance update,
        # not only on a day change, because a balance fetch that RECOVERS must
        # be able to re-open the gate -- and the writer refuses to restate a
        # base that already belongs to today.
        self._capture_first_boot_base(current_day_str_utc)

    async def apply_funding_pnl(
        self, symbol: str, funding_pnl: float, mode: str = "live"
    ) -> bool:
        """Record a FUNDING charge against the day's PnL.

        Funding is a CARRY COST, not a trade: the paper wallet is debited (or
        credited) at every settlement boundary, between the entry and the
        close. The daily-loss gate's numerator is the wallet's day PnL, so the
        gate has to see the funding while the position is still open --
        otherwise it measures a loss that has already happened as if it had
        not.

        It is deliberately NOT a trade result: it does not move the
        consecutive-loss streak (which counts losing CLOSES -- a funding
        payment is not a losing trade, and counting it could falsely trip the
        max-consecutive-losses guard) and it is not a trade timestamp.

        `mode` says which sandbox the funding belongs to. A PAPER funding
        charge must land in the PAPER counters: it is simulated money, and
        the LIVE daily-loss gate is what stands between the account and
        real money. The default is "live" so a caller that omits it cannot
        accidentally book a live funding charge into the paper sandbox.

        Returns True when the funding IS in today's PnL. The caller needs
        that distinction: the close subtracts the funding that was ALREADY
        reported, so a funding that never reached this counter must NOT be
        subtracted -- otherwise the close would report it a second time, or
        the gate would never hear about it at all.
        """
        mode = self._normalize_mode(mode)
        stats = self._mode_stats(mode)
        if mode == "paper":
            self._check_and_reset_paper_daily_stats()
        else:
            self._check_and_reset_daily_stats()
        amount = float(funding_pnl or 0.0)
        stats.today_pnl += amount
        logger.info(
            f"[RiskManager:Funding] Funding for {symbol}: "
            f"{amount:+.8f} USDT. "
            f"GlobalTodayPnL={stats.today_pnl:.8f} "
            f"(mode: {mode}) "
            f"(the consecutive-loss streak is unchanged: a funding payment is "
            f"a carry cost, not a losing trade)."
        )
        # FROM HERE THE FUNDING IS IN TODAY'S PnL. Nothing below may raise:
        # a failure would reach the caller as "not applied", and the close
        # would then report the same funding a second time.
        try:
            self._check_risk_limits(mode=mode)
        except Exception as exc:
            logger.error(
                f"[RiskManager:Funding] The limit check failed after the "
                f"funding for {symbol} was applied: {exc}",
                exc_info=True,
            )
        try:
            await self.save_state()
        except Exception as exc:
            logger.error(
                f"[RiskManager:Funding] The trading-guard state could not be "
                f"persisted after the funding for {symbol} was applied: {exc}",
                exc_info=True,
            )
        return True

    async def update_trade_result(
        self,
        symbol: str,
        pnl: float,
        exit_reason: Optional[str] = None,
        mode: str = "live",
    ):
        """Book a CLOSED trade into the sandbox that owns it.

        `mode` says which sandbox the trade belongs to: "live" (real money)
        or "paper" (simulated). Upstream `risk_manager.py:915`-`:936` takes
        the same argument, for the same reason: a PAPER loss must not be
        added to the LIVE counters, because those counters are what
        `_check_risk_limits()` uses to decide whether REAL money may trade.
        The default is "live" so an existing caller that forgets the argument
        cannot accidentally book a loss into the paper sandbox.
        """
        mode = self._normalize_mode(mode)
        stats = self._mode_stats(mode)
        if mode == "paper":
            self._check_and_reset_paper_daily_stats()
        else:
            self._check_and_reset_daily_stats()
        stats.today_pnl += pnl
        stats.last_trade_time = time.time()
        if pnl <= 0:
            stats.consecutive_losses += 1
        else:
            stats.consecutive_losses = 0
        logger.info(
            f"Global PnL Updated: PnL={pnl:.2f}, GlobalTodayPnL={stats.today_pnl:.2f}, GlobalConsecLosses={stats.consecutive_losses} (mode: {mode})"
        )
        self._check_risk_limits(mode=mode)

        # Persist the guard state NOW, not on the controller's next periodic
        # tick: the streak is what closes the gate, so a process that dies
        # between this loss and that tick would restart with a SMALLER
        # streak than the one that closed the gate -- the same hole this
        # whole change exists to close, just smaller.
        await self.save_state()

        # AUTO-BLACKLIST: Tracking consecutive stops by symbols
        # Excluding BE (Break-Even) from stop count for blacklist
        is_be = exit_reason is not None and "be" in exit_reason.lower()
        is_stop_loss = (
            exit_reason is not None and "stop" in exit_reason.lower() and not is_be
        )

        if is_stop_loss:
            # Adding stop timestamp
            if symbol not in self._symbol_stop_timestamps:
                self._symbol_stop_timestamps[symbol] = []
            self._symbol_stop_timestamps[symbol].append(time.time())
            logger.debug(
                f"[AutoBlacklist:{symbol}] Stop recorded. Total stops: {len(self._symbol_stop_timestamps[symbol])}"
            )
        elif pnl > 0 or is_be:
            # On a PROFITABLE trade or BE (break-even), clear the stop history
            if symbol in self._symbol_stop_timestamps:
                self._symbol_stop_timestamps[symbol] = []

        # Checking auto-blocking rules
        await self._check_and_apply_auto_blacklist_rules(symbol)

    async def update_symbol_strategy_performance(
        self,
        symbol: str,
        strategy_name: str,
        pnl_usd: float,
        initial_risk_usd_planned: float,
    ):
        if not self._strategy_symbol_adjustment_enabled:
            return

        perf_key = (symbol, strategy_name)
        stats = self._symbol_strategy_performance[perf_key]
        log_prefix = f"[RiskManager:{symbol}:{strategy_name}:Perf]"

        # Update the cumulative PnL. This is done at the very beginning.
        stats.total_pnl_usd += pnl_usd

        # Sliding window logic (remains unchanged)
        if (
            len(stats.trade_results_buffer) == self._strategy_symbol_window_size
            and self._strategy_symbol_window_size > 0
        ):
            old_pnl, old_risk_planned = stats.trade_results_buffer[0]
            stats.current_pnl_sum_usd -= old_pnl
            stats.sum_initial_risk_usd_in_window -= old_risk_planned
        stats.trade_results_buffer.append((pnl_usd, initial_risk_usd_planned))
        stats.current_pnl_sum_usd = sum(p for p, r in stats.trade_results_buffer)
        stats.sum_initial_risk_usd_in_window = sum(
            r for p, r in stats.trade_results_buffer
        )
        stats.current_wins_in_window = sum(
            1 for p, r in stats.trade_results_buffer if p > 0
        )
        stats.current_trades_in_window = len(stats.trade_results_buffer)

        stats.current_consecutive_losses = 0
        stats.current_consecutive_wins_for_recovery = 0
        counting_losses = True
        counting_wins = True
        for pnl_hist, _ in reversed(stats.trade_results_buffer):
            if counting_losses:
                if pnl_hist <= 0:
                    stats.current_consecutive_losses += 1
                else:
                    counting_losses = False
            if counting_wins:
                if pnl_hist > 0:
                    stats.current_consecutive_wins_for_recovery += 1
                else:
                    counting_wins = False
            if not counting_losses and not counting_wins:
                break
        stats.total_trades_for_assessment += 1
        logger.debug(
            f"{log_prefix} Updated. Total PnL: {stats.total_pnl_usd:.2f}, Window PnL: {stats.current_pnl_sum_usd:.2f}, Trades: {stats.current_trades_in_window}"
        )

        # Step 1: Check and adjust risk
        await self._check_and_adjust_risk_for_symbol_strategy(symbol, strategy_name)

        # Step 2: Save the updated state to the DB
        await self._save_performance_to_db(symbol, strategy_name, stats)

    async def _check_and_adjust_risk_for_symbol_strategy(
        self, symbol: str, strategy_name: str
    ):
        perf_key = (symbol, strategy_name)
        stats = self._symbol_strategy_performance[perf_key]
        log_prefix = f"[RiskManager:{symbol}:{strategy_name}:Adjust]"

        if not self._strategy_symbol_risk_multipliers:
            logger.error(
                f"{log_prefix} Risk multipliers list is empty. Cannot adjust risk."
            )
            return

        max_multiplier_idx = len(self._strategy_symbol_risk_multipliers) - 1

        if stats.total_trades_for_assessment < self._strategy_symbol_min_trades_assess:
            logger.debug(
                f"{log_prefix} Not enough total trades ({stats.total_trades_for_assessment}/{self._strategy_symbol_min_trades_assess}) to assess."
            )
            return

        reduction_triggered = False
        # Risk Reduction Logic
        if (
            stats.current_risk_multiplier_index > 0
        ):  # Only reduce if not already at max penalty (index 0)
            reason_for_reduction = []
            if stats.current_trades_in_window >= self._strategy_symbol_window_size:
                pnl_pct_in_window = (
                    (
                        stats.current_pnl_sum_usd
                        / stats.sum_initial_risk_usd_in_window
                        * 100.0
                    )
                    if stats.sum_initial_risk_usd_in_window > 1e-9
                    else 0.0
                )
                win_rate_in_window = (
                    (
                        stats.current_wins_in_window
                        / stats.current_trades_in_window
                        * 100.0
                    )
                    if stats.current_trades_in_window > 0
                    else 0.0
                )
                if pnl_pct_in_window < self._strategy_symbol_pnl_thresh_pct:
                    reason_for_reduction.append(
                        f"PnL {pnl_pct_in_window:.2f}% < {self._strategy_symbol_pnl_thresh_pct:.2f}%"
                    )
                if win_rate_in_window < self._strategy_symbol_wr_thresh_pct:
                    reason_for_reduction.append(
                        f"WR {win_rate_in_window:.2f}% < {self._strategy_symbol_wr_thresh_pct:.2f}%"
                    )
            if (
                stats.current_consecutive_losses
                >= self._strategy_symbol_max_consec_loss
            ):
                reason_for_reduction.append(
                    f"ConsecLoss {stats.current_consecutive_losses} >= {self._strategy_symbol_max_consec_loss}"
                )

            if reason_for_reduction:
                reduction_triggered = True
                stats.current_risk_multiplier_index = max(
                    0, stats.current_risk_multiplier_index - 1
                )  # Decrease index
                stats.last_penalty_timestamp = time.time()
                stats.current_consecutive_wins_for_recovery = 0
                logger.warning(
                    f"{log_prefix} RISK REDUCED. New multiplier index: {stats.current_risk_multiplier_index} "
                    f"(Value: {self._strategy_symbol_risk_multipliers[stats.current_risk_multiplier_index]:.2f}). "
                    f"Reason(s): {'; '.join(reason_for_reduction)}"
                )

        # Risk Improvement/Enhancement Logic
        if (
            not reduction_triggered
            and stats.current_risk_multiplier_index < max_multiplier_idx
        ):  # Only improve if not at max benefit
            cooldown_passed = (
                time.time() - stats.last_penalty_timestamp
            ) >= self._strategy_symbol_cooldown_penalty_sec

            if cooldown_passed:
                reason_for_improvement = []
                if (
                    stats.current_consecutive_wins_for_recovery
                    >= self._strategy_symbol_rec_consec_wins
                ):
                    reason_for_improvement.append(
                        f"ConsecWinsRec {stats.current_consecutive_wins_for_recovery} >= {self._strategy_symbol_rec_consec_wins}"
                    )
                if stats.current_trades_in_window >= self._strategy_symbol_window_size:
                    pnl_pct_in_window_rec = (
                        (
                            stats.current_pnl_sum_usd
                            / stats.sum_initial_risk_usd_in_window
                            * 100.0
                        )
                        if stats.sum_initial_risk_usd_in_window > 1e-9
                        else 0.0
                    )
                    if pnl_pct_in_window_rec > self._strategy_symbol_rec_pnl_thresh_pct:
                        reason_for_improvement.append(
                            f"PnL {pnl_pct_in_window_rec:.2f}% > {self._strategy_symbol_rec_pnl_thresh_pct:.2f}%"
                        )

                if reason_for_improvement:
                    new_index_before_change = stats.current_risk_multiplier_index
                    stats.current_risk_multiplier_index = min(
                        max_multiplier_idx, stats.current_risk_multiplier_index + 1
                    )  # Increase index
                    stats.current_consecutive_wins_for_recovery = 0

                    log_message_action = (
                        "RISK ENHANCED"
                        if self._strategy_symbol_risk_multipliers[
                            stats.current_risk_multiplier_index
                        ]
                        > self._s_s_default_multiplier_value
                        else "RISK RECOVERED"
                    )
                    if (
                        new_index_before_change < self._s_s_default_risk_idx
                        and stats.current_risk_multiplier_index
                        == self._s_s_default_risk_idx
                    ):
                        log_message_action = "RISK RECOVERED TO NORMAL"

                    logger.info(
                        f"{log_prefix} {log_message_action}. New multiplier index: {stats.current_risk_multiplier_index} "
                        f"(Value: {self._strategy_symbol_risk_multipliers[stats.current_risk_multiplier_index]:.2f}). "
                        f"Reason(s): {'; '.join(reason_for_improvement)}"
                    )
            else:
                logger.debug(
                    f"{log_prefix} Improvement check: Cooldown period not yet passed for index {stats.current_risk_multiplier_index}."
                )

    def trigger_emergency_stop(
        self,
        reason: str = "manual emergency stop",
        *,
        operator_initiated: bool = True,
    ) -> None:
        """
        Latch trading OFF. Blocks every new entry until an operator explicitly
        clears it.

        This deliberately does NOT flatten positions — the controller owns the
        exchange calls needed to do that. Splitting the two keeps this method
        safe to call from any context (including a failed command handler).

        `operator_initiated` distinguishes the operator's kill switch (the
        default: the EMERGENCY_STOP command, or a stop restored from Redis)
        from an INFRASTRUCTURE failure -- the start-up branch where Redis could
        not be read and the controller could not prove the latch was clear.
        Only an operator stop may require a deliberate clear; an
        infrastructure failure must be releasable as soon as the state can be
        verified again. See `is_emergency_stop_operator_initiated()`.
        """
        self._emergency_stop_active = True
        self._emergency_stop_reason = reason
        self._emergency_stop_operator_initiated = bool(operator_initiated)
        self._is_trading_allowed = False
        self._trading_disabled_reason = f"EMERGENCY STOP active ({reason})"
        logger.critical(f"EMERGENCY STOP engaged: {reason}")

    def clear_emergency_stop(self) -> None:
        """Release the emergency-stop latch. Operator action only."""
        self._emergency_stop_active = False
        self._emergency_stop_reason = None
        self._emergency_stop_operator_initiated = False
        self._is_trading_allowed = True
        self._trading_disabled_reason = None
        # The operator's clear releases the LATCH. It must not open a gate
        # that the TRADING GUARD is holding closed.
        #
        # The guard state is not an operator decision: a loss streak restored
        # from Redis is the account's real history, and an UNVERIFIED state is
        # an infrastructure fault. Before the guard state was persisted this
        # could not matter -- the streak was always zero in RAM after a
        # restart -- so the unconditional re-enable was harmless. With the
        # streak restored it is not: clearing a kill switch would resume an
        # account that is still over its loss limit, which is exactly the
        # hole this change exists to close, reached by another route.
        #
        # The sibling release_unverified_emergency_stop() has always behaved
        # this way ("it never re-opens the global gate while a risk limit is
        # still in force"). This keeps the operator path consistent for the
        # state this change introduced, and leaves its documented behaviour
        # for everything else untouched.
        if (
            self._guard_state_restored
            or self._guard_state_unverified
            or getattr(self, "_guard_state_lost", False)
        ):
            self._check_risk_limits()
        logger.warning("EMERGENCY STOP cleared. New entries re-enabled.")

    def release_unverified_emergency_stop(self) -> bool:
        """Release a latch that was engaged because the state was UNVERIFIABLE.

        This is NOT the operator's kill switch, so it may be released without
        a human decision -- but it may only release the LATCH. It never
        re-opens the global gate while a risk limit is still in force: if
        `_global_gate_disable_reason` is set (a breached drawdown / daily
        loss / consecutive-loss limit, or a balance that could not be
        fetched), the gate stays closed and keeps reporting that reason.

        `clear_emergency_stop()` is the operator's path and deliberately
        keeps its existing, unconditional behaviour.

        Returns True if a latch was released.
        """
        if not self._emergency_stop_active:
            return False
        if self._emergency_stop_operator_initiated:
            # An operator's kill switch. Only clear_emergency_stop() may
            # release it.
            return False
        self._emergency_stop_active = False
        self._emergency_stop_reason = None
        self._emergency_stop_operator_initiated = False
        if self._global_gate_disable_reason is None:
            # Nothing else is holding the gate closed.
            self._is_trading_allowed = True
            self._trading_disabled_reason = None
        # else: a risk limit is still in force. Leave the gate closed and
        # leave the limit's reason visible -- releasing the latch must not
        # loosen a risk control.
        logger.warning(
            "EMERGENCY STOP (unverified start-up) released. New entries "
            "re-enabled unless a risk limit still applies."
        )
        return True

    def is_emergency_stop_active(self) -> bool:
        return bool(self._emergency_stop_active)

    def is_emergency_stop_operator_initiated(self) -> bool:
        """True when the latch was engaged by an operator, not by a failure.

        An operator stop keeps requiring an explicit clear. A latch engaged
        because the emergency-stop state could not be VERIFIED (Redis was
        unreachable at start-up) is an infrastructure fault and is releasable
        automatically once Redis can be read again.
        """
        return bool(getattr(self, "_emergency_stop_operator_initiated", False))

    def get_trading_disabled_reason(self) -> Optional[str]:
        """Why the gate last refused a symbol, in operator-readable words.

        Returns None only if the gate has never refused anything since this
        RiskManager was created.
        """
        return getattr(self, "_trading_disabled_reason", None)

    def _set_trading_disabled_reason(self, reason: str) -> None:
        """Record why the gate is refusing, for the log line and the API."""
        self._trading_disabled_reason = reason

    async def is_symbol_trading_allowed(
        self, symbol: str, mode: str = "live"
    ) -> bool:
        """
        Checks if trading is allowed for the given symbol.
        Includes checking the global flag and the user's blacklist.
        The blacklist is checked "on the fly" from the DB - changes are applied without a restart.

        `mode` says which sandbox the question is about: "live" (real
        money) or "paper" (simulated). The GLOBAL risk gate is per sandbox
        (upstream `bot_module/risk_manager.py:1163` takes the same
        argument), so a PAPER loss cannot refuse a LIVE symbol and a LIVE
        loss cannot refuse a PAPER symbol.

        The emergency stop and the blacklist stay SHARED: they are operator
        and account level controls, not per-sandbox limits, and upstream
        keeps them shared too.
        """
        mode = self._normalize_mode(mode)
        # 0. Emergency stop latch. Checked before everything else so that no
        # other path can re-enable trading while an emergency stop is active.
        if self._emergency_stop_active:
            self._set_trading_disabled_reason(
                f"EMERGENCY STOP active ({self._emergency_stop_reason})"
            )
            logger.warning(
                f"[Blacklist:{symbol}] Trading blocked: EMERGENCY STOP active "
                f"({self._emergency_stop_reason})"
            )
            return False

        # 1. Check the global flag for THIS sandbox (drawdown, consecutive
        #    losses, etc.). The two sandboxes have separate counters,
        #    separate thresholds and separate gates, so this is where the
        #    paper/live isolation is enforced.
        if not self._is_mode_trading_allowed(mode):
            self._set_trading_disabled_reason(
                self._mode_disable_reason(mode)
                or "trading disabled globally by the risk manager (reason not recorded)"
            )
            logger.debug(
                f"[Blacklist:{symbol}] Trading globally disabled (mode: {mode})"
            )
            return False

        # 2. Checking the blacklist (on-the-fly from the DB)
        if self.db_session and crud and self.user_id:
            try:
                # CRITICAL: Reset the SQLAlchemy session cache before the query.
                # Without this, the bot will see a cached version of the config,
                # and blacklist changes via API will not be applied until the bot is restarted.
                self.db_session.expire_all()

                # Get the config from the DB (now the data is guaranteed to be fresh)
                config = await crud.get_config(self.db_session, user_id=self.user_id)
                if config and config.risk_management:
                    rm_settings = ensure_dict(config.risk_management)

                    # Extracting blacklist data
                    blacklist_data = ensure_dict(rm_settings.get("blacklist"))

                    if blacklist_data:
                        coins = blacklist_data.get("coins", [])
                        now = datetime.now(timezone.utc)

                        for coin in coins:
                            coin = ensure_dict(coin)

                            coin_symbol = coin.get("symbol", "").upper()

                            if coin_symbol == symbol.upper():
                                until_val = (
                                    coin.get("until")
                                    if isinstance(coin, dict)
                                    else getattr(coin, "until", None)
                                )

                                # If until is specified, checking if the period has expired
                                if until_val:
                                    try:
                                        if isinstance(until_val, str):
                                            until_dt = datetime.fromisoformat(
                                                until_val.replace("Z", "+00:00")
                                            )
                                        else:
                                            until_dt = until_val

                                        if until_dt > now:
                                            reason = (
                                                coin.get(
                                                    "reason", "No reason specified"
                                                )
                                                if isinstance(coin, dict)
                                                else getattr(
                                                    coin,
                                                    "reason",
                                                    "No reason specified",
                                                )
                                            )
                                            logger.warning(
                                                f"[Blacklist:{symbol}] Symbol is BLACKLISTED until {until_dt.isoformat()}. Reason: {reason}"
                                            )

                                            # Send a notification to Telegram if not sent recently
                                            await self._notify_blacklist(
                                                symbol, reason, until_dt
                                            )

                                            self._set_trading_disabled_reason(
                                                f"symbol {symbol} is BLACKLISTED until "
                                                f"{until_dt.isoformat()} (reason: {reason})"
                                            )
                                            return False
                                        # else: period expired, symbol allowed
                                    except (ValueError, TypeError) as e:
                                        logger.warning(
                                            f"[Blacklist:{symbol}] Failed to parse until date: {until_val}. Treating as permanent. Error: {e}"
                                        )

                                        # Send notification to Telegram for an incorrect date (as a permanent block)
                                        await self._notify_blacklist(
                                            symbol,
                                            "Invalid date format - treated as permanent",
                                            None,
                                        )

                                        self._set_trading_disabled_reason(
                                            f"symbol {symbol} is BLACKLISTED "
                                            f"(invalid 'until' date {until_val!r}, "
                                            "treated as permanent)"
                                        )
                                        return False
                                else:
                                    # until is None = permanent blacklist
                                    reason = (
                                        coin.get("reason", "No reason specified")
                                        if isinstance(coin, dict)
                                        else getattr(
                                            coin, "reason", "No reason specified"
                                        )
                                    )
                                    logger.warning(
                                        f"[Blacklist:{symbol}] Symbol is PERMANENTLY BLACKLISTED. Reason: {reason}"
                                    )

                                    # Sending notification to Telegram
                                    await self._notify_blacklist(symbol, reason, None)

                                    self._set_trading_disabled_reason(
                                        f"symbol {symbol} is PERMANENTLY BLACKLISTED "
                                        f"(reason: {reason})"
                                    )
                                    return False
            except Exception as e:
                # FAIL CLOSED. This used to log and fall through to
                # `return True` — i.e. a transient DB error (connection reset,
                # timeout, pool exhaustion) silently disabled the user's
                # blacklist, which is precisely the control a user arms after a
                # bad trade. A risk control that switches itself off when the
                # database hiccups is not a risk control.
                #
                # Blocking here only gates NEW ENTRIES: both call sites
                # (controller._process_signal and assess_signal) are entry
                # paths, so existing positions can still be closed and their
                # stops still managed. Recovery is automatic — this re-queries
                # on every signal, so the first call after the DB heals allows
                # trading again.
                logger.critical(
                    f"[Blacklist:{symbol}] Error checking blacklist: {e}. "
                    f"FAILING CLOSED — new entries for this symbol are blocked "
                    f"until the blacklist query succeeds. Existing positions are "
                    f"NOT affected and can still be closed.",
                    exc_info=True,
                )
                try:
                    await self._notify_blacklist(
                        symbol,
                        f"Blacklist check failed (DB error) — new entries blocked: {e}",
                        None,
                    )
                except Exception:
                    pass
                self._set_trading_disabled_reason(
                    f"blacklist check failed (DB error) — new entries for {symbol} "
                    f"are blocked until the query succeeds: {e}"
                )
                return False

        return True

    async def _notify_blacklist(
        self, symbol: str, reason: str, until: Optional[datetime]
    ):
        """Sends a notification about a symbol block to Telegram no more than once every 10 minutes."""
        now = time.time()
        last_time = self.last_blacklist_notification.get(symbol, 0)

        if now - last_time > 600:  # 10 minutes
            if self.telegram_notifier:
                self.last_blacklist_notification[symbol] = now
                try:
                    await self.telegram_notifier.blacklist_alert(
                        symbol=symbol,
                        reason=reason,
                        until=until,
                        chat_id=self.user_telegram_chat_id,
                        api_key_name=self.api_key_name,
                    )
                except Exception as e:
                    logger.error(
                        f"[AutoBlacklist:{symbol}] Failed to send notification: {e}"
                    )

    async def _check_and_apply_auto_blacklist_rules(self, symbol: str):
        """Checks auto-blacklist rules and adds the symbol to the blacklist when triggered."""
        stop_timestamps = self._symbol_stop_timestamps.get(symbol, [])
        if not stop_timestamps:
            return  # No stops - nothing to check

        if not self.db_session or not crud or not self.user_id:
            return

        auto_rules = self._auto_blacklist_rules
        if not auto_rules:
            return

        now = time.time()

        try:
            for rule in auto_rules:
                rule = ensure_dict(rule)
                if not rule.get("enabled", True):
                    continue

                rule_consecutive_stops = rule.get("consecutiveStops", 999)
                within_period = rule.get(
                    "withinPeriod"
                )  # '15m', '30m', '1h', '2h', '4h', '8h', '24h' or None

                # Defining period in seconds
                period_seconds = None
                if within_period:
                    period_map = {
                        "15m": 15 * 60,
                        "30m": 30 * 60,
                        "1h": 1 * 60 * 60,
                        "2h": 2 * 60 * 60,
                        "4h": 4 * 60 * 60,
                        "8h": 8 * 60 * 60,
                        "24h": 24 * 60 * 60,
                    }
                    period_seconds = period_map.get(within_period)

                # Filtering stops by period
                if period_seconds:
                    cutoff_time = now - period_seconds
                    relevant_stops = [ts for ts in stop_timestamps if ts >= cutoff_time]
                else:
                    # No time limit - all stops in a row
                    relevant_stops = stop_timestamps

                stops_count = len(relevant_stops)
                logger.debug(
                    f"[AutoBlacklist:{symbol}] Rule check: {stops_count} stops (period={within_period}), required={rule_consecutive_stops}"
                )

                if stops_count >= rule_consecutive_stops:
                    duration = rule.get("duration", "end_of_day")
                    logger.warning(
                        f"[AutoBlacklist:{symbol}] Rule triggered! {stops_count} stops within {within_period or 'all time'} >= {rule_consecutive_stops}. Duration: {duration}"
                    )
                    await self._auto_add_to_blacklist(
                        symbol, duration, stops_count, within_period=within_period
                    )
                    # After adding, clear the stop history
                    self._symbol_stop_timestamps[symbol] = []
                    break  # Applying only the first triggered rule
        except Exception as e:
            logger.error(
                f"[AutoBlacklist:{symbol}] Error checking auto-blacklist rules: {e}",
                exc_info=True,
            )

    async def _auto_add_to_blacklist(
        self,
        symbol: str,
        duration: str,
        consecutive_stops: int,
        within_period: Optional[str] = None,
    ):
        """Adds a symbol to the blacklist automatically based on a rule."""
        if not self.db_session or not crud or not self.user_id:
            logger.error(
                f"[AutoBlacklist:{symbol}] Cannot add to blacklist: DB session or user_id missing"
            )
            return

        try:
            now = datetime.now(timezone.utc)

            # Calculating the lock expiration time
            until_dt: Optional[datetime] = None
            if duration == "1h":
                until_dt = now + timedelta(hours=1)
            elif duration == "4h":
                until_dt = now + timedelta(hours=4)
            elif duration == "8h":
                until_dt = now + timedelta(hours=8)
            elif duration == "end_of_day":
                # End of day UTC
                tomorrow = now.date() + timedelta(days=1)
                until_dt = datetime.combine(
                    tomorrow, dt_time(0, 0, 0, tzinfo=timezone.utc)
                )
            # 'permanent' - until_dt remains None

            period_label = within_period if within_period else "all time"
            reason = f"Auto-blacklist: {consecutive_stops} consecutive stops within {period_label}"

            # Getting current configuration
            self.db_session.expire_all()
            config_obj = await crud.get_config(self.db_session, user_id=self.user_id)
            if not config_obj:
                logger.error(
                    f"[AutoBlacklist:{symbol}] Config not found for user {self.user_id}"
                )
                return

            rm_settings = (
                ensure_dict(config_obj.risk_management)
                if config_obj.risk_management
                else {}
            )
            blacklist_data = ensure_dict(rm_settings.get("blacklist", {}))
            coins = blacklist_data.get("coins", [])

            # Checking if the symbol has already been added
            existing_idx = None
            for i, coin in enumerate(coins):
                coin = ensure_dict(coin)
                if coin.get("symbol", "").upper() == symbol.upper():
                    existing_idx = i
                    break

            new_coin = {
                "symbol": symbol.upper(),
                "until": until_dt.isoformat() if until_dt else None,
                "reason": reason,
                "addedAt": now.isoformat(),
            }

            if existing_idx is not None:
                coins[existing_idx] = new_coin
                logger.info(
                    f"[AutoBlacklist:{symbol}] Updated existing blacklist entry"
                )
            else:
                coins.append(new_coin)
                logger.info(f"[AutoBlacklist:{symbol}] Added new blacklist entry")

            # Save back
            blacklist_data["coins"] = coins
            rm_settings["blacklist"] = blacklist_data

            await crud.update_config_section(
                self.db_session, self.user_id, "risk_management", rm_settings
            )
            await self.db_session.commit()

            logger.warning(
                f"[AutoBlacklist:{symbol}] Symbol added to blacklist until {until_dt.isoformat() if until_dt else 'permanent'}. Reason: {reason}"
            )

            # Sending notification to Telegram
            if self.telegram_notifier:
                try:
                    await self.telegram_notifier.blacklist_alert(
                        symbol=symbol,
                        reason=reason,
                        until=until_dt.isoformat() if until_dt else None,
                        chat_id=self.user_telegram_chat_id,
                        api_key_name=self.api_key_name,
                    )
                except Exception as notif_e:
                    logger.error(
                        f"[AutoBlacklist:{symbol}] Failed to send Telegram notification: {notif_e}"
                    )

        except Exception as e:
            logger.error(
                f"[AutoBlacklist:{symbol}] Error adding to blacklist: {e}",
                exc_info=True,
            )

    def _daily_loss_base_is_usable(self) -> bool:
        """Can today's daily-loss / drawdown ratios actually be computed?

        The distinction is between KNOWN and UNKNOWN, not between large and
        small:

        * a base of zero or less -> NOT usable. Nothing can divide by it.
        * a base stamped with a day that is not TODAY -> NOT usable. This is the
          measured defect: the denominator is from a day that has ended, so the
          ratio reads LOW and the gate under-trips. A large stale base is still
          a stale base.
        * a base stamped with TODAY -> usable.
        * a base with NO stamp at all -> usable. Nothing in the product
          produces this shape: every site that assigns the base also stamps the
          day. It only appears when `TradeStats` is populated directly, which is
          what the pre-v2 callers and the existing tests do. Treating it as
          unknown would fail closed on correct existing behaviour, so it keeps
          the pre-v2 meaning.

        The day comparison happens HERE, on the read side, rather than being
        left to the discipline of every writer that stamps the base. That is
        deliberate: the defect being fixed IS two fields drifting apart, so
        trusting write-side discipline to keep them in step would assume away
        the exact failure mode under repair. Raised by the hermes reviewer.
        """
        if self.stats.start_of_day_balance <= 1e-9:
            return False
        stamp = self.stats.start_of_day_day_str
        if stamp:
            return stamp == self._current_utc_day_str()
        return True

    def _check_risk_limits(self, mode: str = "live"):  # Global limits
        """Evaluate the global risk limits for ONE sandbox.

        `mode` selects the sandbox: "live" (real money) or "paper"
        (simulated). The two are judged against SEPARATE counters, SEPARATE
        thresholds and SEPARATE gates, so a PAPER loss can never close the
        LIVE gate, and a LIVE loss can never close the PAPER gate.

        The LIVE branch below is the code that was here before this argument
        existed, unchanged: it is not loosened, not tightened and not
        re-ordered. The PAPER branch delegates to `_check_paper_risk_limits`,
        which mirrors it against the paper counters.
        """
        mode = self._normalize_mode(mode)
        if mode == "paper":
            self._check_paper_risk_limits()
            return
        # A risk limit may only be treated as "within range" when the numbers
        # it is computed from are KNOWN. If the guard state could not be read
        # at start-up then the loss streak and the day's PnL are unknown, so
        # no limit can be shown to be satisfied -- and a limit that is
        # silently assumed to be satisfied is exactly how a blocked account
        # gets un-blocked. Fail CLOSED until the state can be read again.
        if getattr(self, "_guard_state_unverified", False):
            self._is_trading_allowed = False
            self._global_gate_disable_reason = RISK_GUARD_UNVERIFIED_REASON
            if not getattr(self, "_emergency_stop_active", False):
                self._trading_disabled_reason = RISK_GUARD_UNVERIFIED_REASON
            # The persisted state carries the paper counters too, so it is
            # unknown for the paper sandbox as well. The PAPER gate is closed
            # on the same trigger -- this does not touch the live path.
            self._close_paper_gate_for_unknown_guard_state(
                RISK_GUARD_UNVERIFIED_REASON
            )
            return
        if getattr(self, "_guard_state_lost", False):
            # The guard state is GONE, not merely unreadable. Today's PnL and
            # the loss streak cannot be shown to be within limits, so the gate
            # stays closed. This heals itself at the UTC day rollover (see
            # `_check_and_reset_daily_stats`), which is the one moment the day's
            # counters restart at zero by definition.
            self._is_trading_allowed = False
            self._global_gate_disable_reason = RISK_GUARD_STATE_LOST_REASON
            if not getattr(self, "_emergency_stop_active", False):
                self._trading_disabled_reason = RISK_GUARD_STATE_LOST_REASON
            # The persisted state carries the paper counters too, so it is
            # lost for the paper sandbox as well. The PAPER gate is closed on
            # the same trigger -- this does not touch the live path.
            self._close_paper_gate_for_unknown_guard_state(
                RISK_GUARD_STATE_LOST_REASON
            )
            return
        should_be_allowed = True
        disable_reason = ""
        if self.stats.current_balance < self.min_balance_threshold:
            should_be_allowed = False
            disable_reason = f"Balance (${self.stats.current_balance:.2f}) < min (${self.min_balance_threshold:.2f})"
        else:
            if not self._daily_loss_base_is_usable():
                # Both the drawdown and the daily-loss limits are a RATIO
                # against the day's opening balance. Without a base that
                # belongs to the CURRENT UTC day the ratio cannot be computed,
                # and "cannot be computed" is NOT "within limits" -- it used to
                # be treated as within limits, which is a fail-OPEN inside a
                # function whose own comment above demands failing closed on
                # unknown state. A base near zero used to skip BOTH checks
                # silently, with no log at all.
                #
                # A brand-new account is not blocked by this: the first
                # `initialize_balance` captures the base at the UTC rollover
                # (inside `update_balance`), where the balance genuinely IS the
                # opening balance, so the very next limit check can decide.
                # What is blocked is the window where the base is genuinely
                # unknown.
                should_be_allowed = False
                disable_reason = RISK_GUARD_BASE_UNKNOWN_REASON
            else:
                drawdown_pct = (
                    abs(self.stats.today_pnl / self.stats.start_of_day_balance)
                    if self.stats.today_pnl < 0
                    else 0
                )
                if drawdown_pct >= self.max_drawdown_threshold:
                    should_be_allowed = False
                    disable_reason = f"Max drawdown ({drawdown_pct * 100:.2f}%) >= limit ({self.max_drawdown_threshold * 100:.2f}%)"
                daily_loss_pct = (
                    abs(self.stats.today_pnl / self.stats.start_of_day_balance)
                    if self.stats.today_pnl < 0
                    else 0
                )
                if daily_loss_pct >= self.daily_max_loss_threshold:
                    should_be_allowed = False
                    disable_reason = f"Daily loss ({daily_loss_pct * 100:.2f}%) >= limit ({self.daily_max_loss_threshold * 100:.2f}%)"
            if self.stats.consecutive_losses >= self.max_consecutive_losses:
                should_be_allowed = False
                disable_reason = f"Max consec losses ({self.stats.consecutive_losses}) >= limit ({self.max_consecutive_losses})"
        if not should_be_allowed:
            # Record WHICH limit tripped, with the measured value and the
            # threshold. `disable_reason` already carries both numbers
            # (e.g. "Max drawdown (12.34%) >= limit (10.00%)"), so the gate
            # can name the breach instead of reporting a bare "general block".
            #
            # This is recorded even when the global flag was ALREADY closed by
            # something else (e.g. the emergency-stop latch). Otherwise a limit
            # that breaches DURING a stop is invisible, and releasing that
            # stop would silently re-open the gate past a tripped limit.
            breach_reason = f"global risk limit breached: {disable_reason}"
            if self._is_trading_allowed:
                logger.critical(f"Trading disabled globally! Reason: {disable_reason}")
                self._is_trading_allowed = False
                self._trading_disabled_reason = breach_reason
                # Added check for self.loop_from_controller
                if self.telegram_notifier and self.loop_from_controller:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.telegram_notifier.risk_manager_alert(
                                reason=disable_reason,
                                alert_type="TRADE_DISABLED",
                                current_balance=self.stats.current_balance,
                                daily_pnl=self.stats.today_pnl,
                                chat_id=self.user_telegram_chat_id,
                                api_key_name=self.api_key_name,
                            ),
                            self.loop_from_controller,
                        )
                    except Exception as e:
                        logger.error(f"Failed to schedule Telegram notification: {e}")
            self._global_gate_disable_reason = breach_reason
        elif should_be_allowed:
            # The limits are within range again.
            self._global_gate_disable_reason = None
            if self._emergency_stop_active:
                # The latch owns the gate while it is engaged; it is released
                # only by clear_emergency_stop() or by the start-up self-heal.
                # Clearing the stale limit reason here is what lets that
                # release re-open the gate -- but only once no limit is
                # actually in force.
                pass
            elif not self._is_trading_allowed:
                logger.info(
                    "Global risk limits are now within acceptable range. Re-enabling trading globally."
                )
                self._is_trading_allowed = True

    def _close_paper_gate_for_unknown_guard_state(self, reason: str) -> None:
        """Close the PAPER gate because the persisted guard state is UNKNOWN.

        The persisted guard state carries BOTH sandboxes (the live counters
        at the top level and the paper counters under the "paper" key), so
        "the state could not be read" or "the state was lost" is a fact
        about the PAPER counters too. A paper-only account has no live
        counters to protect, so without this its paper gate would stay OPEN
        while the persisted state was unknown -- a weakening of the
        fail-closed behaviour this fork relies on.
        """
        if self._paper_is_trading_allowed:
            logger.critical(
                f"Paper trading disabled! Reason: {reason}"
            )
        self._paper_is_trading_allowed = False
        self._paper_global_gate_disable_reason = reason

    def _paper_daily_loss_base_is_usable(self) -> bool:
        """Can the paper sandbox's daily-loss / drawdown ratios be computed?

        The paper sandbox has its OWN opening balance, anchored on the paper
        wallet's balance (see `_anchor_paper_start_of_day_base`). It is
        stamped with the UTC day it belongs to, exactly like the live base,
        and the same rules apply: a base of zero cannot be divided by, and a
        base stamped with a day that is not TODAY measures the day's loss
        against the wrong denominator.
        """
        if self.paper_stats.start_of_day_balance <= 1e-9:
            return False
        stamp = self.paper_stats.start_of_day_day_str
        if stamp:
            return stamp == self._current_utc_day_str()
        return True

    def _anchor_paper_start_of_day_base(self, balance: float) -> bool:
        """Install the PAPER wallet's balance as the paper day's opening balance.

        The paper sandbox's own base, written once per UTC day and never
        restated from a mid-session balance. It is anchored when the day's
        balance is first observed; from then on the day's paper loss is
        measured against the denominator that was in force when the day
        started.
        """
        amount = float(balance or 0.0)
        if amount <= 1e-9:
            return False
        current_day_str = self._current_utc_day_str()
        if (
            self.paper_stats.start_of_day_day_str == current_day_str
            and self.paper_stats.start_of_day_balance > 1e-9
        ):
            return False
        self.paper_stats.start_of_day_balance = amount
        self.paper_stats.start_of_day_day_str = current_day_str
        return True

    def _check_and_reset_paper_daily_stats(self) -> None:
        """Roll the PAPER sandbox over to the new UTC day.

        Deliberately SEPARATE from `_check_and_reset_daily_stats()`: that
        function owns the live sandbox's counters, its opening-balance
        provenance and the lost-state latch, and the paper sandbox must not
        inherit any of them. A paper day starts at zero paper PnL and a zero
        paper loss streak, and the paper opening balance is re-anchored from
        the paper wallet.
        """
        _, current_day_str_utc = self._get_current_day_start_info()
        if self.paper_stats.last_known_day_str == current_day_str_utc:
            return
        logger.info(
            "[RiskManager:PaperDailyReset] New trading day detected (UTC): %s. "
            "The PAPER sandbox's day counters restart at zero.",
            current_day_str_utc,
        )
        self.paper_stats.today_pnl = 0.0
        self.paper_stats.consecutive_losses = 0
        self.paper_stats.last_known_day_str = current_day_str_utc
        # The paper wallet's balance at the rollover IS the day's opening
        # balance, so the base is dropped and re-anchored from it.
        self.paper_stats.start_of_day_balance = 0.0
        self.paper_stats.start_of_day_day_str = ""
        self._anchor_paper_start_of_day_base(self.paper_stats.current_balance)

    def _check_paper_risk_limits(self) -> None:
        """Evaluate the PAPER sandbox's limits against the PAPER counters.

        The mirror of the live `_check_risk_limits()`: the same three
        limits, the same fail-closed behaviour when the numbers are unknown,
        and the same emergency-stop semantics -- but measured against
        `self.paper_stats` and the paper thresholds, and writing to the
        PAPER gate. A PAPER breach closes the PAPER gate and nothing else.
        """
        stats = self.paper_stats
        max_dd, max_daily_loss, max_consec = self._mode_thresholds("paper")

        if not getattr(self, "_paper_balance_known", False):
            # The PAPER wallet's balance has not been read in this process
            # yet. It is UNKNOWN, not zero, and the limits cannot be judged
            # against an unknown number -- so they are NOT judged, and the
            # gate is left exactly as it is. Judging them here would close
            # the paper gate for the wrong reason ("balance $0.00 < min"),
            # and the controller consults that gate before the balance is
            # refreshed, which would stop paper trading until the next
            # restart.
            return

        # The persisted guard state carries both sandboxes, so an unknown or
        # lost state is unknown for the paper counters too. Fail CLOSED.
        if getattr(self, "_guard_state_unverified", False):
            self._close_paper_gate_for_unknown_guard_state(
                RISK_GUARD_UNVERIFIED_REASON
            )
            return
        if getattr(self, "_guard_state_lost", False):
            self._close_paper_gate_for_unknown_guard_state(
                RISK_GUARD_STATE_LOST_REASON
            )
            return

        should_be_allowed = True
        disable_reason = ""
        if stats.current_balance < self.min_balance_threshold:
            should_be_allowed = False
            disable_reason = (
                f"[PAPER] Balance (${stats.current_balance:.2f}) < min "
                f"(${self.min_balance_threshold:.2f})"
            )
        else:
            if not self._paper_daily_loss_base_is_usable():
                should_be_allowed = False
                disable_reason = f"[PAPER] {RISK_GUARD_BASE_UNKNOWN_REASON}"
            else:
                drawdown_pct = (
                    abs(stats.today_pnl / stats.start_of_day_balance)
                    if stats.today_pnl < 0
                    else 0
                )
                if drawdown_pct >= max_dd:
                    should_be_allowed = False
                    disable_reason = (
                        f"[PAPER] Max drawdown ({drawdown_pct * 100:.2f}%) >= "
                        f"limit ({max_dd * 100:.2f}%)"
                    )
                daily_loss_pct = (
                    abs(stats.today_pnl / stats.start_of_day_balance)
                    if stats.today_pnl < 0
                    else 0
                )
                if daily_loss_pct >= max_daily_loss:
                    should_be_allowed = False
                    disable_reason = (
                        f"[PAPER] Daily loss ({daily_loss_pct * 100:.2f}%) >= "
                        f"limit ({max_daily_loss * 100:.2f}%)"
                    )
            if stats.consecutive_losses >= max_consec:
                should_be_allowed = False
                disable_reason = (
                    f"[PAPER] Max consec losses ({stats.consecutive_losses}) >= "
                    f"limit ({max_consec})"
                )
        if not should_be_allowed:
            if self._paper_is_trading_allowed:
                logger.critical(f"Paper trading disabled! Reason: {disable_reason}")
                self._paper_is_trading_allowed = False
                self._paper_last_disable_reason = disable_reason
                if self.telegram_notifier and self.loop_from_controller:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.telegram_notifier.risk_manager_alert(
                                reason=disable_reason,
                                alert_type="TRADE_DISABLED",
                                current_balance=stats.current_balance,
                                daily_pnl=stats.today_pnl,
                                chat_id=self.user_telegram_chat_id,
                                api_key_name=self.api_key_name,
                            ),
                            self.loop_from_controller,
                        )
                    except Exception as e:
                        logger.error(f"Failed to schedule Telegram notification: {e}")
            self._paper_global_gate_disable_reason = disable_reason
        else:
            self._paper_global_gate_disable_reason = None
            if not self._paper_is_trading_allowed:
                logger.info(
                    "Paper risk limits are now within acceptable range. "
                    "Re-enabling paper trading."
                )
                self._paper_is_trading_allowed = True
                self._paper_last_disable_reason = None

    async def assess_signal(
        self,
        signal: StrategySignal,
        lot_params: Optional[Dict[str, float]],
        min_notional_usd: Optional[float],
        mode: str = "live",
        executor_override: Optional[Any] = None,
    ) -> Tuple[bool, Optional[float], Optional[float], Optional[str]]:
        log_prefix = f"[RiskAssessV2:{signal.strategy_name}:{signal.symbol}:{signal.direction.name}]"
        logger.info(
            f"{log_prefix} --- STARTING SIGNAL ASSESSMENT (Mode: {mode.upper()}) ---"
        )

        current_balance_val = 0.0
        risk_per_trade_base = 0.0
        max_stop_distance_pct_to_use = 0.0
        balance_updated_successfully = False

        if mode == "paper":
            # THE PAPER BALANCE IS READ BEFORE THE GATE IS CONSULTED.
            #
            # The PAPER gate is judged against the PAPER wallet, and the
            # paper wallet's balance is only known once it has been read.
            # Judging it afterwards would evaluate the gate against a
            # balance it has never seen (0.0), which closes the paper gate
            # on the very first signal of the process -- and then nothing
            # could re-open it, because the balance is only read on the very
            # path the closed gate blocks. Upstream avoids the same
            # deadlock by evaluating the paper limits inside the paper branch
            # (upstream `bot_module/risk_manager.py:1695`-`:1700`).
            try:
                paper_balances = await self.paper_executor.get_account_balance()
                usdt_balance_data = (
                    paper_balances.get("USDT") if paper_balances else None
                )
                current_balance_val = (
                    float(usdt_balance_data["free"]) if usdt_balance_data else 0.0
                )
                risk_per_trade_base = self.paper_risk_per_trade
                max_stop_distance_pct_to_use = self.paper_max_stop_distance_pct
                balance_updated_successfully = True
                # The PAPER wallet's balance belongs to the PAPER sandbox.
                # It must never be written into `self.stats`, which is the
                # LIVE account's balance.
                self.paper_stats.current_balance = current_balance_val
                self._paper_balance_known = True
                self._check_and_reset_paper_daily_stats()
                self._anchor_paper_start_of_day_base(current_balance_val)
                self._check_risk_limits(mode="paper")
                logger.info(
                    f"{log_prefix} Using PAPER settings. Balance: ${current_balance_val:.2f}, Risk/Trade: {risk_per_trade_base * 100:.2f}%"
                )
            except Exception as e:
                logger.error(
                    f"{log_prefix} Failed to get paper balance: {e}", exc_info=True
                )
                return False, None, 0.0, "PAPER_BALANCE_FETCH_FAILED"

        # BLACKLIST CHECK (on-the-fly)
        # Check the blacklist at an early stage to avoid wasting resources on further checks
        #
        # The gate is consulted for THIS MODE: the live sandbox and the
        # paper sandbox have separate counters, separate thresholds and
        # separate gates, so a paper loss cannot refuse a live signal and a
        # live loss cannot refuse a paper signal.
        if not await self.is_symbol_trading_allowed(signal.symbol, mode=mode):
            logger.warning(
                f"{log_prefix} Signal REJECTED. Reason: Symbol is in blacklist or trading globally disabled."
            )
            return False, None, 0.0, "SYMBOL_BLACKLISTED"

        if mode != "paper":  # live
            self._check_and_reset_daily_stats()
            if executor_override is not None and executor_override is not self.executor:
                try:
                    balances = await executor_override.get_account_balance()
                    usdt_balance_data = balances.get("USDT") if balances else None
                    if not usdt_balance_data:
                        balance_updated_successfully = False
                    else:
                        free_bal = float(usdt_balance_data.get("free", 0.0) or 0.0)
                        locked_bal = float(usdt_balance_data.get("locked", 0.0) or 0.0)
                        fetched_total = free_bal + locked_bal

                        # Reject non-finite balances before they reach shared
                        # state. Every limit here (min balance, the >90% drop
                        # guard, drawdown) is a COMPARISON, and NaN compares
                        # False against everything -- so NaN would slip past the
                        # anomaly guard, leave trading enabled, and be written
                        # into stats.current_balance, poisoning the other
                        # balance paths too. Fail closed instead.
                        if not math.isfinite(fetched_total):
                            logger.error(
                                f"{log_prefix} Executor override returned a "
                                f"non-finite USDT balance ({fetched_total!r}). "
                                f"Treating the balance fetch as failed."
                            )
                            current_balance_val = self.stats.current_balance
                            balance_updated_successfully = False
                        # Mirror update_balance()'s anomaly guard (its
                        # "anomalously low new balance" branch): a >90% drop is
                        # treated as a possible API glitch and the previous
                        # balance is kept. Without this, the override path would
                        # act on a bad read that the default path deliberately
                        # distrusts, and would then latch trading off globally.
                        elif (
                            self.stats.current_balance > 10
                            and fetched_total < self.stats.current_balance * 0.1
                            and fetched_total < self.min_balance_threshold / 2
                        ):
                            logger.warning(
                                f"{log_prefix} Override executor reported an "
                                f"anomalously low balance (${fetched_total:.2f}) vs "
                                f"previous (${self.stats.current_balance:.2f}). "
                                f"Keeping the previous balance for this assessment."
                            )
                            current_balance_val = self.stats.current_balance
                            balance_updated_successfully = True
                        else:
                            current_balance_val = fetched_total
                            balance_updated_successfully = current_balance_val > 0
                            if balance_updated_successfully:
                                # The balance that sizes this trade must also be
                                # the balance the global risk limits are judged
                                # against. This branch previously updated neither
                                # the shared stats nor the limits, so the gate
                                # below (`if not self._is_trading_allowed`) read a
                                # STALE flag while sizing used a FRESH balance --
                                # and this is the branch EVERY live order takes,
                                # because TradingController._process_signal always
                                # passes executor_override. An account below
                                # min_balance_threshold kept opening positions.
                                async with self._balance_lock:
                                    self.stats.current_balance = current_balance_val
                                self._check_risk_limits()
                except Exception as e:
                    logger.error(
                        f"{log_prefix} Failed to get live balance from market executor override: {e}",
                        exc_info=True,
                    )
                    balance_updated_successfully = False
            else:
                balance_updated_successfully = await self.update_balance()
                self._check_risk_limits()
                current_balance_val = self.stats.current_balance
            risk_per_trade_base = self.live_risk_per_trade
            max_stop_distance_pct_to_use = self.live_max_stop_distance_pct
            logger.debug(
                f"{log_prefix} Using LIVE settings. Balance: ${current_balance_val:.2f}, Risk/Trade: {risk_per_trade_base * 100:.2f}%"
            )

        if current_balance_val <= 1e-9:
            return False, None, 0.0, "ZERO_BALANCE"

        # 1. By default, the final risk is equal to the base risk for the current mode.
        if signal.risk_usd is not None:
            initial_base_risk_usd_planned = max(float(signal.risk_usd), 0.0)
            logger.debug(
                f"{log_prefix} Using fixed risk budget from strategy signal: ${initial_base_risk_usd_planned:.2f}"
            )
        else:
            risk_per_trade_final = risk_per_trade_base

            # 2. If there is an override in the signal, use it.
            if signal.risk_pct is not None:
                risk_per_trade_final = max(float(signal.risk_pct), 0.0)
                logger.debug(
                    f"{log_prefix} Using risk per trade from strategy signal: {risk_per_trade_final * 100:.2f}%"
                )

            # Now `risk_per_trade_final` always has a correct value.
            initial_base_risk_usd_planned = current_balance_val * risk_per_trade_final

        logger.debug(
            f"{log_prefix} Initial Base Risk Planned (before S/S): ${initial_base_risk_usd_planned:.2f}"
        )

        if self._emergency_stop_active:
            logger.warning(
                f"{log_prefix} Signal REJECTED. Reason: EMERGENCY STOP active "
                f"({self._emergency_stop_reason})."
            )
            return False, None, initial_base_risk_usd_planned, "EMERGENCY_STOP"

        if not self._is_mode_trading_allowed(mode):
            logger.warning(
                f"{log_prefix} Signal REJECTED. Reason: Trading disabled globally by portfolio risk limits."
            )
            return False, None, initial_base_risk_usd_planned, "GLOBAL_RISK_LIMIT"

        if not balance_updated_successfully:
            logger.warning(
                f"{log_prefix} Signal REJECTED. Reason: Failed to update/confirm current balance."
            )
            return False, None, initial_base_risk_usd_planned, "BALANCE_UPDATE_FAILED"

        # 1. Maximum allowable risk per trade in USD (including S/S multiplier)
        target_max_risk_usd = initial_base_risk_usd_planned
        current_risk_multiplier_ss = 1.0  # By default
        if (
            self._strategy_symbol_adjustment_enabled
            and self._strategy_symbol_risk_multipliers
        ):
            perf_key = (signal.symbol, signal.strategy_name)
            # defaultdict will create SymbolStrategyPerformanceStats with a default index if the key is missing
            perf_stats = self._symbol_strategy_performance[perf_key]
            idx = perf_stats.current_risk_multiplier_index

            if 0 <= idx < len(self._strategy_symbol_risk_multipliers):
                current_risk_multiplier_ss = self._strategy_symbol_risk_multipliers[idx]
            else:  # Fallback to the default index if the saved one is incorrect
                logger.error(
                    f"{log_prefix} Invalid risk_multiplier_index {idx} for {perf_key}. Resetting to default index {self._s_s_default_risk_idx}."
                )
                current_risk_multiplier_ss = self._strategy_symbol_risk_multipliers[
                    self._s_s_default_risk_idx
                ]
                perf_stats.current_risk_multiplier_index = (
                    self._s_s_default_risk_idx
                )  # Fixing index in statistics

            target_max_risk_usd *= current_risk_multiplier_ss
            logger.info(
                f"{log_prefix} Strategy/Symbol Risk Multiplier: {current_risk_multiplier_ss:.2f} (Index: {perf_stats.current_risk_multiplier_index}). "
                f"Target Max Risk USD for sizing (after S/S): ${target_max_risk_usd:.2f}"
            )

        if target_max_risk_usd <= 1e-9:
            logger.warning(
                f"{log_prefix} Signal REJECTED. Reason: Target Max Risk USD for sizing is zero/negative (${target_max_risk_usd:.2f}) "
                f"after S/S multiplier {current_risk_multiplier_ss:.2f}. Base planned risk was ${initial_base_risk_usd_planned:.2f}."
            )
            return False, None, initial_base_risk_usd_planned, "ZERO_RISK"

        # 2. Maximum position nominal in USD
        #
        # Deliberately mode-aware, and deliberately written as "not paper"
        # rather than "== live". TradingController._executor_for_market_type
        # (controller.py:786) resolves the executor with `if mode == "paper":
        # ... else: <live executor>`, i.e. everything that is not "paper"
        # trades real money. An `if mode == "live"` test here would fail OPEN
        # in exactly that gap: an unexpected value ("testnet", "LIVE", None)
        # would select the live executor but silently get the 10.0 backtest
        # cap. Matching the executor's own fail-direction keeps the cap
        # fail-closed: only an explicit "paper" gets the backtest cap.
        if mode == "paper":
            max_pos_size_pct_cfg = getattr(
                config,
                "BACKTEST_MAX_POSITION_SIZE_PCT_BALANCE",
                0.50,
            )
        else:
            max_pos_size_pct_cfg = getattr(
                config,
                "MAX_REAL_POSITION_SIZE_PCT_BALANCE",  # Live-specific cap
                getattr(config, "BACKTEST_MAX_POSITION_SIZE_PCT_BALANCE", 0.50),
            )
        max_notional_for_position_usd = current_balance_val * max_pos_size_pct_cfg
        logger.debug(
            f"{log_prefix} Max Position Notional (config {max_pos_size_pct_cfg * 100:.2f}% of balance ${current_balance_val:.2f}): ${max_notional_for_position_usd:.2f}"
        )

        # 3. Price validation and calculation of distance to stop
        entry_price_for_calc = (
            signal.trigger_price
            if signal.trigger_price is not None
            else signal.entry_price
        )
        if entry_price_for_calc is None or entry_price_for_calc <= 0:
            logger.warning(
                f"{log_prefix} Signal REJECTED. Reason: Invalid entry/trigger price for calculation ({entry_price_for_calc})."
            )
            return False, None, initial_base_risk_usd_planned, "INVALID_PRICE"
        entry_price_d = Decimal(str(entry_price_for_calc))

        stop_loss_price = signal.stop_loss

        # NO STOP LOSS MODE
        # If stop_loss is None, it means the strategy works without a stop (DCA/Grid).
        # Position size is defined as risk_value (% of balance) directly,
        # without calculation via distance to stop.
        if stop_loss_price is None:
            logger.info(
                f"{log_prefix} NO STOP LOSS mode. Sizing position from the planned first-entry budget."
            )

            # Position size = target_max_risk_usd / entry_price (as margin, not risk)
            notional_for_position = target_max_risk_usd
            qty_no_sl = Decimal(str(notional_for_position)) / entry_price_d

            # Limiting by max_notional
            qty_limit_by_max_notional_d = (
                Decimal(str(max_notional_for_position_usd)) / entry_price_d
            )
            working_quantity_d = min(qty_no_sl, qty_limit_by_max_notional_d)

            logger.info(
                f"{log_prefix} No-SL sizing: notional=${notional_for_position:.2f}, qty={working_quantity_d:.8f}"
            )

            # Moving to stages 6+ (exchange filters)
            # Use stop_loss_distance_d = 0 for compatibility, but set the flag
            stop_loss_distance_d = Decimal("0")
            stop_loss_price_d = Decimal("0")
        else:
            # STANDARD LOGIC WITH STOP
            if stop_loss_price <= 0:
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: Invalid stop_loss price ({stop_loss_price})."
                )
                return False, None, initial_base_risk_usd_planned, "INVALID_SL"
            stop_loss_price_d = Decimal(str(stop_loss_price))

            if (
                signal.direction == SignalDirection.LONG
                and stop_loss_price_d >= entry_price_d
            ) or (
                signal.direction == SignalDirection.SHORT
                and stop_loss_price_d <= entry_price_d
            ):
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: SL ({stop_loss_price_d}) incorrectly placed relative to entry ({entry_price_d}) for {signal.direction.name}."
                )
                return False, None, initial_base_risk_usd_planned, "SL_WRONG_SIDE"

            stop_loss_distance_d = abs(entry_price_d - stop_loss_price_d)
            if stop_loss_distance_d <= Decimal(str(1e-9 * entry_price_for_calc)):
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: Stop loss distance is zero or negligible ({stop_loss_distance_d})."
                )
                return False, None, initial_base_risk_usd_planned, "ZERO_SL_DISTANCE"
            logger.debug(
                f"{log_prefix} EntryPriceD: {entry_price_d}, SLPriceD: {stop_loss_price_d}, SLDistD: {stop_loss_distance_d}"
            )

            max_stop_pct_d = Decimal(str(max_stop_distance_pct_to_use))
            min_stop_pct_d = Decimal(
                str(getattr(config, "RISK_MANAGER_MIN_STOP_DISTANCE_PCT", 0.05))
            ) / Decimal("100")
            if stop_loss_distance_d > entry_price_d * max_stop_pct_d:
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: SL distance {stop_loss_distance_d} ({(stop_loss_distance_d / entry_price_d * 100):.2f}%) > max allowed limit ({max_stop_pct_d * 100:.2f}%)."
                )
                return False, None, initial_base_risk_usd_planned, "SL_TOO_FAR"
            if stop_loss_distance_d < entry_price_d * min_stop_pct_d:
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: SL distance {stop_loss_distance_d} ({(stop_loss_distance_d / entry_price_d * 100):.2f}%) < min allowed limit ({min_stop_pct_d * 100:.2f}%)."
                )
                return False, None, initial_base_risk_usd_planned, "SL_TOO_CLOSE"

            # 5. Determining quantity based on limits (standard logic)
            qty_limit_by_max_risk_d = (
                Decimal(str(target_max_risk_usd)) / stop_loss_distance_d
            )
            qty_limit_by_max_notional_d = (
                Decimal(str(max_notional_for_position_usd)) / entry_price_d
            )

            working_quantity_d = min(
                qty_limit_by_max_risk_d, qty_limit_by_max_notional_d
            )
            logger.debug(
                f"{log_prefix} QtyLimitByRisk(S/S adj): {qty_limit_by_max_risk_d:.8f}, QtyLimitByMaxNotional: {qty_limit_by_max_notional_d:.8f} -> WorkingQtyPreAdjust: {working_quantity_d:.8f}"
            )

        # 4. R/R check (skipping if there is no stop)
        signal_details = (
            signal.details if isinstance(getattr(signal, "details", None), dict) else {}
        )
        skip_rr_for_dca_grid = bool(
            signal_details.get("skip_min_rr_for_dca_grid")
            or signal_details.get("uses_dca_or_grid_management")
        )

        if stop_loss_price is not None and not skip_rr_for_dca_grid:
            target_profit_price_for_rr_calc = signal.take_profit
            if not target_profit_price_for_rr_calc and signal.partial_targets:
                relevant_partials = [
                    pt.price for pt in signal.partial_targets if pt.price is not None
                ]
                if relevant_partials:
                    target_profit_price_for_rr_calc = (
                        max(relevant_partials)
                        if signal.direction == SignalDirection.LONG
                        else min(relevant_partials)
                    )

            if (
                not target_profit_price_for_rr_calc
                or target_profit_price_for_rr_calc <= 0
            ):
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: No valid TP (final or partial) for R/R calculation. Derived TP: {target_profit_price_for_rr_calc}"
                )
                return False, None, initial_base_risk_usd_planned, "NO_TP_FOR_RR"

            take_profit_price_d = Decimal(str(target_profit_price_for_rr_calc))
            if (
                signal.direction == SignalDirection.LONG
                and take_profit_price_d <= entry_price_d
            ) or (
                signal.direction == SignalDirection.SHORT
                and take_profit_price_d >= entry_price_d
            ):
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: Derived TP ({take_profit_price_d}) not profitable relative to entry ({entry_price_d})."
                )
                return False, None, initial_base_risk_usd_planned, "TP_NOT_PROFITABLE"

            profit_distance_d = abs(take_profit_price_d - entry_price_d)
            if stop_loss_distance_d <= Decimal("1e-9"):
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: SL distance is zero, cannot calculate R/R."
                )
                return False, None, initial_base_risk_usd_planned, "ZERO_SL_FOR_RR"
            rr_ratio_prices = profit_distance_d / stop_loss_distance_d
            min_rr_decimal_prices = Decimal(str(self.min_rr_ratio))
            if rr_ratio_prices < min_rr_decimal_prices:
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: Price R/R {rr_ratio_prices:.2f} (using TP={target_profit_price_for_rr_calc}) < min required {self.min_rr_ratio:.2f}."
                )
                return False, None, initial_base_risk_usd_planned, "LOW_RR"
            logger.debug(
                f"{log_prefix} Price R/R check PASSED: {rr_ratio_prices:.2f} >= {self.min_rr_ratio:.2f}"
            )
        elif skip_rr_for_dca_grid:
            logger.info(
                f"{log_prefix} R/R check SKIPPED (DCA/Grid management strategy)."
            )
        else:
            logger.info(f"{log_prefix} R/R check SKIPPED (no stop loss mode).")

        # 6. Applying exchange filters to working_quantity_d
        step_size_d = (
            Decimal(str(lot_params.get("stepSize", "0")))
            if lot_params
            else Decimal("0")
        )
        min_qty_d = (
            Decimal(str(lot_params.get("minQty", "0"))) if lot_params else Decimal("0")
        )
        max_qty_float = (
            float(lot_params.get("maxQty", float("inf")))
            if lot_params
            else float("inf")
        )
        min_notional_d = (
            Decimal(str(min_notional_usd))
            if min_notional_usd is not None
            else Decimal("0")
        )

        if step_size_d > Decimal("0"):
            prev_qty_before_step = working_quantity_d
            working_quantity_d = (working_quantity_d / step_size_d).quantize(
                Decimal("0"), rounding=ROUND_DOWN
            ) * step_size_d
            logger.debug(
                f"{log_prefix} Qty after stepSize ({step_size_d}): {prev_qty_before_step:.8f} -> {working_quantity_d:.8f}"
            )

        if float(working_quantity_d) > max_qty_float:  # Comparison with maxQty
            logger.warning(
                f"{log_prefix} Working qty {working_quantity_d:.8f} > maxQty {max_qty_float}. Clamping to maxQty."
            )
            working_quantity_d = Decimal(str(max_qty_float))
            if step_size_d > Decimal("0"):  # Re-rounding
                working_quantity_d = (working_quantity_d / step_size_d).quantize(
                    Decimal("0"), rounding=ROUND_DOWN
                ) * step_size_d
            logger.debug(
                f"{log_prefix} Qty after maxQty clamp and re-step: {working_quantity_d:.8f}"
            )

        if working_quantity_d < min_qty_d:
            logger.debug(
                f"{log_prefix} Working qty {working_quantity_d:.8f} < minQty {min_qty_d:.8f}. Attempting to adjust to minQty."
            )
            # Attempt to use minQty if it fits within BOTH limits: maximum notional AND maximum risk
            if min_qty_d * entry_price_d <= Decimal(
                str(max_notional_for_position_usd)
            ) and min_qty_d * stop_loss_distance_d <= Decimal(
                str(target_max_risk_usd)
            ):  # Comparing with target_max_risk_usd (already with S/S)
                working_quantity_d = min_qty_d
                logger.info(
                    f"{log_prefix} Adjusted qty to minQty: {working_quantity_d:.8f} (within notional and target risk limits)."
                )
            else:
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: Cannot use minQty ({min_qty_d:.8f}) as it would violate "
                    f"max_notional_for_pos (${max_notional_for_position_usd:.2f} vs needed ${min_qty_d * entry_price_d:.2f}) "
                    f"or target_max_risk (${target_max_risk_usd:.2f} vs needed ${min_qty_d * stop_loss_distance_d:.2f})."
                )
                return False, None, initial_base_risk_usd_planned, "MIN_QTY_VIOLATION"

        if working_quantity_d <= Decimal(
            "1e-9"
        ):  # If after all rounding/checks the quantity becomes zero
            logger.warning(
                f"{log_prefix} Signal REJECTED. Reason: Quantity became zero or negative ({working_quantity_d:.8f}) after minQty/maxQty/stepSize adjustments."
            )
            return False, None, initial_base_risk_usd_planned, "ZERO_QTY_AFTER_ADJUST"

        # Checking minNotional
        current_notional_d = working_quantity_d * entry_price_d
        if min_notional_d > Decimal("0") and current_notional_d < min_notional_d:
            logger.debug(
                f"{log_prefix} Current notional {current_notional_d:.2f} (qty {working_quantity_d:.8f}) < minNotional {min_notional_d:.2f}. Attempting adjustment."
            )
            required_qty_for_min_notional_d = min_notional_d / entry_price_d
            if step_size_d > Decimal("0"):
                required_qty_for_min_notional_d = (
                    required_qty_for_min_notional_d / step_size_d
                ).quantize(Decimal("0"), rounding=ROUND_UP) * step_size_d

            if (
                lot_params and required_qty_for_min_notional_d < min_qty_d
            ):  # Ensure it is not less than minQty
                required_qty_for_min_notional_d = min_qty_d
                logger.debug(
                    f"{log_prefix} Qty for minNotional was < minQty, adjusted to minQty: {required_qty_for_min_notional_d:.8f}"
                )

            # Check if this new quantity fits within BOTH limits: max notional AND max risk
            if required_qty_for_min_notional_d * entry_price_d <= Decimal(
                str(max_notional_for_position_usd)
            ) and required_qty_for_min_notional_d * stop_loss_distance_d <= Decimal(
                str(target_max_risk_usd)
            ):  # Comparing with target_max_risk_usd
                working_quantity_d = required_qty_for_min_notional_d
                logger.info(
                    f"{log_prefix} Adjusted qty to {working_quantity_d:.8f} to meet minNotional (within notional and target risk limits)."
                )
            else:
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: Cannot adjust qty for minNotional as it would violate "
                    f"max_notional_for_pos (${max_notional_for_position_usd:.2f} vs needed ${required_qty_for_min_notional_d * entry_price_d:.2f}) "
                    f"or target_max_risk (${target_max_risk_usd:.2f} vs needed ${required_qty_for_min_notional_d * stop_loss_distance_d:.2f})."
                )
                return (
                    False,
                    None,
                    initial_base_risk_usd_planned,
                    "MIN_NOTIONAL_VIOLATION",
                )

        final_quantity_float = float(working_quantity_d)
        if final_quantity_float <= 1e-9:  # Final quantity check
            logger.warning(
                f"{log_prefix} Signal REJECTED. Reason: Final quantity is zero or negative ({final_quantity_float:.8f}) after all adjustments."
            )
            return False, None, initial_base_risk_usd_planned, "ZERO_QTY_FINAL"

        # 7. Dollar R/R check (if the parameter is enabled and there is a stop loss)
        min_dollar_rr_ratio_config = getattr(
            config, "RISK_MANAGER_MIN_DOLLAR_RR_RATIO", None
        )
        if (
            min_dollar_rr_ratio_config is not None
            and stop_loss_price is not None
            and not skip_rr_for_dca_grid
        ):
            min_dollar_rr_decimal = Decimal(str(min_dollar_rr_ratio_config))
            potential_profit_value_d = working_quantity_d * profit_distance_d
            actual_loss_value_d_for_dollar_rr = (
                working_quantity_d * stop_loss_distance_d
            )

            if actual_loss_value_d_for_dollar_rr > Decimal("1e-9"):
                dollar_rr_ratio_actual = (
                    potential_profit_value_d / actual_loss_value_d_for_dollar_rr
                )
                if dollar_rr_ratio_actual < min_dollar_rr_decimal:
                    logger.warning(
                        f"{log_prefix} Signal REJECTED. Reason: Dollar R/R {dollar_rr_ratio_actual:.2f} "
                        f"(Profit: ${potential_profit_value_d:.2f}, Risk: ${actual_loss_value_d_for_dollar_rr:.2f}) "
                        f"< min required {min_dollar_rr_decimal:.2f}."
                    )
                    return False, None, initial_base_risk_usd_planned, "LOW_DOLLAR_RR"
            elif potential_profit_value_d <= Decimal(
                "0"
            ):  # If risk is zero and profit is not positive
                logger.warning(
                    f"{log_prefix} Signal REJECTED. Reason: Dollar R/R check failed. Risk is zero, but potential profit (${potential_profit_value_d:.2f}) is not positive."
                )
                return False, None, initial_base_risk_usd_planned, "ZERO_RISK_NO_PROFIT"
            logger.debug(
                f"{log_prefix} Dollar R/R check PASSED (if enabled). Ratio: {dollar_rr_ratio_actual if actual_loss_value_d_for_dollar_rr > 1e-9 else 'N/A'}"
            )
        elif min_dollar_rr_ratio_config is not None and skip_rr_for_dca_grid:
            logger.info(
                f"{log_prefix} Dollar R/R check SKIPPED (DCA/Grid management strategy)."
            )
        elif min_dollar_rr_ratio_config is not None:
            logger.info(f"{log_prefix} Dollar R/R check SKIPPED (no stop loss mode).")

        # Final calculation of actual risk for logging
        actual_risk_usd_final_trade = float(working_quantity_d * stop_loss_distance_d)

        logger.info(
            f"{log_prefix} Signal APPROVED. Final Qty: {final_quantity_float:.8f}. "
            f"InitialBaseRiskPlannedUSD: ${initial_base_risk_usd_planned:.2f}, "
            f"TargetMaxRiskForSizingUSD: ${target_max_risk_usd:.2f} (this was used for qty calc), "
            f"ActualFinalTradeRiskUSD: ${actual_risk_usd_final_trade:.2f} (this is the real $ risk with final qty)."
        )
        logger.info(f"{log_prefix} --- ASSESSMENT FINISHED: APPROVED ---")

        return True, final_quantity_float, initial_base_risk_usd_planned, None

    async def _load_performance_from_db(self):
        if not crud or not self.db_session:
            logger.warning(
                "CRUD or DB session not available, skipping loading performance state from DB."
            )
            return

        try:
            logger.info(
                f"Loading symbol-strategy performance state from DB for user_id: {self.user_id}"
            )
            performance_records = await crud.get_all_symbol_strategy_performance(
                db=self.db_session, user_id=self.user_id
            )

            count = 0
            for record in performance_records:
                perf_key = (record.symbol, record.strategy_name)

                # JSON deserialization back to deque
                buffer_list = json.loads(record.trade_results_buffer_json)
                buffer_deque = deque(
                    buffer_list, maxlen=self._strategy_symbol_window_size
                )

                # Recreating statistics object
                stats_obj = SymbolStrategyPerformanceStats(
                    trade_results_buffer=buffer_deque,
                    current_risk_multiplier_index=record.current_risk_multiplier_index,
                    last_penalty_timestamp=record.last_penalty_timestamp,
                    total_trades_for_assessment=record.total_trades_for_assessment,
                    total_pnl_usd=getattr(record, "total_pnl_usd", 0.0),
                )

                # Recalculating derivative fields based on the buffer
                stats_obj.current_pnl_sum_usd = sum(
                    p for p, r in stats_obj.trade_results_buffer
                )
                stats_obj.sum_initial_risk_usd_in_window = sum(
                    r for p, r in stats_obj.trade_results_buffer
                )
                stats_obj.current_wins_in_window = sum(
                    1 for p, r in stats_obj.trade_results_buffer if p > 0
                )
                stats_obj.current_trades_in_window = len(stats_obj.trade_results_buffer)

                self._symbol_strategy_performance[perf_key] = stats_obj
                count += 1

            logger.info(
                f"Successfully loaded {count} performance records from DB for user_id: {self.user_id}"
            )

        except Exception as e:
            logger.error(
                f"Failed to load performance state from DB for user_id: {self.user_id}. Error: {e}",
                exc_info=True,
            )

    async def _save_performance_to_db(
        self, symbol: str, strategy_name: str, stats: SymbolStrategyPerformanceStats
    ):
        if not crud or not self.db_session:
            logger.warning(
                "CRUD or DB session not available, skipping saving performance state to DB."
            )
            return

        try:
            # Preparing data for saving to the DB
            performance_data = {
                "symbol": symbol,
                "strategy_name": strategy_name,
                "trade_results_buffer_json": json.dumps(
                    list(stats.trade_results_buffer)
                ),
                "current_risk_multiplier_index": stats.current_risk_multiplier_index,
                "last_penalty_timestamp": stats.last_penalty_timestamp,
                "total_trades_for_assessment": stats.total_trades_for_assessment,
                "total_pnl_usd": stats.total_pnl_usd,
            }

            # Call the CRUD function to create or update a record
            await crud.update_or_create_symbol_strategy_performance(
                db=self.db_session,
                user_id=self.user_id,
                performance_data=performance_data,
            )
            logger.debug(
                f"Saved performance for {symbol}-{strategy_name} to DB for user_id: {self.user_id}"
            )

        except Exception as e:
            logger.error(
                f"Failed to save performance state to DB for user_id: {self.user_id}, {symbol}-{strategy_name}. Error: {e}",
                exc_info=True,
            )

    def get_pnl_for_strategy(self, symbol: str, strategy_name: str) -> float:
        """
        Returns the cumulative PnL for a specific symbol/strategy pair
        for the entire duration of the current bot session.

        Data is loaded from the DB at startup and updated after each trade.
        """
        perf_stats = self._symbol_strategy_performance.get((symbol, strategy_name))
        if perf_stats:
            # Returning a new field with total PnL, not PnL from the window
            return perf_stats.total_pnl_usd
        return 0.0

    # ------------------------------------------------------------------
    # Trading-guard state persistence
    # ------------------------------------------------------------------
    #
    # `TradeStats.consecutive_losses` and `TradeStats.today_pnl` used to live
    # in RAM only: `save_state()` was a no-op (`pass`), so the numbers that
    # decide whether the account may trade were born again at zero on every
    # process start.
    #
    # Measured in production on 2026-10-21: a routine deploy restart moved a
    # blocked account's `consecutive_losses` from 10 to 0 and the trading gate
    # from blocked to allowed. The account was correctly blocked by the loss
    # streak, and a deploy silently un-blocked it.
    #
    # The state is mirrored into a DEDICATED Redis key
    # (`REDIS_STATE_KEY_RISK`). It is NOT written into
    # `REDIS_STATE_KEY_PORTFOLIO`: that key is the published view of the
    # account and PR #25 fixed the API's reading of it, so its shape must not
    # change.
    #
    # WHAT IS PERSISTED
    #   * consecutive_losses -- the loss streak. This is the counter that
    #     closes the gate, so it is the whole point of the change.
    #   * today_pnl -- the day's realised PnL, which the daily-loss and
    #     drawdown limits are computed from.
    #   * start_of_day_balance -- the day's baseline. It travels with
    #     today_pnl because those limits are a RATIO against it: a PnL
    #     without its baseline would be measured against the wrong
    #     denominator and could close (or open) the gate on a fiction.
    #   * day_str -- the UTC date the numbers belong to, so the daily
    #     rollover cannot resurrect a previous day's PnL.
    #
    # WHAT IS DELIBERATELY NOT PERSISTED
    #   * The emergency-stop latch. It is ALREADY durable: the API writes
    #     `emergency_stop:{user_id}` and the controller restores it, with a
    #     deliberate distinction between an operator's kill switch and an
    #     infrastructure failure. A second copy here would be a second source
    #     of truth that could resurrect a stop the operator had cleared.
    #   * The trading-disabled latch itself. It is DERIVED: the gate is
    #     recomputed by _check_risk_limits() from the restored numbers, so a
    #     persisted latch could only ever be a stale one.
    #   * The balance. It is live-fetched from the exchange and is not a
    #     persisted quantity.
    #   * current_trading_day_start_ts. It is written and never read
    #     anywhere in the codebase, so persisting it would be noise.

    def _risk_state_redis_key(self) -> str:
        """The dedicated Redis key holding THIS controller's guard state.

        Scoped per user AND per api key, exactly like the portfolio key
        (``depthsight:state:portfolio:{user_id}:{api_key_id}``), so two
        controllers of the same user cannot overwrite each other's streak.
        The streak is a PER-CONTROLLER quantity -- the API reports the MAX
        across controllers -- so one shared key would make a controller
        restore another controller's counter.
        """
        return f"{self.redis_state_key}:{self.user_id}:{self.api_key_id}"

    def _guard_seen_redis_key(self) -> str:
        """A SEPARATE key recording that this controller HAS traded before.

        WHY THIS EXISTS. A missing guard key was treated as a first boot
        (`restore_state` returned False with `_guard_state_unverified = False`),
        so `initialize_balance` zeroed `today_pnl` and `consecutive_losses` and
        the gate re-opened. But "no key" and "never had a key" are different
        facts: the first is a LOST state, the second is a fresh account. The
        guard key alone cannot tell them apart -- it is the thing that went
        missing.

        So the "has this controller ever persisted guard state?" answer is
        stored under its own key, written the first time `save_state`
        succeeds and never overwritten. A missing guard key next to a PRESENT
        marker is a lost state and fails closed; missing next to missing is a
        first boot and still trades. A brand-new account is therefore not
        bricked.

        LIMITATION, stated rather than hidden: under a memory-based eviction
        policy this marker and the guard key are evicted together, so this does
        not cover a Redis-wide flush -- it covers a lost or deleted guard key.
        It is a strict improvement over "always open", not a proof.
        """
        return f"{self._risk_state_redis_key()}:seen"

    def _current_utc_day_str(self) -> str:
        """The UTC calendar date the current trading day belongs to."""
        return self._get_current_day_start_info()[1]

    async def save_state(self):
        """Persist the trading-guard state so it survives a restart.

        The controller AWAITS this on its periodic tick, and the Redis write
        is awaited too (bounded by a hard timeout). It is deliberately NOT
        fire-and-forget: the guarantee only holds if what is on disk is what
        the controller believes in. A failed write is logged CRITICAL and
        swallowed -- a Redis outage must not take the trading loop down -- but
        it is never silent.
        """
        async with self._risk_state_write_lock:
            if self._guard_state_unverified:
                # The in-memory counters are NOT trustworthy: the process was
                # born with a zero streak because the persisted state could
                # not be read. Writing them now would DESTROY the persisted
                # truth, so read it again first and write only what that
                # read returns.
                if not await self._reverify_guard_state():
                    return

            if getattr(self, "_guard_state_lost", False):
                # The persisted state is GONE, not merely unreadable: today's
                # PnL and the loss streak are UNKNOWN, not zero. The
                # in-memory counters are the zeros this process was born
                # with, so writing them now would LAUNDER an unknown state
                # into a payload -- and the next start-up would read that
                # payload as "the streak really is empty" and OPEN the gate.
                #
                # This is the same hazard the `_guard_state_unverified` branch
                # above guards against, in its permanent form: that latch
                # heals on the next successful read, this one cannot, because
                # there is nothing left to read. So while it is set, nothing
                # is persisted, the state stays lost, and the next start-up
                # detects the loss again and fails closed. The latch is
                # released at the UTC day rollover, where the day's counters
                # legitimately restart at zero.
                #
                # Found by the DSH reviewer and verified against the tree at
                # 035b0d2: without this guard the controller's unconditional
                # periodic save defeated the latch one tick later.
                logger.critical(
                    "[RiskGuard] NOT persisting the trading-guard state: it was "
                    f"lost on UTC day {self._guard_state_lost_day_str or 'unknown'} "
                    "and the in-memory counters are UNKNOWN, not zero. Writing "
                    "them would make the next start-up read the loss as an "
                    "empty streak and open the gate. The lost-state latch "
                    "holds until the UTC day rolls over."
                )
                return

            if not self.redis_client:
                logger.critical(
                    "[RiskGuard] No redis_client; the trading-guard state will "
                    "NOT survive a restart. Fix the Redis connection."
                )
                return

            payload = {
                "version": RISK_STATE_VERSION,
                "day_str": self.stats.last_known_day_str
                or self._current_utc_day_str(),
                "consecutive_losses": int(self.stats.consecutive_losses),
                "today_pnl": float(self.stats.today_pnl),
                "start_of_day_balance": float(self.stats.start_of_day_balance),
                # WHICH DAY THE BASE BELONGS TO. Without this the reader can
                # only check `day_str`, which describes the streak and the
                # day's PnL -- not this number. A payload whose base was
                # captured on an earlier day but whose `day_str` says today
                # used to be accepted, and then re-blessed on every subsequent
                # restore, so the base stayed in force for the life of the
                # account while the daily-loss gate measured a real loss
                # against a denominator from days ago.
                "start_of_day_day_str": self.stats.start_of_day_day_str or "",
                "saved_at_utc": datetime.now(timezone.utc).isoformat(),
                # THE PAPER SANDBOX'S STATE, IN THE SAME VALUE, UNDER THE SAME
                # KEY.
                #
                # The key is `f"{redis_state_key}:{user_id}:{api_key_id}"` --
                # per user and per api key, and NOT per mode. Adding a mode
                # dimension to the KEY would mean the state that is already
                # persisted under the old key is no longer found, which
                # silently RESETS the trading guard for every account on
                # deploy. So both sandboxes' state lives in ONE value: the
                # LIVE counters keep their existing top-level names (so a
                # payload written before this change is still read correctly,
                # with no reset), and the PAPER counters sit under "paper".
                #
                # A payload that predates this change has no "paper" key; the
                # paper counters then start at zero, which is CORRECT -- the
                # paper sandbox was never persisted before.
                "paper": {
                    "day_str": self.paper_stats.last_known_day_str or "",
                    "consecutive_losses": int(self.paper_stats.consecutive_losses),
                    "today_pnl": float(self.paper_stats.today_pnl),
                    "start_of_day_balance": float(
                        self.paper_stats.start_of_day_balance
                    ),
                    "start_of_day_day_str": (
                        self.paper_stats.start_of_day_day_str or ""
                    ),
                },
            }
            try:
                await asyncio.wait_for(
                    self.redis_client.set(
                        self._risk_state_redis_key(),
                        json.dumps(payload, default=str),
                    ),
                    timeout=RISK_STATE_PERSIST_TIMEOUT_SECONDS,
                )
                logger.debug(
                    "[RiskGuard] Persisted the trading-guard state: "
                    "consecutive_losses=%s today_pnl=%.2f (UTC day %s).",
                    payload["consecutive_losses"],
                    payload["today_pnl"],
                    payload["day_str"],
                )
                # Record that this controller has persisted guard state at
                # least once. Written only on a SUCCESSFUL guard write, and
                # never overwritten, so it answers "has this account traded
                # before?" even after the guard key itself is lost.
                try:
                    await asyncio.wait_for(
                        self.redis_client.set(self._guard_seen_redis_key(), "1"),
                        timeout=RISK_STATE_PERSIST_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    # Not fatal to this save -- the guard state IS on disk.
                    # But the lost-state discriminator just got weaker, so it
                    # is logged rather than swallowed.
                    logger.warning(
                        "[RiskGuard] FAILED to write the 'has traded before' "
                        f"marker {self._guard_seen_redis_key()}: {exc}. A "
                        "subsequent loss of the guard state will be "
                        "indistinguishable from a first boot.",
                    )
            except Exception as exc:
                logger.critical(
                    f"[RiskGuard] FAILED to persist the trading-guard state to "
                    f"{self._risk_state_redis_key()}: {exc}. The loss streak "
                    "will NOT survive a restart.",
                    exc_info=True,
                )

    async def restore_state(self) -> bool:
        """Restore the trading-guard state a previous process persisted.

        Called ONCE at start-up, before anything can trade. Returns True when
        a state was restored.

        FAIL-CLOSED. If the state cannot be read the gate stays closed until
        it can be: a verification failure must never turn into permission to
        trade. Unlike the operator's emergency stop, this latch is an
        INFRASTRUCTURE fault and is released automatically as soon as the
        state can be read again (see `_reverify_guard_state`), so a Redis
        blip costs availability for as long as Redis is down and not a human
        decision.
        """
        payload, error = await self._read_persisted_guard_state()
        if error is not None:
            self._guard_state_unverified = True
            self._is_trading_allowed = False
            self._global_gate_disable_reason = RISK_GUARD_UNVERIFIED_REASON
            if not self._emergency_stop_active:
                self._trading_disabled_reason = RISK_GUARD_UNVERIFIED_REASON
            logger.critical(
                f"[RiskGuard] FAILED to read the trading-guard state from "
                f"{self._risk_state_redis_key()}: {error}. Trading stays "
                "DISABLED until the state can be read (fail-closed)."
            )
            return False

        self._guard_state_unverified = False
        if payload is None:
            # Nothing persisted. Is that a FIRST BOOT or a LOST state? Those
            # are different facts with opposite consequences, and the guard
            # key alone cannot tell them apart -- it is the thing that is
            # missing. The "has traded before" marker can: it is written under
            # its own key and is never part of the payload, so it survives the
            # loss of the guard key it describes.
            #
            # A first boot must still be able to trade, so this is deliberately
            # NOT "always fail closed on a missing key".
            traded_before = await self._has_traded_before()
            if traded_before is not False:
                # `None` means the marker could not be READ. That is not a
                # first boot -- it is an unknown -- and an unknown fails
                # closed exactly like a known loss.
                self._mark_guard_state_lost()
                if traded_before:
                    logger.critical(
                        "[RiskGuard] The persisted trading-guard state is MISSING for "
                        f"{self._risk_state_redis_key()}, but "
                        f"{self._guard_seen_redis_key()} says this controller has "
                        "traded before. This is a LOST state, not a first boot: the "
                        "loss streak and the day's PnL cannot be shown to be zero. "
                        "Trading stays DISABLED (fail-closed) until the state is "
                        "persisted again."
                    )
                else:
                    logger.critical(
                        "[RiskGuard] The persisted trading-guard state is MISSING for "
                        f"{self._risk_state_redis_key()} and the 'has traded before' "
                        f"marker {self._guard_seen_redis_key()} could NOT be read. "
                        "Whether this is a first boot or a LOST state cannot be "
                        "established, so the loss streak and the day's PnL cannot be "
                        "shown to be zero. Trading stays DISABLED (fail-closed)."
                    )
                return False
            logger.info(
                "[RiskGuard] No persisted trading-guard state and no "
                "'has traded before' marker: starting with an empty loss "
                "streak. This is a first boot, not a lost state."
            )
            return False
        return self._apply_persisted_guard_state(payload)

    def _guard_state_is_unknown(self) -> bool:
        """Is the persisted trading-guard state UNKNOWN?

        True when the state could not be read at start-up, or when it has
        VANISHED for an account that has traded before. In both cases today's
        PnL and the loss streak cannot be shown to be within limits, so the
        gate must stay closed until the state can be established -- a
        restore, a successful re-read, or a UTC day strictly later than the
        day the state was lost.

        This is the single predicate for "may the gate be re-opened?". It
        exists because three separate call sites had each grown their own
        version of that question, and each of them had got it wrong in a
        different way.
        """
        return bool(
            getattr(self, "_guard_state_unverified", False)
            or getattr(self, "_guard_state_lost", False)
        )

    def _mark_guard_state_lost(self) -> None:
        """Latch the fail-closed state for a guard key that has VANISHED.

        Today's PnL and the loss streak cannot be shown to be zero, so the
        gate closes and stays closed. The latch is released at the UTC day
        rollover (see `_check_and_reset_daily_stats`), which is the one moment
        the day's counters restart at zero by definition -- but ONLY on a
        rollover to a STRICTLY LATER day than the day the state was lost. A
        fresh process sees its first day-key change on the same start that
        set the latch, so 'release on any day change' would clear it while
        today's loss is still unknown. The rollover's own comment repeats
        this, because the two rules are easy to conflate.

        Both places that can observe a missing guard key call this, so they
        cannot drift apart.
        """
        self._guard_state_lost = True
        self._guard_state_lost_day_str = self._current_utc_day_str()
        self._is_trading_allowed = False
        self._global_gate_disable_reason = RISK_GUARD_STATE_LOST_REASON
        if not self._emergency_stop_active:
            self._trading_disabled_reason = RISK_GUARD_STATE_LOST_REASON

    async def _has_traded_before(self) -> Optional[bool]:
        """Has THIS controller ever successfully persisted a guard state?

        Returns:
            True  -- the marker is PRESENT, so a missing guard key means the
                     state was LOST.
            False -- the marker is ABSENT, so a missing guard key really does
                     mean this account has never traded: a first boot.
            None  -- the marker could NOT be read. The answer is UNKNOWN.

        The third state is the one that matters. An unreadable discriminator
        must not be read as "first boot": that is precisely the path on which
        the gate re-opens on a zero streak.

        Failing closed on `None` does NOT brick a new account. A brand-new
        account's marker is simply ABSENT, which is `False`, not `None`. Only
        a genuinely unreadable marker yields `None`, and by then the guard-key
        read has already proven that Redis is at least partly working, so
        this is a partial failure -- not a first boot.
        """
        if not self.redis_client:
            return None
        try:
            raw = await asyncio.wait_for(
                self.redis_client.get(self._guard_seen_redis_key()),
                timeout=RISK_STATE_PERSIST_TIMEOUT_SECONDS,
            )
        except Exception:
            return None
        return bool(raw)

    async def _read_persisted_guard_state(
        self,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Read the persisted trading-guard state.

        Returns `(payload, error)`. `payload` is None when nothing is
        persisted (a first boot, or a bot that never traded). `error` is a
        human-readable string when the state could NOT be read -- the caller
        must then fail closed.
        """
        if not self.redis_client:
            return None, "there is no redis client"
        key = self._risk_state_redis_key()
        try:
            raw = await asyncio.wait_for(
                self.redis_client.get(key),
                timeout=RISK_STATE_PERSIST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None, (
                f"the read timed out after {RISK_STATE_PERSIST_TIMEOUT_SECONDS}s"
            )
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

        if not raw:
            return None, None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            return None, f"the persisted payload is not valid JSON ({exc})"
        if not isinstance(payload, dict):
            return None, "the persisted payload is not a JSON object"
        return payload, None

    def _apply_persisted_guard_state(self, payload: Dict[str, Any]) -> bool:
        """Apply a persisted guard state to this RiskManager.

        Returns True when a state was applied, False when it was discarded
        because it belongs to a previous UTC day.
        """
        current_day_str = self._current_utc_day_str()
        persisted_day_str = str(payload.get("day_str") or "")
        if persisted_day_str != current_day_str:
            # The daily rollover resets the loss streak and the day's PnL at
            # the UTC day boundary. A previous day's numbers must not be
            # resurrected: doing so would re-block an account whose streak
            # was legitimately cleared, and would carry a stale PnL into a
            # new day.
            logger.info(
                "[RiskGuard] Discarding the persisted trading-guard state: it "
                "belongs to UTC day %r and today is %r. The daily rollover "
                "clears the loss streak and the day's PnL at the UTC day "
                "boundary.",
                persisted_day_str,
                current_day_str,
            )
            # A previous-day payload PROVES this account has traded, so its
            # live balance is a MID-DAY balance and cannot be today's opening
            # balance. Any base already stamped with TODAY's day key that is
            # UNBACKED is therefore invalidated here -- otherwise this early
            # return leaves a mid-day base in force with the gate open on it.
            # The review panel measured exactly that: base=865.23,
            # usable=True, trading ALLOWED=True, the gate reading 0.0000%
            # against a true 4.4188% loss.
            #
            # A base this process VOUCHED for is kept: one captured at a
            # genuine UTC rollover, one captured for an account that had never
            # traded, and one restored from a payload that says it belongs to
            # today. Those ARE the day's opening balance, and dropping them
            # would leave the account with no denominator at all. An UNBACKED
            # base -- a capture made while the persisted state could not be
            # read -- is a GUESS and is dropped.
            if (
                getattr(self.stats, "start_of_day_day_str", None) == current_day_str
                and self._opening_base_is_unbacked()
            ):
                logger.critical(
                    "[RiskGuard] Invalidating the opening balance: the "
                    "persisted state belongs to UTC day %r, so it proves this "
                    "account has traded and today's opening balance is "
                    "UNKNOWN. The base stamped with today's day key ($%.8f) "
                    "is unbacked -- it was not captured at a UTC rollover, not "
                    "restored from a payload that says it belongs to today, "
                    "and not captured for an account that has never traded -- "
                    "so it is dropped and the daily-loss gate fails CLOSED.",
                    persisted_day_str,
                    float(self.stats.start_of_day_balance or 0.0),
                )
                self.stats.start_of_day_balance = 0.0
                self.stats.start_of_day_day_str = ""
                self._start_of_day_base_provenance = None
                # Re-evaluate the gate: it must not stay OPEN on a base that
                # has just been invalidated.
                self._check_risk_limits()
            # The account has traded, so its live balance is a mid-day balance
            # and must never be installed as today's opening balance.
            self._account_has_traded_before = True
            self._account_history_resolved = True
            return False

        try:
            consecutive_losses = int(payload.get("consecutive_losses") or 0)
        except (TypeError, ValueError):
            consecutive_losses = 0
        try:
            today_pnl = float(payload.get("today_pnl") or 0.0)
        except (TypeError, ValueError):
            today_pnl = 0.0
        try:
            start_of_day_balance = float(payload.get("start_of_day_balance") or 0.0)
        except (TypeError, ValueError):
            start_of_day_balance = 0.0

        # The streak only ever grows within a UTC day (it is reset by a win
        # or by the daily rollover), so the higher of the persisted and the
        # in-memory value is the conservative -- and, after a failed restore,
        # the more likely correct -- one.
        self.stats.consecutive_losses = max(
            0,
            consecutive_losses,
            int(self.stats.consecutive_losses or 0),
        )
        self.stats.today_pnl = today_pnl

        # THE BASE MUST CARRY ITS OWN DAY. `day_str` above describes the STREAK
        # and the day's PnL. It says nothing about which day
        # `start_of_day_balance` was captured on, and the two were independent:
        # a base captured on one day could ride along under a later day's stamp
        # and be accepted forever. Each restore then stamped `last_known_day_str
        # = current_day_str` onto it, which both re-blessed the stale number as
        # current AND suppressed the daily re-baseline in `initialize_balance`
        # (whose `guard_state_restored_today` branch skips it). The base
        # therefore survived indefinitely while the daily-loss gate measured a
        # real loss against a denominator from days earlier -- which
        # UNDER-TRIPS: the same loss read 5.75% on the stale base and 8.07% on
        # the true one.
        #
        # So the base is restored ONLY when the payload says it belongs to
        # today. A payload without the field (any v1 payload) is UNKNOWN, not
        # known-good, and is treated the same way -- `initialize_balance` then
        # leaves the base unusable and the daily-loss gate fails CLOSED
        # (`RISK_GUARD_BASE_UNKNOWN_REASON`) until the UTC rollover captures
        # the real opening balance. It is NOT re-derived from the live
        # balance: that balance is a mid-day balance which already contains
        # today's PnL, and restating the day's loss against a denominator
        # that contains it reads LOW.
        #
        # `today_pnl` IS still restored: it is day-scoped by `day_str`, which
        # was checked above, and discarding it would measure nothing at all and
        # open the gate wider. Restoring it against a correctly re-derived base
        # is exactly the measurement the control is supposed to make.
        persisted_base_day_str = str(payload.get("start_of_day_day_str") or "")
        base_is_from_today = (
            persisted_base_day_str == current_day_str and start_of_day_balance > 0
        )
        if base_is_from_today:
            self.stats.start_of_day_balance = start_of_day_balance
            self.stats.start_of_day_day_str = current_day_str
            # The payload says this base belongs to today, so it is backed:
            # a later previous-day payload must not invalidate it.
            self._start_of_day_base_provenance = "restored"
        else:
            logger.critical(
                "[RiskGuard] NOT restoring the persisted start-of-day balance "
                f"(${start_of_day_balance:.2f}): it belongs to UTC day "
                f"{persisted_base_day_str or 'UNKNOWN (pre-v2 payload)'} and today "
                f"is {current_day_str}. A base from an earlier day makes the "
                "daily-loss and drawdown gates measure against the wrong "
                "denominator, which reads LOW and under-trips. Today's opening "
                "balance stays UNKNOWN and the gate fails CLOSED until the UTC "
                "rollover captures the real one.",
            )
            # If an UNBACKED base was written BEFORE this restore, it is a
            # GUESS and must be invalidated: it was captured while the
            # persisted state could not be read, so it may already contain
            # today's PnL. That is what happens in the "init-first" call
            # order: `initialize_balance()` runs before the state is read, so
            # the capture it makes is a guess. Dropping it leaves today's
            # opening balance UNKNOWN and the gate fails closed.
            #
            # A base this process VOUCHED for -- captured at a genuine UTC
            # rollover, captured for an account that had never traded, or
            # restored from a payload that says it belongs to today -- is the
            # day's REAL opening balance and is KEPT. The persisted base here
            # belongs to another day and is refused, but the one in force is
            # measured against the right denominator, and throwing it away
            # would block a healthy account for no gain.
            if (
                getattr(self.stats, "start_of_day_day_str", None) == current_day_str
                and self._opening_base_is_unbacked()
            ):
                logger.critical(
                    "[RiskGuard] Invalidating an opening balance that was "
                    "written before this restore ($%.2f): it is UNBACKED -- it "
                    "was captured while the persisted state could not be read, "
                    "so it may already contain today's PnL and it is not the "
                    "day's opening balance.",
                    self.stats.start_of_day_balance,
                )
                self.stats.start_of_day_balance = 0.0
                self.stats.start_of_day_day_str = ""
                self._start_of_day_base_provenance = None

        self.stats.last_known_day_str = current_day_str
        self._guard_state_restored = True

        # THE PAPER SANDBOX'S OWN STATE, restored from the SAME payload.
        # It lives under the "paper" key, so the LIVE counters above keep
        # their existing top-level names and a payload written before this
        # change still restores the live state correctly -- there is NO
        # guard reset on deploy. A payload without a "paper" key simply
        # leaves the paper counters at zero, which is correct: the paper
        # sandbox was never persisted before.
        paper_payload = payload.get("paper")
        if isinstance(paper_payload, dict):
            paper_day_str = str(paper_payload.get("day_str") or "")
            if paper_day_str == current_day_str:
                try:
                    paper_consecutive_losses = int(
                        paper_payload.get("consecutive_losses") or 0
                    )
                except (TypeError, ValueError):
                    paper_consecutive_losses = 0
                try:
                    paper_today_pnl = float(paper_payload.get("today_pnl") or 0.0)
                except (TypeError, ValueError):
                    paper_today_pnl = 0.0
                try:
                    paper_start_of_day_balance = float(
                        paper_payload.get("start_of_day_balance") or 0.0
                    )
                except (TypeError, ValueError):
                    paper_start_of_day_balance = 0.0
                self.paper_stats.consecutive_losses = max(0, paper_consecutive_losses)
                self.paper_stats.today_pnl = paper_today_pnl
                if paper_start_of_day_balance > 1e-9:
                    self.paper_stats.start_of_day_balance = paper_start_of_day_balance
                    self.paper_stats.start_of_day_day_str = str(
                        paper_payload.get("start_of_day_day_str") or current_day_str
                    )
                self.paper_stats.last_known_day_str = current_day_str
            else:
                logger.info(
                    "[RiskGuard] Discarding the persisted PAPER sandbox state: it "
                    "belongs to UTC day %r and today is %r. The paper counters "
                    "restart at zero, exactly like the live ones.",
                    paper_day_str,
                    current_day_str,
                )

        # Re-evaluate the gate against the restored numbers. The start-up
        # path re-opened the gate on the way in (initialize_balance resets
        # the counters and the gate); this closes it again when the restored
        # streak still breaches the limit.
        self._check_risk_limits()
        # The PAPER gate is re-evaluated against the PAPER counters, which
        # were restored from the same payload.
        self._check_risk_limits(mode="paper")
        if not self._is_trading_allowed and not self._emergency_stop_active:
            if self._global_gate_disable_reason:
                self._trading_disabled_reason = self._global_gate_disable_reason

        logger.critical(
            "[RiskGuard] RESTORED the trading-guard state: consecutive_losses=%s "
            "today_pnl=%.2f (UTC day %s). The trading gate is %s.",
            self.stats.consecutive_losses,
            self.stats.today_pnl,
            current_day_str,
            "CLOSED" if not self._is_trading_allowed else "open",
        )
        return True

    async def _reverify_guard_state(self) -> bool:
        """Try again to read the persisted guard state.

        Releases the fail-closed start-up latch once the state can be read
        again. Returns True when the state could be read, and therefore the
        in-memory counters can be trusted again.
        """
        payload, error = await self._read_persisted_guard_state()
        if error is not None:
            logger.critical(
                f"[RiskGuard] Still cannot read the trading-guard state from "
                f"{self._risk_state_redis_key()}: {error}. Trading stays "
                "DISABLED (fail-closed)."
            )
            return False
        self._guard_state_unverified = False
        if payload is not None:
            applied = self._apply_persisted_guard_state(payload)
            if not applied:
                # The payload belonged to a PREVIOUS UTC day and was
                # DISCARDED: nothing was applied, and
                # `_apply_persisted_guard_state` deliberately skipped its own
                # limit re-evaluation on that path. The unreadable-state
                # latch has just been cleared, so the gate is otherwise left
                # holding a verdict that was computed against a state nobody
                # could read.
                #
                # The in-memory counters are this process's view of TODAY,
                # which the daily rollover legitimately reset to zero, so the
                # gate is re-evaluated against them -- the same treatment the
                # first-boot branch below gets.
                self._check_risk_limits()
        else:
            # Nothing is persisted. That is a LOST state for an account that
            # has traded before and a first boot otherwise -- the SAME
            # distinction `restore_state()` makes.
            #
            # Assuming "the streak really is empty" here skipped the
            # discriminator entirely, so a guard key that vanished during a
            # Redis outage re-opened the gate on the process-born zero
            # streak -- and the zeroed counters were then persisted, which is
            # the laundering the lost-state latch exists to prevent.
            traded_before = await self._has_traded_before()
            if traded_before is not False:
                self._mark_guard_state_lost()
                logger.critical(
                    "[RiskGuard] The persisted trading-guard state is MISSING for "
                    f"{self._risk_state_redis_key()}, but this controller has traded "
                    "before (the 'has traded before' marker is present, or could "
                    "not be read). This is a LOST state, not a first boot: the loss "
                    "streak and the day's PnL cannot be shown to be zero. Trading "
                    "stays DISABLED (fail-closed) until the state is persisted again."
                )
            else:
                # Nothing is persisted, so the streak really is empty.
                self._check_risk_limits()
        # THE OPERATOR-FACING REASON MUST BE THE CURRENT ONE. The gate was
        # re-evaluated above, but `_check_risk_limits()` only rewrites
        # `_trading_disabled_reason` when it CLOSES the gate, and here the
        # gate was ALREADY closed -- by the unreadable-state latch, whose
        # reason is now stale. Without this, a Redis that has recovered is
        # blamed forever, and the real reason the gate is held (for example
        # "today's opening balance is unknown") is never shown.
        #
        # `restore_state()` already does exactly this after its own limit
        # re-evaluation; this is the same fix-up for the same reason on the
        # re-verification path.
        if not self._is_trading_allowed and not self._emergency_stop_active:
            if self._global_gate_disable_reason:
                self._trading_disabled_reason = self._global_gate_disable_reason
        if getattr(self, "_guard_state_lost", False):
            logger.critical(
                "[RiskGuard] The trading-guard state is READABLE again, but it "
                "is MISSING for an account that has traded before: this is a "
                "LOST state, not a first boot. The trading-guard state stays "
                "DISABLED (fail-closed) until the UTC day rolls over."
            )
        else:
            logger.warning(
                "[RiskGuard] The trading-guard state is verifiable again. The "
                "fail-closed start-up latch is released."
            )
        return True

    def _adjust_and_round_quantity(
        self,
        quantity: float,
        symbol: str,
        price: Optional[float],
        lot_params: Optional[Dict],
        min_notional: Optional[float],
    ) -> Optional[float]:
        """Rounds and checks the quantity according to exchange filters."""
        if quantity <= 1e-12:
            return 0.0
        log_prefix = f"[_AdjustQty:{symbol}]"
        adj_qty = quantity

        # Log input data for diagnostics
        logger.debug(
            f"{log_prefix} Input: qty={quantity:.8f}, price={price}, lot_params={lot_params}, min_notional={min_notional}"
        )

        try:
            if lot_params and lot_params.get("stepSize", 0) > 0:
                step = Decimal(str(lot_params["stepSize"]))
                qty_dec = Decimal(str(quantity))
                adj_qty = float(
                    (qty_dec / step).quantize(Decimal("0"), rounding=ROUND_DOWN) * step
                )
                if adj_qty != quantity:
                    logger.debug(
                        f"{log_prefix} Qty {quantity:.8f} rounded to step {step} -> {adj_qty:.8f}"
                    )

            if adj_qty <= 1e-12:
                return 0.0

            min_qty_filter = lot_params.get("minQty", 0) if lot_params else 0
            if adj_qty < min_qty_filter:
                # Extended logging for diagnostics
                logger.warning(
                    f"{log_prefix} REJECTED: Rounded qty {adj_qty:.8f} < minQty {min_qty_filter}. "
                    f"Original qty: {quantity:.8f}, lot_params: {lot_params}"
                )
                return None

            if (
                min_notional is not None
                and min_notional > 0
                and price is not None
                and price > 0
            ):
                notional_value = adj_qty * price
                if notional_value < min_notional:
                    # Extended logging for diagnostics
                    logger.warning(
                        f"{log_prefix} REJECTED: Notional ${notional_value:.2f} (qty={adj_qty:.8f} * price={price:.8f}) < minNotional ${min_notional:.2f}. "
                        f"Original qty: {quantity:.8f}"
                    )
                    return None

            max_qty_filter = (
                lot_params.get("maxQty", float("inf")) if lot_params else float("inf")
            )
            if adj_qty > max_qty_filter:
                logger.warning(
                    f"{log_prefix} Rounded qty {adj_qty:.8f} > maxQty {max_qty_filter}. Clamping."
                )
                adj_qty = max_qty_filter
                if adj_qty < min_qty_filter:
                    return None
                if (
                    min_notional is not None
                    and min_notional > 0
                    and price is not None
                    and price > 0
                ):
                    if (adj_qty * price) < min_notional:
                        return None

            if adj_qty <= 1e-12:
                return 0.0
            return adj_qty

        except Exception as e:
            logger.error(
                f"{log_prefix} Error adjusting quantity {quantity:.8f}: {e}",
                exc_info=True,
            )
            return None

    async def calculate_scaled_in_quantity(
        self,
        position: BasePosition,
        add_size_pct: float,
        current_price: float,
        lot_params: Optional[Dict[str, float]],
        min_notional_usd: Optional[float],
    ) -> Optional[float]:
        log_prefix = f"[RiskManager:ScaleInQty:{position.symbol}]"

        if (
            not position.initial_risk_usd_planned
            or position.initial_risk_usd_planned <= 0
        ):
            logger.error(
                f"{log_prefix} Cannot calculate scale-in quantity without initial_risk_usd_planned."
            )
            return None

        additional_risk_usd = position.initial_risk_usd_planned * (add_size_pct / 100.0)

        # If position is without stop-loss (DCA mode)
        if position.current_sl_price is None:
            # Calculating size based on margin, same as at entry
            new_quantity = additional_risk_usd / current_price
            logger.debug(
                f"{log_prefix} Scale-in NO_STOP_LOSS mode. Target additional notional: ${additional_risk_usd:.2f}. Qty: {new_quantity:.8f}"
            )
        else:
            stop_loss_distance = abs(current_price - position.current_sl_price)

            if stop_loss_distance <= 1e-9:
                logger.error(f"{log_prefix} Stop loss distance is zero or negative.")
                return None

            new_quantity = additional_risk_usd / stop_loss_distance

        return self._adjust_and_round_quantity(
            new_quantity, position.symbol, current_price, lot_params, min_notional_usd
        )
