// src/components/strategies/strategyEditorSettings.ts
//
// Pure (non-React) helpers for the two settings the client types in the
// strategy editor and the server has to honour:
//
//   1. The RISK LIMITS  -> `config_data.risk_settings`
//   2. The STRATEGY PICK -> `config_data.strategy_name`
//
// This file deliberately contains no React imports so it can be unit tested
// without a DOM.

import type { StrategyRiskSettings } from "@/types/api";

/**
 * The server's risk floors, in PERCENT.
 *
 * These mirror `bot_module/strategy_registry.py`'s `SERVER_RISK_FLOOR`, which
 * reads them from `bot_module/config.py`:
 *
 *   DEFAULT_RISK_PER_TRADE_PERCENT = 1.0
 *   DEFAULT_MAX_DRAWDOWN_PERCENT   = 20.0
 *   DEFAULT_DAILY_MAX_LOSS_PERCENT = 5.0
 *
 * The server refuses a client value that is LOOSER than the floor for that
 * field. The client may only TIGHTEN a limit, so the UI mirrors the same
 * ceilings and refuses to offer anything looser.
 */
export const SERVER_DAILY_MAX_LOSS_FLOOR_PCT = 5;
export const SERVER_RISK_PER_TRADE_FLOOR_PCT = 1;
export const SERVER_MAX_DRAWDOWN_FLOOR_PCT = 20;

/** The server's ceiling for every risk number the client may send. */
export const SERVER_RISK_FLOOR_PCT: Record<
	"risk_per_trade_pct" | "max_drawdown_pct" | "daily_loss_limit_pct" | "position_size_pct",
	number
> = {
	risk_per_trade_pct: SERVER_RISK_PER_TRADE_FLOOR_PCT,
	max_drawdown_pct: SERVER_MAX_DRAWDOWN_FLOOR_PCT,
	daily_loss_limit_pct: SERVER_DAILY_MAX_LOSS_FLOOR_PCT,
	// The per-strategy position budget is clamped to the account's risk per
	// trade, which is itself capped by the risk-per-trade floor.
	position_size_pct: SERVER_RISK_PER_TRADE_FLOOR_PCT,
};

/**
 * Defaults shown in the editor. They are the ACCOUNT's own defaults
 * (`api/crud.py`: riskPerTradePercent 1.0, maxDrawdown 10.0,
 * dailyMaxLossPercent 5.0), so an untouched form never sends anything looser
 * than the account already has.
 */
export const DEFAULT_STRATEGY_RISK_SETTINGS: StrategyRiskSettings = {
	risk_per_trade_pct: 1,
	max_drawdown_pct: 10,
	daily_loss_limit_pct: SERVER_DAILY_MAX_LOSS_FLOOR_PCT,
	position_size_pct: 1,
};

export type StrategyRiskField = keyof StrategyRiskSettings;

export type StrategyRiskErrorCode =
	| "notANumber"
	| "notPositive"
	| "aboveFloor"
	| "positionExceedsRisk";

export type StrategyRiskErrors = Partial<
	Record<StrategyRiskField, StrategyRiskErrorCode>
>;

const RISK_FIELDS: StrategyRiskField[] = [
	"risk_per_trade_pct",
	"max_drawdown_pct",
	"daily_loss_limit_pct",
	"position_size_pct",
];

/**
 * Validates the risk limits the client typed.
 *
 * The rules mirror the server's `_validate_risk_settings` so the UI can never
 * offer a limit the server would reject:
 *   - every field must be a finite number;
 *   - every field must be greater than zero;
 *   - no field may exceed the server's floor for that field (the client may
 *     TIGHTEN a limit, never loosen it);
 *   - the per-strategy position budget may not exceed the risk per trade,
 *     because the account's effective risk per trade is never higher than
 *     that.
 */
export const validateStrategyRiskSettings = (
	value: StrategyRiskSettings | null | undefined,
): StrategyRiskErrors => {
	const errors: StrategyRiskErrors = {};
	if (!value) {
		for (const field of RISK_FIELDS) errors[field] = "notANumber";
		return errors;
	}

	for (const field of RISK_FIELDS) {
		const raw = value[field];
		if (typeof raw !== "number" || !Number.isFinite(raw)) {
			errors[field] = "notANumber";
			continue;
		}
		if (raw <= 0) {
			errors[field] = "notPositive";
			continue;
		}
		if (raw > SERVER_RISK_FLOOR_PCT[field]) {
			errors[field] = "aboveFloor";
		}
	}

	if (
		!errors.position_size_pct &&
		!errors.risk_per_trade_pct &&
		value.position_size_pct > value.risk_per_trade_pct
	) {
		errors.position_size_pct = "positionExceedsRisk";
	}

	return errors;
};

export const hasStrategyRiskErrors = (errors: StrategyRiskErrors): boolean =>
	Object.keys(errors).length > 0;

/**
 * The text the client has typed for each risk field. The strings are the
 * source of truth while the client is typing, so an empty or half-typed field
 * is not silently rewritten; the numbers are derived from them.
 */
export type StrategyRiskDraft = Record<StrategyRiskField, string>;

export const toRiskDraft = (
	value: StrategyRiskSettings | null | undefined,
): StrategyRiskDraft => {
	const read = (field: StrategyRiskField): string => {
		const raw = value?.[field];
		return typeof raw === "number" && Number.isFinite(raw) ? String(raw) : "";
	};
	return {
		risk_per_trade_pct: read("risk_per_trade_pct"),
		max_drawdown_pct: read("max_drawdown_pct"),
		daily_loss_limit_pct: read("daily_loss_limit_pct"),
		position_size_pct: read("position_size_pct"),
	};
};

/**
 * The numbers behind the draft. A field the client has emptied or typed
 * nonsense into becomes NaN, which the validation reports as "not a number".
 */
export const parseRiskDraft = (
	draft: StrategyRiskDraft,
): StrategyRiskSettings => {
	const read = (field: StrategyRiskField): number => {
		const raw = draft?.[field];
		if (typeof raw !== "string" || raw.trim() === "") return Number.NaN;
		return Number(raw);
	};
	return {
		risk_per_trade_pct: read("risk_per_trade_pct"),
		max_drawdown_pct: read("max_drawdown_pct"),
		daily_loss_limit_pct: read("daily_loss_limit_pct"),
		position_size_pct: read("position_size_pct"),
	};
};

/**
 * The strategy classes the trading bot actually registers.
 *
 * There is NO endpoint that exposes this registry to the frontend, so this
 * list is a hand-maintained mirror of `bot_module/strategy.py`'s `STRATEGIES`
 * dictionary, verified by importing the module:
 *
 *   REGISTERED: ['AggTradeReversal', 'ConsolidationImpulse', 'DensityBounce',
 *                'FakeBreakout', 'FirstPullbacksInTrend', 'GeneticStrategy',
 *                'ReverseFakeBreakout', 'ReverseVolumeBreakout',
 *                'VisualBuilderStrategy', 'VolumeBreakout']
 *
 * Classes that are NOT in this list cannot be instantiated by the bot. The
 * bot verifies every candidate before it registers and rejects the failures
 * with a log line naming the reason ("Strategy 'X' REJECTED from the
 * registry ... A client that picks it would get a strategy that cannot
 * start."). Picking an unregistered class makes the start request fail
 * silently, so the client must never offer one.
 *
 * This mirror can drift from the bot. A class the bot adds later is not
 * offered until this list is updated, and a class the bot removes stays
 * offered until it is removed here - so the SERVER has to be the authority.
 * TODO(GATE-4): replace this constant with a GET endpoint that returns the
 * live registry. See the "NEEDS OUTSIDE MY FILES" section of the Gate 4
 * frontend report.
 */
export const BOT_REGISTERED_STRATEGY_CLASSES = [
	"VisualBuilderStrategy",
	"GeneticStrategy",
	"VolumeBreakout",
	"FakeBreakout",
	"ReverseVolumeBreakout",
	"ReverseFakeBreakout",
	"DensityBounce",
	"ConsolidationImpulse",
	"AggTradeReversal",
	"FirstPullbacksInTrend",
] as const;

export type BotRegisteredStrategyClass =
	(typeof BOT_REGISTERED_STRATEGY_CLASSES)[number];

/**
 * The class the visual builder always produces. The bot forces this class for
 * any visual-shaped config, and the blocks the user drew are only executed by
 * it.
 */
export const VISUAL_BUILDER_STRATEGY_CLASS = "VisualBuilderStrategy";

export const isStrategyClassRegistered = (
	className: string | null | undefined,
): boolean =>
	typeof className === "string" &&
	(BOT_REGISTERED_STRATEGY_CLASSES as readonly string[]).includes(className);

export type StrategyClassErrorCode = "empty" | "notRegistered";

export type StrategyClassErrors = {
	className?: StrategyClassErrorCode;
};

/**
 * Validates the strategy class the client picked.
 *
 * `notRegistered` is a WARNING, not a block: the class can only be unknown
 * here because it came back from the server (the picker itself only offers
 * registered classes), and this client-side mirror of the bot's registry can
 * be out of date. The server is the authority on what it can instantiate.
 */
export const validateStrategyClass = (
	className: string | null | undefined,
): StrategyClassErrors => {
	if (!className || !className.trim()) return { className: "empty" };
	if (!isStrategyClassRegistered(className)) return { className: "notRegistered" };
	return {};
};

/**
 * True when the picked class is not the visual builder class while the
 * strategy carries visual blocks. The blocks are then not executed by the
 * picked class, and the backtester still runs VisualBuilderStrategy - a
 * live/backtest divergence the user has to be told about.
 */
export const divergesFromVisualBuilder = (
	className: string | null | undefined,
): boolean =>
	typeof className === "string" &&
	className.length > 0 &&
	className !== VISUAL_BUILDER_STRATEGY_CLASS;
