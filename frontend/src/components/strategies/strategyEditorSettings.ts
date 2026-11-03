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
//
// THE SERVER IS THE AUTHORITY
// ---------------------------
// This file used to carry a hand-written mirror of the trading bot's
// registry and a hand-written copy of the server's risk floors. Both could
// only ever be right by luck: a class the bot added later was not offered
// until the mirror was updated, and a class the bot removed stayed offered.
//
// The server now publishes its own answer on
// `GET /api/v1/strategies/registry` (`describe_client_contract()` in
// `bot_module/strategy_registry.py`, generated from the very constants the
// validator uses). `applyStrategyRegistry()` applies that answer, so the
// list and the floors on screen are the server's own.
//
// There is NO hand-written copy of either left in this file. A copy could
// only ever be right by luck, and a copy that was LOOSER than the server's
// own would have the editor offering a limit the server refuses.

import type {
	RiskManagementSettings,
	StrategyRegistryContract,
	StrategyRiskSettings,
} from "@/types/api";

export type StrategyRiskField = keyof StrategyRiskSettings;

/**
 * The server's risk floors, per field.
 *
 * The server owns them. `GET /api/v1/strategies/registry` publishes the floor
 * for every risk setting, generated from the very constants the validator
 * uses (`bot_module/strategy_registry.py`'s `SERVER_RISK_FLOOR`, which reads
 * them from `bot_module/config.py`):
 *
 *   DEFAULT_RISK_PER_TRADE_PERCENT = 1.0
 *   DEFAULT_MAX_DRAWDOWN_PERCENT   = 20.0
 *   DEFAULT_DAILY_MAX_LOSS_PERCENT = 5.0
 *   DEFAULT_MAX_CONSECUTIVE_LOSSES = 10
 *
 * There is NO hand-written copy of them here any more. `null` means the
 * server has not answered yet: the floor for that field is UNKNOWN, so
 * nothing can be checked against it and nothing may be sent -- the editor
 * would otherwise be offering a limit the server refuses.
 *
 * The server refuses a client value that is LOOSER than the floor for that
 * field. The client may only TIGHTEN a limit, so the UI mirrors the same
 * ceilings and refuses to offer anything looser.
 */
export const SERVER_RISK_FLOOR: Record<StrategyRiskField, number | null> = {
	risk_per_trade_pct: null,
	max_drawdown_pct: null,
	daily_loss_limit_pct: null,
	position_size_pct: null,
	max_stop_distance_pct: null,
	max_consecutive_losses: null,
};

/**
 * The server's ceiling for every risk number the client may send.
 *
 * `max_consecutive_losses` is a COUNT of trades, not a percentage; it lives
 * in the same table because the editor renders every field from it.
 */
export const SERVER_RISK_FLOOR_PCT = SERVER_RISK_FLOOR;

/**
 * The risk limits the editor starts from when neither the saved strategy nor
 * the account carries a value.
 *
 * They are the ACCOUNT's own defaults (`api/crud.py`: riskPerTradePercent
 * 1.0, maxDrawdown 10.0, dailyMaxLossPercent 5.0), pulled DOWN to the
 * server's floor for every field the floor is known for. An untouched form
 * therefore never sends anything the server would refuse -- and when the
 * server's floor is tighter than the account's default, the floor wins.
 */
export const defaultStrategyRiskSettings = (): StrategyRiskSettings => {
	const settings = {
		risk_per_trade_pct: 1,
		max_drawdown_pct: 10,
		daily_loss_limit_pct: 5,
		position_size_pct: 1,
	};
	for (const field of Object.keys(settings) as (keyof typeof settings)[]) {
		const floor = SERVER_RISK_FLOOR[field];
		if (typeof floor === "number") {
			settings[field] = Math.min(settings[field], floor);
		}
	}
	// The per-strategy position budget may not exceed the risk per trade.
	settings.position_size_pct = Math.min(
		settings.position_size_pct,
		settings.risk_per_trade_pct,
	);
	return settings;
};

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
	"max_stop_distance_pct",
	"max_consecutive_losses",
];

/**
 * The fields that may be left out.
 *
 * "Omitting one means leave the account's current value alone" is the
 * server's rule (`bot_module/strategy_registry.py`), so an omitted field is
 * not an error here either.
 */
export const OPTIONAL_RISK_FIELDS: StrategyRiskField[] = [
	"max_stop_distance_pct",
	"max_consecutive_losses",
];

/**
 * True once the server's floor is known for EVERY risk field.
 *
 * Until then the client cannot tell a limit the server accepts from one it
 * refuses, so nothing may be sent: see `hasServerRiskFloors`.
 */
export const hasServerRiskFloors = (): boolean =>
	RISK_FIELDS.every((field) => typeof SERVER_RISK_FLOOR[field] === "number");

/**
 * The one rule per field the server REFUSES a value over.
 *
 * `_validate_risk_settings` in `bot_module/strategy_registry.py` refuses a
 * value that is not a number, that is not greater than zero, and that is
 * looser than the server's floor for that field. A floor the server has not
 * published yet cannot be exceeded, so nothing is reported for it.
 */
const validateRiskField = (
	raw: unknown,
	floor: number | null,
): StrategyRiskErrorCode | undefined => {
	if (typeof raw !== "number" || !Number.isFinite(raw)) return "notANumber";
	if (raw <= 0) return "notPositive";
	if (typeof floor === "number" && raw > floor) return "aboveFloor";
	return undefined;
};

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
		for (const field of RISK_FIELDS) {
			if (!OPTIONAL_RISK_FIELDS.includes(field)) errors[field] = "notANumber";
		}
		return errors;
	}

	for (const field of RISK_FIELDS) {
		const raw = value[field];
		if (raw === undefined || raw === null) {
			// An omitted limit means "leave the account's value alone".
			if (OPTIONAL_RISK_FIELDS.includes(field)) continue;
			errors[field] = "notANumber";
			continue;
		}
		const error = validateRiskField(raw, SERVER_RISK_FLOOR[field]);
		if (error) errors[field] = error;
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

/**
 * Validates the risk limits the client is about to SEND BACK to the server.
 *
 * The server treats every risk setting as optional ("omitting one means
 * leave the account's current value alone"), so only the settings that are
 * actually present are checked -- and a present one must be one the server
 * accepts: a number greater than zero and no looser than the floor.
 *
 * The position budget is not checked here: the server CLAMPS it to the
 * account's risk per trade (`apply_client_risk_config`) rather than refusing
 * it, so a value above it is not something the server will turn down.
 */
export const validateRiskSettingsForSend = (
	value: StrategyRiskSettings | null | undefined,
): StrategyRiskErrors => {
	const errors: StrategyRiskErrors = {};
	if (!value) return errors;

	for (const field of RISK_FIELDS) {
		const raw = value[field];
		if (raw === undefined || raw === null) continue;
		const error = validateRiskField(raw, SERVER_RISK_FLOOR[field]);
		if (error) errors[field] = error;
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
		max_stop_distance_pct: read("max_stop_distance_pct"),
		max_consecutive_losses: read("max_consecutive_losses"),
	};
};

/**
 * The numbers behind the draft. A field the client has emptied or typed
 * nonsense into becomes NaN, which the validation reports as "not a number".
 * An optional field the client left empty is simply absent: the server then
 * leaves the account's own value alone.
 */
export const parseRiskDraft = (
	draft: StrategyRiskDraft,
): StrategyRiskSettings => {
	const read = (field: StrategyRiskField): number => {
		const raw = draft?.[field];
		if (typeof raw !== "string" || raw.trim() === "") return Number.NaN;
		return Number(raw);
	};
	const parsed: StrategyRiskSettings = {
		risk_per_trade_pct: read("risk_per_trade_pct"),
		max_drawdown_pct: read("max_drawdown_pct"),
		daily_loss_limit_pct: read("daily_loss_limit_pct"),
		position_size_pct: read("position_size_pct"),
	};
	for (const field of OPTIONAL_RISK_FIELDS) {
		const value = read(field);
		if (Number.isFinite(value)) parsed[field] = value;
	}
	return parsed;
};

/**
 * Where each risk limit lives in the ACCOUNT's `risk_management` block.
 *
 * These are the keys the bot actually reads: `bot_module/risk_manager.py:179`
 * reads `user_settings["risk_management"]`, and the risk manager looks the
 * individual limits up under these names. Seeding the form from them is what
 * makes the editor show the limits that are in force, instead of a
 * hardcoded guess.
 */
export const RISK_FIELD_TO_ACCOUNT_KEY: Partial<
	Record<StrategyRiskField, string>
> = {
	risk_per_trade_pct: "riskPerTradePercent",
	max_drawdown_pct: "maxDrawdown",
	daily_loss_limit_pct: "dailyMaxLossPercent",
	max_stop_distance_pct: "maxStopDistancePct",
	max_consecutive_losses: "maxConsecutiveLosses",
};

/**
 * The risk limits the ACCOUNT is currently running with.
 *
 * Returns null when the account carries no usable value, so the caller can
 * fall back to the defaults.
 *
 * A value looser than the server floor is returned AT the floor: a client
 * may tighten a limit, never loosen it, and the editor must not offer a
 * limit the server would refuse.
 */
export const toRiskSettingsFromAccount = (
	riskManagement:
		| RiskManagementSettings
		| Record<string, unknown>
		| null
		| undefined,
): StrategyRiskSettings | null => {
	if (!riskManagement || typeof riskManagement !== "object") return null;
	const source = riskManagement as Record<string, unknown>;
	const settings: StrategyRiskSettings = defaultStrategyRiskSettings();
	let found = false;
	for (const field of Object.keys(
		RISK_FIELD_TO_ACCOUNT_KEY,
	) as StrategyRiskField[]) {
		const key = RISK_FIELD_TO_ACCOUNT_KEY[field];
		if (!key) continue;
		const raw = source[key];
		if (typeof raw !== "number" || !Number.isFinite(raw) || raw <= 0) continue;
		const floor = SERVER_RISK_FLOOR[field];
		settings[field] = typeof floor === "number" ? Math.min(raw, floor) : raw;
		found = true;
	}
	if (!found) return null;
	// The per-strategy position budget may not exceed the account's risk per
	// trade -- the server refuses it, so the form must not offer it either.
	settings.position_size_pct = Math.min(
		settings.position_size_pct,
		settings.risk_per_trade_pct,
	);
	return settings;
};

/**
 * Pulls every limit that is looser than the server's floor DOWN to the floor.
 *
 * The server refuses a limit that is looser than its floor, so a form that
 * still shows one is offering something the server will not take. This runs
 * when the server's contract arrives, so the numbers on screen are never
 * looser than the server's own ceiling.
 *
 * The draft is returned unchanged when nothing had to be pulled down, so the
 * editor does not rewrite what the client typed.
 */
export const clampRiskDraftToFloors = (
	draft: StrategyRiskDraft,
): StrategyRiskDraft => {
	let changed = false;
	const clamped: StrategyRiskDraft = { ...draft };
	for (const field of RISK_FIELDS) {
		const floor = SERVER_RISK_FLOOR[field];
		if (typeof floor !== "number") continue;
		const raw = draft?.[field];
		if (typeof raw !== "string" || raw.trim() === "") continue;
		const value = Number(raw);
		if (!Number.isFinite(value)) continue;
		if (value > floor) {
			clamped[field] = String(floor);
			changed = true;
		}
	}
	return changed ? clamped : draft;
};

/**
 * The strategy classes the server can actually run.
 *
 * There is NO hand-written list here any more. The server publishes its own
 * registry on `GET /api/v1/strategies/registry` and the answer is applied to
 * this array by `applyStrategyRegistry()`.
 *
 * A class that is not in this list cannot be instantiated by the bot. The
 * server refuses such a pick outright (`422`), so the client must never
 * offer one.
 */
export const BOT_REGISTERED_STRATEGY_CLASSES: string[] = [];

let registryLoaded = false;

/** True once the server's contract has been applied. */
export const isStrategyRegistryLoaded = (): boolean => registryLoaded;

/**
 * The server's floor for the per-trade risk, in the unit the strategy
 * parameter uses: a FRACTION of the balance (0.01 is 1%).
 *
 * `null` means the server has not published it (or does not publish it at
 * all). The `risk_settings` block is in PERCENT, so the two must never be
 * mixed: 1.0 in `risk_settings` is 1%, and 0.01 in the parameter is 1%.
 */
let perTradeRiskFloorFraction: number | null = null;

/** The name of the strategy parameter that carries the per-trade risk. */
let perTradeRiskParameter = "risk_pct_per_trade";

/** The server's per-trade-risk floor, as a fraction of the balance. */
export const serverPerTradeRiskFloorFraction = (): number | null =>
	perTradeRiskFloorFraction;

/** The name of the strategy parameter that carries the per-trade risk. */
export const serverPerTradeRiskParameter = (): string => perTradeRiskParameter;

/**
 * The per-trade risks a `config_data` carries, with the path each sits at.
 *
 * The per-trade risk is not only in `risk_settings`: a strategy may read it
 * from `config_data.risk_pct_per_trade`, from
 * `config_data.config.risk_pct_per_trade` or from
 * `config_data.strategy_params.risk_pct_per_trade`
 * (`BaseStrategy._get_param` in `bot_module/strategy.py` reads the first two,
 * the "Reverse*" strategies read the third). The server bounds all three and
 * REFUSES a value looser than its floor, so the client has to look in all
 * three before it sends anything.
 */
export const collectPerTradeRisks = (
	configData: unknown,
): { path: string; value: unknown }[] => {
	const found: { path: string; value: unknown }[] = [];
	if (!configData || typeof configData !== "object") return found;
	const source = configData as Record<string, unknown>;
	const parameter = perTradeRiskParameter;

	const top = source[parameter];
	if (top !== undefined && top !== null) {
		found.push({ path: `config_data.${parameter}`, value: top });
	}

	const nested = source.config;
	if (nested && typeof nested === "object") {
		const nestedValue = (nested as Record<string, unknown>)[parameter];
		if (nestedValue !== undefined && nestedValue !== null) {
			found.push({
				path: `config_data.config.${parameter}`,
				value: nestedValue,
			});
		}
	}

	const params = source.strategy_params;
	if (params && typeof params === "object") {
		const paramValue = (params as Record<string, unknown>)[parameter];
		if (paramValue !== undefined && paramValue !== null) {
			found.push({
				path: `config_data.strategy_params.${parameter}`,
				value: paramValue,
			});
		}
	}

	return found;
};

/**
 * True when the client is about to send a per-trade risk the server refuses.
 *
 * The server refuses a value that is not a number, that is negative, and
 * that is looser than its floor (a FRACTION of the balance). A value the
 * client cannot judge -- because the server has not published its floor --
 * is left to the server, but a value that is no number at all is refused
 * either way.
 */
export const hasRefusedPerTradeRisk = (configData: unknown): boolean => {
	const floor = perTradeRiskFloorFraction;
	for (const { value } of collectPerTradeRisks(configData)) {
		if (typeof value !== "number" || !Number.isFinite(value)) return true;
		if (value < 0) return true;
		if (typeof floor === "number" && value > floor) return true;
	}
	return false;
};

/**
 * Apply the server's contract: the strategy names it can run, and its risk
 * floors.
 *
 * The array is emptied and refilled IN PLACE, so every component that holds
 * a reference to it sees the server's list.
 *
 * A contract that does not carry a floor for every risk field leaves that
 * field unknown (`hasServerRiskFloors()` is then false), and nothing may be
 * sent while a floor is unknown.
 */
export const applyStrategyRegistry = (
	contract: StrategyRegistryContract | null | undefined,
): void => {
	if (!contract) return;
	const available = Array.isArray(contract.strategy_pick?.available)
		? contract.strategy_pick.available.filter(
				(name): name is string =>
					typeof name === "string" && name.trim().length > 0,
			)
		: [];
	BOT_REGISTERED_STRATEGY_CLASSES.length = 0;
	BOT_REGISTERED_STRATEGY_CLASSES.push(...available);

	const floor = contract.server_floor;
	if (floor && typeof floor === "object") {
		for (const field of RISK_FIELDS) {
			const value = floor[field];
			if (typeof value === "number" && Number.isFinite(value) && value > 0) {
				SERVER_RISK_FLOOR[field] = value;
			}
		}
	}

	// The per-trade risk has its own floor, in its own unit (a FRACTION of
	// the balance, where the `risk_settings` block is in PERCENT). The server
	// refuses a value looser than it, in every place it may hide.
	const perTradeRisk = contract.per_trade_risk;
	if (perTradeRisk && typeof perTradeRisk === "object") {
		if (typeof perTradeRisk.parameter === "string" && perTradeRisk.parameter) {
			perTradeRiskParameter = perTradeRisk.parameter;
		}
		const fraction = perTradeRisk.server_floor;
		if (typeof fraction === "number" && Number.isFinite(fraction)) {
			perTradeRiskFloorFraction = fraction;
		}
	}

	registryLoaded = true;
};

export type BotRegisteredStrategyClass = string;

/**
 * The class the visual builder always produces. The bot forces this class for
 * any visual-shaped config, and the blocks the user drew are only executed by
 * it.
 */
export const VISUAL_BUILDER_STRATEGY_CLASS = "VisualBuilderStrategy";

export const isStrategyClassRegistered = (
	className: string | null | undefined,
): boolean =>
	typeof className === "string" && BOT_REGISTERED_STRATEGY_CLASSES.includes(className);

export type StrategyClassErrorCode = "empty" | "notRegistered";

export type StrategyClassErrors = {
	className?: StrategyClassErrorCode;
};

/**
 * Validates the strategy class the client picked.
 *
 * `notRegistered` is a BLOCK, not a warning: the list comes from the server
 * itself, so a class that is not in it is a class this server cannot run --
 * and the server refuses the save with `422` anyway. The only case where the
 * client cannot judge is before the server's contract has arrived; then it
 * says nothing rather than guess.
 */
export const validateStrategyClass = (
	className: string | null | undefined,
): StrategyClassErrors => {
	if (!className || !className.trim()) return { className: "empty" };
	if (!registryLoaded) return {};
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
