// src/__tests__/topStrategiesDegradedBadge.test.tsx
//
// The DEGRADED state must be VISIBLE, not just present in the API.
//
// The backend now measures the configured MAIN_APP screener feed and
// publishes a strategy that depends on it as status="degraded" with the
// reason in `status_detail`. Before this, a dead feed produced a strategy
// that reported status="running" -- so the dashboard showed a GREEN badge
// for a strategy that could never trade.
//
// This drives the real dashboard table and asserts the BADGE CLASS, not
// just the text: without a "degraded" case in the badge mapping the
// strategy would fall through to the GREY default, which is neither the
// amber "impaired" badge nor the green "running" one. Asserting only on
// the absence of green would pass against that grey default too, so the
// colour itself is pinned.

import { beforeAll, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type React from "react";
import indexEn from "@/locales/en/index.json";
import commonEn from "@/locales/en/common.json";

const FEED_REASON =
	"MAIN_APP feed unreachable: wss://screener.depthsight.pro/ws/bot/ " +
	"(http 525): WebSocket upgrade rejected with HTTP status 525";

const degradedStrategy = {
	id: "cfg-dynamic",
	name: "EMA 50/200 Golden Cross",
	strategy_name: "VisualBuilderStrategy",
	symbol: "BTCUSDT",
	market_type: "futures",
	status: "degraded",
	status_detail: FEED_REASON,
	feed_health: {
		state: "degraded",
		url: "wss://screener.depthsight.pro/ws/bot/",
		reachable: false,
		failure: "http",
		status_code: 525,
		detail: "WebSocket upgrade rejected with HTTP status 525",
		reason: FEED_REASON,
		checked_at: "2026-10-29T00:00:00+00:00",
		elapsed_ms: 717,
	},
	pnl: 0,
	open_positions: 0,
	started_at: "2026-10-29T00:00:00+00:00",
	params: {},
	mode: "paper",
	symbol_selection_mode: "DYNAMIC",
};

const healthyStrategy = {
	...degradedStrategy,
	id: "cfg-static",
	name: "Pinned BTC strategy",
	status: "running",
	status_detail: null,
	feed_health: {
		...degradedStrategy.feed_health,
		state: "ok",
		reachable: true,
		failure: null,
		status_code: 101,
		detail: "WebSocket upgrade accepted",
		reason: null,
	},
};

// The union keeps `status_detail` honest: a healthy strategy has NO
// reason, so it is `null`, while a degraded one carries the reason.
let strategies: Array<typeof degradedStrategy | typeof healthyStrategy> = [
	degradedStrategy,
];

// The saved CONFIGS the strategies page lists. The page merges the running
// strategy data over these, so a config without a status still renders the
// running status.
let savedConfigs = [
	{
		id: "cfg-dynamic",
		name: "EMA 50/200 Golden Cross",
		description: "",
		config_data: { strategy_name: "VisualBuilderStrategy" },
		symbols: ["BTCUSDT"],
		mode: "paper",
		symbol_selection_mode: "DYNAMIC",
	},
];

vi.mock("@/lib/api", () => ({
	useStrategies: (params?: { mode?: string }) => ({
		data: params?.mode === "paper" ? strategies : [],
		isLoading: false,
		isError: false,
		error: null,
	}),
	useStrategyConfigsList: () => ({
		data: savedConfigs,
		isLoading: false,
		isError: false,
		error: null,
	}),
	useStopStrategy: () => ({ mutate: vi.fn(), isPending: false }),
	useStartStrategy: () => ({ mutate: vi.fn(), isPending: false }),
	useDeleteStrategyConfig: () => ({ mutate: vi.fn(), isPending: false }),
	useRunBacktest: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/context/PortfolioModeContext", () => ({
	usePortfolioMode: () => ({ mode: "paper" }),
}));

vi.mock("@/stores/accountStore", () => ({
	useAccountStore: () => ({
		selectedApiKeyId: undefined,
		selectedMarketType: undefined,
	}),
}));

vi.mock("react-router-dom", () => ({
	Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
		<a href={to}>{children}</a>
	),
	useNavigate: () => () => {},
}));

// The page shell and the three modals need app-level providers that have
// nothing to do with the badge. They are stubbed so this test renders the
// strategy card itself.
vi.mock("@/components/layout/PageLayout", () => ({
	PageLayout: ({ children }: { children: React.ReactNode }) => (
		<div>{children}</div>
	),
}));
vi.mock("@/components/strategies/BacktestModal", () => ({
	BacktestModal: () => null,
}));
vi.mock("@/components/strategies/LaunchStrategyModal", () => ({
	LaunchStrategyModal: () => null,
}));
vi.mock("@/components/strategies/StrategyDetailsPanel", () => ({
	StrategyDetailsPanel: () => null,
}));

import { TopStrategiesTable } from "@/components/dashboard/TopStrategiesTable";
import StrategiesPage from "@/pages/Strategies";

beforeAll(async () => {
	await i18n.use(initReactI18next).init({
		resources: { en: { index: indexEn, common: commonEn } },
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

/** The badge markup for the one strategy row on the table. */
const badgeOf = (html: string): string => {
	const match = html.match(/<div[^>]*title="[^"]*"[^>]*>[^<]*<\/div>/);
	return match ? match[0] : "";
};

describe("the dashboard shows a DEGRADED strategy as degraded", () => {
	it("labels the strategy DEGRADED, not RUNNING", () => {
		const html = renderToStaticMarkup(<TopStrategiesTable />);
		expect(html).toContain("DEGRADED");
		expect(html).not.toContain("RUNNING");
	});

	it("paints the badge amber, not green and not the grey default", () => {
		const badge = badgeOf(renderToStaticMarkup(<TopStrategiesTable />));
		expect(badge).not.toBe("");
		// Amber = impaired by an outage.
		expect(badge).toContain("bg-yellow-500");
		// NEVER green: green means "this strategy is working".
		expect(badge).not.toContain("bg-green-500");
		expect(badge).not.toContain("bg-green-600");
		expect(badge).not.toContain("bg-emerald-500");
		// And NOT the grey "unknown status" default, which is what a
		// degraded strategy would fall through to without a case of its own.
		expect(badge).not.toContain("bg-gray-500");
	});

	it("carries the reason -- the unreachable URL and the failure -- on the badge", () => {
		const badge = badgeOf(renderToStaticMarkup(<TopStrategiesTable />));
		// The reason reaches the user through the badge's title attribute.
		expect(badge).toContain("screener.depthsight.pro");
		expect(badge).toContain("525");
	});

	it("still shows a healthy strategy as running, in green", () => {
		strategies = [healthyStrategy];
		try {
			const html = renderToStaticMarkup(<TopStrategiesTable />);
			expect(html).toContain("RUNNING");
			expect(html).toContain("bg-green-500");
			expect(html).not.toContain("DEGRADED");
		} finally {
			strategies = [degradedStrategy];
		}
	});
});

// The SAME defect, on the OTHER page that lists strategies. It had its own
// `getStatusColor` with no "degraded" case, so a degraded strategy fell
// through to the GREY default, and it never rendered `status_detail` at
// all -- so the user saw a grey badge and no explanation.
describe("the strategies page shows a DEGRADED strategy as degraded", () => {
	it("labels the strategy DEGRADED, not RUNNING", () => {
		const html = renderToStaticMarkup(<StrategiesPage />);
		expect(html).toContain("DEGRADED");
		expect(html).not.toContain("RUNNING");
	});

	it("paints the badge amber, not green and not the grey default", () => {
		const html = renderToStaticMarkup(<StrategiesPage />);
		// Amber = impaired by an outage.
		expect(html).toContain("bg-amber-500");
		// NEVER green: green means "this strategy is working".
		expect(html).not.toContain("bg-emerald-500");
		// And NOT the grey "unknown status" default.
		expect(html).not.toContain("bg-slate-400");
	});

	it("shows the reason -- the unreachable URL and the failure", () => {
		const html = renderToStaticMarkup(<StrategiesPage />);
		expect(html).toContain("screener.depthsight.pro");
		expect(html).toContain("525");
		// The reason must be RENDERED TEXT, not only an attribute value.
		// The badge carries it in a `title` too, and `expect(html).toContain(
		// reason)` would be satisfied by that attribute alone -- which is
		// how the reason stayed invisible on this page.
		expect(html).toContain(`>${FEED_REASON}<`);
	});

	it("still shows a healthy strategy as running, in green, with no reason", () => {
		strategies = [healthyStrategy];
		savedConfigs = [
			{ ...savedConfigs[0], id: "cfg-static", name: "Pinned BTC strategy" },
		];
		try {
			const html = renderToStaticMarkup(<StrategiesPage />);
			expect(html).toContain("RUNNING");
			expect(html).toContain("bg-emerald-500");
			expect(html).not.toContain("DEGRADED");
			expect(html).not.toContain("screener.depthsight.pro");
		} finally {
			strategies = [degradedStrategy];
			savedConfigs = [
				{
					id: "cfg-dynamic",
					name: "EMA 50/200 Golden Cross",
					description: "",
					config_data: { strategy_name: "VisualBuilderStrategy" },
					symbols: ["BTCUSDT"],
					mode: "paper",
					symbol_selection_mode: "DYNAMIC",
				},
			];
		}
	});
});

// The backend added a deployment-level route for the feed's health. Without
// a client it was DEAD CODE: the route answered nobody.
describe("the feed-health route has a client", () => {
	it("is reachable from the frontend", () => {
		const source = readFileSync(
			join(__dirname, "../lib/api.ts"),
			"utf8",
		);
		expect(source).toContain("useMainAppFeedHealth");
		expect(source).toContain("/strategies/feed-health");
	});
});
