/**
 * Shared vocabulary for the PUBLISHED status of a running strategy.
 *
 * The bot publishes one of: "running", "in_position", "cannot_trade",
 * "blocked". "blocked" is the state of a strategy whose RESOLVED symbol pool
 * is empty: the strategy is fine, the thing that feeds it symbols is not
 * there right now. It must never be rendered as a green "RUNNING" badge (the
 * strategy cannot trade) and it must never be rendered as a red error (nothing
 * is broken in the customer's config).
 *
 * This module is the single place that knows what "blocked" means, so the
 * dashboard table and the strategies page cannot drift apart.
 */

/** The status the bot publishes for a strategy scoped to no symbols. */
export const STRATEGY_STATUS_BLOCKED = "blocked";

/**
 * The one badge style for a blocked strategy. Amber: blocked/waiting, which
 * is neither "it is working" (green) nor "it crashed" (red).
 */
export const BLOCKED_BADGE_CLASS =
	"bg-amber-500 hover:bg-amber-600 text-white";

/** True when the published status is the blocked one. */
export const isBlockedStrategyStatus = (status?: string | null): boolean =>
	(status ?? "").toLowerCase() === STRATEGY_STATUS_BLOCKED;

/**
 * The machine-readable cause the bot published next to the status, mapped to
 * the i18n key that explains it. The bot decides the cause; the UI only
 * translates it -- deriving the cause from the mode again here is how two
 * copies of one rule drift apart.
 */
export const BLOCKED_REASON_I18N_KEYS: Record<string, string> = {
	symbol_source_unavailable: "common:blocked.symbolSourceUnavailable",
	no_symbols_configured: "common:blocked.noSymbolsConfigured",
	unknown_symbol_mode: "common:blocked.unknownSymbolMode",
};

/**
 * The i18n key for a blocked reason, or null when the code is unknown (the
 * caller then falls back to the bot's own English `status_detail`).
 */
export const blockedReasonI18nKey = (
	reasonCode?: string | null,
): string | null => {
	if (!reasonCode) return null;
	return BLOCKED_REASON_I18N_KEYS[reasonCode] ?? null;
};
