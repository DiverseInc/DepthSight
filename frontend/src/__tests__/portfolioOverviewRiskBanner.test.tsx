// src/__tests__/portfolioOverviewRiskBanner.test.tsx
//
// FIX 2 -- the DASHBOARD must tell the client when the bot has been blocked.
//
// The backend already returns `is_trading_allowed` and
// `trading_disabled_reason` on the portfolio status payload. The frontend
// threw them away, so a client whose bot hit the daily loss limit saw a
// completely normal-looking dashboard.
//
// This drives the real dashboard widget, so it fails against the old widget
// (which rendered balance and PnL only).

import { beforeAll, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import indexEn from "@/locales/en/index.json";
import commonEn from "@/locales/en/common.json";

const portfolioStatus = {
	balance: 10000,
	today_pnl: 0,
	is_trading_allowed: false,
	trading_disabled_reason: "DailyLoss: -120.00 USD <= -50.00 USD",
	consecutive_losses: 3,
	timestamp_utc: "2026-01-01T00:00:00Z",
};

vi.mock("@/lib/api", () => ({
	usePortfolioStatus: () => ({ data: portfolioStatus, isLoading: false, isError: false, error: null }),
	usePositions: () => ({ data: [], isLoading: false, isError: false, error: null }),
}));

vi.mock("@/context/PortfolioModeContext", () => ({
	usePortfolioMode: () => ({ mode: "paper" }),
}));

vi.mock("@/stores/accountStore", () => ({
	useAccountStore: () => ({ selectedApiKeyId: undefined, selectedMarketType: undefined }),
}));

import { PortfolioOverviewWidget } from "@/components/dashboard/PortfolioOverviewWidget";

beforeAll(async () => {
	await i18n.use(initReactI18next).init({
		resources: { en: { index: indexEn, common: commonEn } },
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

describe("the dashboard shows the risk gate (FIX 2)", () => {
	// Rendered inside the test (not at collection time) so i18n is ready.
	const renderWidget = () => renderToStaticMarkup(<PortfolioOverviewWidget />);

	it("shows the reason the backend gave for blocking trading", () => {
		expect(renderWidget()).toContain(
			"DailyLoss: -120.00 USD &lt;= -50.00 USD",
		);
	});

	it("marks the state with the same red badge the risk family uses", () => {
		expect(renderWidget()).toContain("cannot_trade");
	});

	it("still renders the balance", () => {
		expect(renderWidget()).toContain("Balance");
	});
});
