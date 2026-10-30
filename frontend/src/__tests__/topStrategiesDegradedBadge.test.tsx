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

let strategies = [degradedStrategy];

vi.mock("@/lib/api", () => ({
	useStrategies: () => ({
		data: strategies,
		isLoading: false,
		isError: false,
		error: null,
	}),
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

import { TopStrategiesTable } from "@/components/dashboard/TopStrategiesTable";

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
