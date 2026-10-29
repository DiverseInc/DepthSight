// src/__tests__/publicScreener.test.tsx
//
// The PUBLIC screener must not overstate anything.
//
// It is the acquisition surface of a paid product: a prospect lands on it
// without an account. So it has to hold three lines at the same time:
//
//   1. it shows REAL numbers, taken from the public API;
//   2. it never shows a number that was not measured;
//   3. it never shows anything that belongs to an account.
//
// This test drives the real page component with fixture API payloads.
//
// The `@/lib/api` mock is a PROXY that throws for anything other than the
// three public hooks. If the page ever reaches for an authenticated hook
// (balances, positions, the user's own strategies) the render blows up here
// instead of quietly leaking on the live site.

import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { HelmetProvider } from "react-helmet-async";
import type React from "react";

const templates = [
	{
		id: "1",
		slug: "rsi-breakout-v2",
		name: "RSI Breakout v2",
		description: "Enters on an RSI break with a defined stop.",
		archetype: "rsi_breakout",
		riskProfile: {
			stopLossPercent: 2,
			takeProfitPercent: 4,
			riskPerTradePercent: 1,
			maxConcurrentTrades: 1,
		},
	},
	{
		id: "2",
		slug: "blank-canvas",
		name: "Blank Canvas",
		description: "Start from scratch.",
		archetype: "blank",
		riskProfile: {
			stopLossPercent: 2,
			takeProfitPercent: 4,
			riskPerTradePercent: 1,
			maxConcurrentTrades: 1,
		},
	},
];

const published = {
	category: "sharpe_ratio",
	period: "all_time",
	basis: "backtest",
	// The number of published results the ranking covers.
	total: 2,
	results: [
		{
			slug: "abc123def456",
			strategyName: "RSI Breakout v2",
			symbol: "BTCUSDT",
			period: {
				start: "2026-01-01T00:00:00",
				end: "2026-03-31T00:00:00",
			},
			category: "sharpe_ratio",
			score: 1.5,
			rank: 1,
			metrics: {
				sharpeRatio: 1.5,
				netPnlPercent: 12,
				netPnl: 1200,
				// This run never measured a win rate. It must read as
				// "Not reported", never as 0.0%.
				winRate: null,
				maxDrawdown: 8,
				trades: 40,
			},
			hasPublishedBacktest: true,
		},
		{
			slug: "zzz999yyy888",
			strategyName: "Private Strategy",
			symbol: "ETHUSDT",
			period: {
				start: "2025-11-01T00:00:00",
				end: "2025-12-31T00:00:00",
			},
			category: "sharpe_ratio",
			score: -0.42,
			rank: 2,
			metrics: {
				sharpeRatio: -0.42,
				netPnlPercent: -4.5,
				netPnl: -450,
				winRate: 48.2,
				maxDrawdown: 11.1,
				trades: 22,
			},
			hasPublishedBacktest: true,
		},
	],
};

// The mock reads this at RENDER time, so a test can point the page at
// a different feed (an empty one) and render it again. The reference
// lives inside the hook, so it is read long after this module body has
// finished running.
let publishedResults = published;

const stats = {
	totalUsers: 12,
	strategyTemplates: 7,
	totalBacktests: 34,
	communityNews: 3,
	communityPosts: 9,
};

vi.mock("@/lib/api", () => {
	const allowed: Record<string, unknown> = {
		usePublicStrategyTemplates: () => ({
			data: templates,
			isLoading: false,
			isError: false,
		}),
		usePublicPublishedResults: () => ({
			data: publishedResults,
			isLoading: false,
			isError: false,
		}),
		usePublicPlatformStats: () => ({
			data: stats,
			isLoading: false,
			isError: false,
		}),
	};

	return new Proxy(
		{},
		{
			// Vitest checks `prop in mock` before handing an export out, so the
			// proxy needs a `has` trap as well as a `get` trap.
			has(_target, prop) {
				if (typeof prop !== "string") return false;
				return prop === "__esModule" || prop === "then" || prop in allowed;
			},
			get(_target, prop) {
				if (typeof prop !== "string") return undefined;
				if (prop === "__esModule") return true;
				if (prop === "then") return undefined;
				if (!(prop in allowed)) {
					throw new Error(
						`The public screener must only read public endpoints; it touched "${prop}".`,
					);
				}
				return allowed[prop];
			},
		},
	);
});

vi.mock("react-router-dom", () => ({
	Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
		<a href={to}>{children}</a>
	),
}));

import PublicScreener from "@/pages/PublicScreener";

const renderPage = () =>
	renderToStaticMarkup(
		<HelmetProvider>
			<PublicScreener />
		</HelmetProvider>,
	);

describe("the public screener page", () => {
	const html = renderPage();

	it("shows the real template names from the public catalogue", () => {
		expect(html).toContain("RSI Breakout v2");
		expect(html).toContain("Blank Canvas");
		expect(html).toContain("Rsi Breakout");
	});

	it("shows the risk settings the template itself publishes", () => {
		// stop loss 2%, take profit 4%, risk per trade 1% -> reward:risk 2.00:1
		expect(html).toContain("2.00%");
		expect(html).toContain("4.00%");
		expect(html).toContain("Reward-to-risk 2.00:1");
	});

	it("says so plainly when a template has no published performance", () => {
		expect(html).toContain("No published backtest");
	});

	it("shows the real numbers of a published result", () => {
		expect(html).toContain("BTCUSDT");
		expect(html).toContain("1.50"); // Sharpe ratio
		expect(html).toContain("12.0%"); // net return
		expect(html).toContain("+$1200.00"); // net P&L
		expect(html).toContain("8.0%"); // max drawdown
		expect(html).toContain("40"); // trades
	});

	it("says what the numbers are and over which period they were measured", () => {
		expect(html).toContain("Ranked by Sharpe ratio");
		expect(html).toContain("backtests, not live trading");
		expect(html).toContain("01 Jan 2026");
		expect(html).toContain("31 Mar 2026");
	});

	// --- The two honesty nits dsh raised on PR #41 -----------------------

	it("labels the ranking window as the ranking, not as the run's own window", () => {
		// The leaderboard window ("all time") describes the window the RANKING
		// covers. It must never be printed as if it described one backtest.
		expect(html).toContain("Ranking window: all time");
		expect(html).not.toContain("31 Mar 2026 (all time)");
		expect(html).not.toContain("31 Dec 2025 (all time)");
	});

	it("labels each card with the window that run actually covered", () => {
		expect(html).toContain("Backtest window: 01 Jan 2026 - 31 Mar 2026");
		expect(html).toContain("Backtest window: 01 Nov 2025 - 31 Dec 2025");
	});

	it("shows a position that agrees with the list the visitor is looking at", () => {
		expect(html).toContain("Position 1 of 2 among published results");
		expect(html).toContain("Position 2 of 2 among published results");
	});

	it("never renders a metric that was not measured as a zero", () => {
		// win_rate is null in the fixture. A page that faked it would print
		// "0.0%" next to the win-rate label.
		expect(html).toContain("Not reported");
		expect(html).not.toContain("0.0%");
	});

	it("shows the platform counters without the demo accounts", () => {
		expect(html).toContain("12"); // totalUsers
		expect(html).toContain("7"); // strategyTemplates
		expect(html).toContain("34"); // totalBacktests
		expect(html).toContain("exclude the seeded demo accounts");
	});

	it("makes no trial or countdown claim", () => {
		expect(html).not.toMatch(/days? left/i);
		expect(html).not.toMatch(/countdown/i);
		expect(html).not.toMatch(/trial/i);
		expect(html).not.toMatch(/expires/i);
		expect(html).not.toMatch(/hurry/i);
	});

	it("renders a result that lost money with its sign intact", () => {
		// The second fixture result LOST money: a negative Sharpe ratio, a
		// negative net return and a negative net P&L. Every one of them has
		// to keep its minus sign -- a page that printed the bare magnitude
		// would be showing a result the backtest never produced.
		expect(html).toContain("-0.42"); // Sharpe ratio
		expect(html).toContain("-4.5%"); // net return
		expect(html).toContain("-$450.00"); // net P&L
		// And the magnitude alone must never appear on its own.
		expect(html).not.toMatch(/>0\.42</);
		expect(html).not.toMatch(/>4\.5%</);
		expect(html).not.toMatch(/>\$450\.00</);
	});

	it("does not describe the feed as a list of winners", () => {
		// The feed is ranked by the metric the visitor picks, inside one
		// category. It does NOT say a result beat anything, and it does
		// NOT call any result the best one.
		expect(html).toContain("A result that lost money is");
		expect(html).not.toMatch(/\bbest\b/i);
		expect(html).not.toMatch(/\bbeat\b/i);
		expect(html).not.toMatch(/\boutperform/i);
		// 'the top N' is a page of a longer ranking, not the best N.
		expect(html).not.toContain("Showing the top");
	});

	it("tells the truth when the feed is empty", () => {
		publishedResults = { ...published, total: 0, results: [] };
		const emptyHtml = renderPage();
		publishedResults = published;

		expect(emptyHtml).toContain(
			"No results have been published for public comparison yet",
		);
		// The old copy promised to withhold any number we could not stand
		// behind. That is no longer true: a losing result IS published, so
		// an empty feed means nothing is published, not that we are hiding
		// something we cannot stand behind.
		expect(emptyHtml).not.toContain("we cannot stand behind");
	});

	it("states plainly that it never shows account data", () => {
		expect(html).toContain("What this page will never show");
		// The apostrophe is HTML-escaped in the rendered markup, so the
		// assertion starts after it.
		expect(html).toContain("balance, equity, XP, positions or API keys");
		expect(html).toContain("it never touches an account");
	});
});
