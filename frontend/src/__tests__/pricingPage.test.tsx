// src/__tests__/pricingPage.test.tsx
//
// FIX 1 -- the RENDERED marketing pricing page must not advertise anything
// the backend does not sell.
//
// This test drives the real `Pricing` component with the backend's plan
// catalog, so it fails against the old hand-written pricing table (which
// advertised "Unlimited backtests", "30 live strategies", PRO blocks,
// genetic search and a 14-day trial on the $30 tier).

import { beforeAll, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import type React from "react";
import type { PublicPlanCatalog } from "@/lib/pricing";

const catalog: PublicPlanCatalog = {
	plans: [
		{
			key: "free",
			name: "Free",
			description: "Basic capabilities and standard strategy blocks.",
			price_usd: 0,
			active: true,
			billing_mode: "monthly",
			billing: {},
			entitlements: [
				{ code: "run_vector_backtest_per_day", label: "20 backtests per day", included: true },
				{ code: "use_ai_assistant_per_day", label: "10 AI Assistant queries per day", included: true },
				{ code: "live_trading", label: "Live trading", included: false },
				{ code: "history", label: "90-day backtest history", included: true },
				{ code: "pro_blocks", label: "PRO blocks (Tape, Book, Open Interest)", included: false },
			],
		},
		{
			key: "standard",
			name: "Standard",
			description: "For active traders. Live trading and standard blocks.",
			price_usd: 30,
			active: true,
			billing_mode: "monthly",
			billing: {
				monthly: { price_usd: 30, period_days: 30 },
				lifetime: { enabled: true, price_usd: 99, slot_limit: 75 },
			},
			entitlements: [
				{ code: "run_vector_backtest_per_day", label: "50 backtests per day", included: true },
				{ code: "use_ai_assistant_per_day", label: "20 AI Assistant queries per day", included: true },
				{ code: "live_trading", label: "Live trading on OKX, Binance, Bybit", included: true },
				{ code: "live_strategies", label: "10 live trading strategies", included: true },
				{ code: "history", label: "1-year backtest history", included: true },
				{ code: "pro_blocks", label: "PRO blocks (Tape, Book, Open Interest)", included: false },
				{ code: "permission_run_genetic_search", label: "Genetic strategy search", included: false },
			],
		},
		{
			key: "pro",
			name: "Professional",
			description: "Maximum power. Access to all PRO blocks and genetic search.",
			price_usd: 100,
			active: true,
			billing_mode: "monthly",
			billing: {
				monthly: { price_usd: 100, period_days: 30 },
				lifetime: { enabled: true, price_usd: 299, slot_limit: 25 },
			},
			entitlements: [
				{ code: "run_vector_backtest_per_day", label: "Unlimited backtests", included: true },
				{ code: "live_trading", label: "Live trading on OKX, Binance, Bybit", included: true },
				{ code: "live_strategies", label: "30 live trading strategies", included: true },
				{ code: "history", label: "Unlimited backtest history", included: true },
				{ code: "pro_blocks", label: "PRO blocks (Tape, Book, Open Interest)", included: true },
				{ code: "permission_run_genetic_search", label: "Genetic strategy search", included: true },
			],
		},
	],
	trial: { enabled: true, days: 0, plan: "standard", active: false },
	billing_mode: "monthly",
};

vi.mock("@/lib/api", () => ({
	usePublicPlans: () => ({ data: catalog, isLoading: false }),
}));

vi.mock("react-router-dom", () => ({
	Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
		<a href={to}>{children}</a>
	),
}));

import { Pricing } from "@/components/marketing/Pricing";

beforeAll(async () => {
	await i18n.use(initReactI18next).init({
		resources: { en: { common: {} } },
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

describe("the rendered pricing page (FIX 1)", () => {
	const html = renderToStaticMarkup(<Pricing />);

	/**
	 * Each plan is rendered in its own card. Cards are located by PRICE, not
	 * by name, so the test asks the commercial question directly ("what does
	 * the $30 tier claim?") and does not depend on how the tier is titled.
	 *
	 * Looked up lazily inside each test so one missing tier does not abort
	 * the whole file.
	 */
	const cards = html.split('<div class="relative rounded-2xl').slice(1);
	const cardForPrice = (price: string) => {
		const card = cards.find((c) => c.includes(`>${price}<`));
		if (!card) throw new Error(`No pricing card found for ${price}`);
		return card;
	};

	const freeCard = () => cardForPrice("$0");
	const standardCard = () => cardForPrice("$30");
	const professionalCard = () => cardForPrice("$100");

	it("shows the $30 tier at $30", () => {
		expect(standardCard()).toContain("$30");
	});

	it("does NOT advertise unlimited backtests for the $30 tier", () => {
		expect(standardCard()).not.toContain("Unlimited backtests");
		expect(standardCard()).toContain("50 backtests per day");
	});

	it("does NOT advertise 30 live strategies for the $30 tier", () => {
		expect(standardCard()).not.toContain("30 live trading strategies");
		expect(standardCard()).toContain("10 live trading strategies");
	});

	it("says the $30 tier does NOT include genetic search or PRO blocks", () => {
		const card = standardCard();
		const notIncludedAt = card.indexOf("Not included");
		expect(notIncludedAt).toBeGreaterThan(-1);
		// Both are listed, but under "Not included", not as entitlements.
		expect(card.indexOf("Genetic strategy search")).toBeGreaterThan(
			notIncludedAt,
		);
		expect(
			card.indexOf("PRO blocks (Tape, Book, Open Interest)"),
		).toBeGreaterThan(notIncludedAt);
	});

	it("does NOT advertise a 14-day trial", () => {
		expect(html).not.toMatch(/14-day/);
		expect(html).not.toMatch(/trial/i);
	});

	it("shows the real $100 tier that used to be hidden, with its own entitlements", () => {
		const card = professionalCard();
		expect(card).toContain("$100");
		// The $100 tier really does have unlimited backtests and 30 strategies.
		expect(card).toContain("Unlimited backtests");
		expect(card).toContain("30 live trading strategies");
		expect(card).toContain("PRO blocks (Tape, Book, Open Interest)");
	});

	it("does NOT advertise a one-off price the backend does not sell", () => {
		// Same truth, asserted on the RENDERED page: the catalog's lifetime
		// block is enabled, but the active billing_mode is "monthly", so the
		// checkout would charge $30 / $100 -- not $99 / $299.
		expect(standardCard()).not.toContain("$99");
		expect(professionalCard()).not.toContain("$299");
		expect(html).not.toContain("lifetime access");
	});

	it("keeps the free tier accurate", () => {
		expect(freeCard()).toContain("$0");
		expect(freeCard()).toContain("20 backtests per day");
		expect(freeCard()).toContain("10 AI Assistant queries per day");
	});

	it("tells the visitor that a new account starts on the Free plan", () => {
		expect(html).toContain("New accounts start on the Free plan");
	});
});
