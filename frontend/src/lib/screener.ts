// src/lib/screener.ts
//
// Types and pure helpers for the PUBLIC screener page.
//
// Every number the page shows comes from a public endpoint:
//
//   * GET /api/v1/strategy-templates        -> the curated template catalogue
//   * GET /api/v1/stats/platform            -> platform counters
//   * GET /api/v1/public/published-results  -> backtest results a user
//                                                published for public comparison
//
// Nothing here is hardcoded, so the page cannot advertise a number the
// backend does not publish. The helpers in this file exist to keep the page
// HONEST: a metric the backtest never recorded has to read "Not reported",
// never 0.00, and a template with no published performance has to say so
// rather than imply a result.
//
// The published feed is NOT a winners-only list. A result that lost money is
// published and ranked exactly like one that made it, so every helper here
// has to render a NEGATIVE number correctly: the sign, the magnitude and the
// unit all have to survive.

export interface PublicStrategyTemplate {
	id: string;
	slug: string;
	name: string;
	description?: string | null;
	archetype: string;
	configData?: Record<string, unknown>;
	riskProfile?: Record<string, unknown>;
	tierRequired?: string;
	sortOrder?: number;
	isActive?: boolean;
}

export interface PublicPublishedMetrics {
	sharpeRatio: number | null;
	netPnlPercent: number | null;
	netPnl: number | null;
	winRate: number | null;
	maxDrawdown: number | null;
	trades: number | null;
}

export interface PublicPublishedResult {
	slug: string;
	strategyName: string;
	symbol: string;
	/** The window THIS run covered. */
	period: { start: string; end: string };
	category: string;
	/**
	 * The run's OWN number for the category the feed is ranked by.
	 *
	 * It is null when the run never measured that metric -- the feed still
	 * lists the result, and the page says "Not reported" rather than
	 * printing a number the backtest never produced.
	 */
	score: number | null;
	/** The position in the published list the visitor is shown. */
	rank: number;
	metrics: PublicPublishedMetrics;
	hasPublishedBacktest: boolean;
}

export interface PublicPublishedResults {
	category: string;
	/**
	 * The window the RANKING covers. It is NOT the window any individual
	 * backtest ran over -- each result carries its own `period.start` /
	 * `period.end`, which is the window that run actually covered.
	 */
	period: string;
	basis: string;
	/** How many published results the ranking covers. */
	total: number;
	results: PublicPublishedResult[];
}

export interface PublicPlatformStats {
	totalUsers: number;
	strategyTemplates: number;
	totalBacktests: number;
	communityNews?: number;
	communityPosts?: number;
}

/** What the page prints instead of a number the data does not contain. */
export const NOT_REPORTED = "Not reported";

/** What the page prints when a template has no published performance. */
export const NO_PUBLISHED_BACKTEST = "No published backtest";

export type ResultCategory = "sharpe_ratio" | "net_pnl_percent";

export const RESULT_CATEGORY_LABELS: Record<ResultCategory, string> = {
	sharpe_ratio: "Sharpe ratio",
	net_pnl_percent: "Net return",
};

export const PERIOD_LABELS: Record<string, string> = {
	weekly: "last 7 days",
	monthly: "last 30 days",
	all_time: "all time",
};

const asNumber = (value: unknown): number | null =>
	typeof value === "number" && Number.isFinite(value) ? value : null;

/** True only when the payload actually carries a number. */
export const isReported = (
	value: number | null | undefined,
): value is number => typeof value === "number" && Number.isFinite(value);

export const formatNumber = (
	value: number | null | undefined,
	digits = 2,
): string => (isReported(value) ? value.toFixed(digits) : NOT_REPORTED);

export const formatPercent = (
	value: number | null | undefined,
	digits = 1,
): string => (isReported(value) ? `${value.toFixed(digits)}%` : NOT_REPORTED);

export const formatSignedCurrency = (
	value: number | null | undefined,
): string =>
	isReported(value)
		? `${value >= 0 ? "+" : "-"}$${Math.abs(value).toFixed(2)}`
		: NOT_REPORTED;

export const formatCount = (value: number | null | undefined): string =>
	isReported(value) ? value.toLocaleString("en-US") : NOT_REPORTED;

export interface TemplateRisk {
	stopLossPercent: number | null;
	takeProfitPercent: number | null;
	riskPerTradePercent: number | null;
	maxConcurrentTrades: number | null;
}

/** The risk numbers the template publishes, read defensively. */
export const readRiskProfile = (
	template: PublicStrategyTemplate,
): TemplateRisk => {
	const profile = template.riskProfile ?? {};
	return {
		stopLossPercent: asNumber(profile.stopLossPercent),
		takeProfitPercent: asNumber(profile.takeProfitPercent),
		riskPerTradePercent: asNumber(profile.riskPerTradePercent),
		maxConcurrentTrades: asNumber(profile.maxConcurrentTrades),
	};
};

/**
 * Reward-to-risk implied by the template's PUBLISHED risk settings.
 *
 * This is a property of the template's configuration, NOT a measured
 * outcome, so the page labels it as such.
 */
export const rewardRiskRatio = (risk: TemplateRisk): number | null => {
	if (!isReported(risk.stopLossPercent) || !risk.stopLossPercent) return null;
	if (!isReported(risk.takeProfitPercent)) return null;
	return risk.takeProfitPercent / risk.stopLossPercent;
};

export const formatArchetype = (archetype: string): string =>
	archetype
		.split(/[_\s]+/)
		.filter(Boolean)
		.map((word) => word.charAt(0).toUpperCase() + word.slice(1))
		.join(" ");

export type ScreenerSort = "name" | "rewardRisk" | "riskPerTrade";

export interface ScreenerFilters {
	query: string;
	archetype: string;
	maxStopLossPercent: number | null;
	maxRiskPerTradePercent: number | null;
	minRewardRisk: number | null;
	sort: ScreenerSort;
}

export const DEFAULT_SCREENER_FILTERS: ScreenerFilters = {
	query: "",
	archetype: "",
	maxStopLossPercent: null,
	maxRiskPerTradePercent: null,
	minRewardRisk: null,
	sort: "name",
};

/**
 * Screen the template catalogue.
 *
 * Every filter is applied to a number the template itself publishes, so a
 * filtered list can never contain a template whose real settings differ
 * from what is shown.
 */
export const filterTemplates = (
	templates: PublicStrategyTemplate[],
	filters: ScreenerFilters,
): PublicStrategyTemplate[] => {
	const query = filters.query.trim().toLowerCase();

	const screened = templates.filter((template) => {
		const risk = readRiskProfile(template);

		if (query) {
			const haystack = `${template.name} ${template.description ?? ""} ${template.archetype}`.toLowerCase();
			if (!haystack.includes(query)) return false;
		}

		if (filters.archetype && template.archetype !== filters.archetype) {
			return false;
		}

		if (
			isReported(filters.maxStopLossPercent) &&
			isReported(risk.stopLossPercent) &&
			risk.stopLossPercent > filters.maxStopLossPercent
		) {
			return false;
		}

		if (
			isReported(filters.maxRiskPerTradePercent) &&
			isReported(risk.riskPerTradePercent) &&
			risk.riskPerTradePercent > filters.maxRiskPerTradePercent
		) {
			return false;
		}

		if (isReported(filters.minRewardRisk)) {
			const ratio = rewardRiskRatio(risk);
			if (!isReported(ratio) || ratio < filters.minRewardRisk) return false;
		}

		return true;
	});

	const sorted = [...screened];
	sorted.sort((a, b) => {
		if (filters.sort === "rewardRisk") {
			const ratioA = rewardRiskRatio(readRiskProfile(a)) ?? -1;
			const ratioB = rewardRiskRatio(readRiskProfile(b)) ?? -1;
			if (ratioA !== ratioB) return ratioB - ratioA;
		}
		if (filters.sort === "riskPerTrade") {
			const riskA = readRiskProfile(a).riskPerTradePercent ?? Number.MAX_SAFE_INTEGER;
			const riskB = readRiskProfile(b).riskPerTradePercent ?? Number.MAX_SAFE_INTEGER;
			if (riskA !== riskB) return riskA - riskB;
		}
		return a.name.localeCompare(b.name);
	});

	return sorted;
};

/** The archetypes actually present in the catalogue, for the filter control. */
export const collectArchetypes = (
	templates: PublicStrategyTemplate[],
): string[] =>
	Array.from(new Set(templates.map((t) => t.archetype))).sort((a, b) =>
		a.localeCompare(b),
	);
