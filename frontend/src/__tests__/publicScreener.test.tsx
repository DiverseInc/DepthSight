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
			score: 0.9,
			rank: 2,
			metrics: {
				sharpeRatio: 0.9,
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
			data: published,
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

	it("states plainly that it never shows account data", () => {
		expect(html).toContain("What this page will never show");
		// The apostrophe is HTML-escaped in the rendered markup, so the
		// assertion starts after it.
		expect(html).toContain("balance, equity, XP, positions or API keys");
		expect(html).toContain("it never touches an account");
	});
});
