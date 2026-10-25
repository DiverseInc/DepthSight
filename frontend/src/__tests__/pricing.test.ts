// src/__tests__/pricing.test.ts
//
// FIX 1 -- the public pricing page must not advertise anything the backend
// does not actually sell.
//
// The fixture below is a verbatim copy of the catalog the backend builds
// from api/plans_config.yml (see tests/test_public_plan_catalog.py, which
// pins the same values on the server side).

import { describe, expect, it } from "vitest";
import {
	buildPricingCards,
	buildSignupNote,
	type PublicPlanCatalog,
} from "@/lib/pricing";

const freePlan = {
	key: "free",
	name: "Free",
	description: "Basic capabilities and standard strategy blocks.",
	price_usd: 0,
	active: true,
	billing_mode: "monthly" as const,
	billing: {},
	entitlements: [
		{ code: "run_vector_backtest_per_day", label: "20 backtests per day", included: true },
		{ code: "use_ai_assistant_per_day", label: "10 AI Assistant queries per day", included: true },
		{ code: "permission_run_backtest", label: "Strategy backtesting", included: true },
		{ code: "permission_run_portfolio_backtest", label: "Portfolio backtesting", included: false },
		{ code: "permission_run_genetic_search", label: "Genetic strategy search", included: false },
		{ code: "permission_train_model", label: "ML Model Laboratory", included: false },
		{ code: "live_trading", label: "Live trading", included: false },
		{ code: "live_strategies", label: "Live trading strategies", included: false },
		{ code: "history", label: "90-day backtest history", included: true },
		{ code: "pro_blocks", label: "PRO blocks (Tape, Book, Open Interest)", included: false },
		{ code: "standard_blocks", label: "Standard blocks (Logic, Indicators, Proximity)", included: true },
		{ code: "paper_trading", label: "Paper trading", included: true },
	],
};

const standardPlan = {
	key: "standard",
	name: "Standard",
	description: "For active traders. Live trading and standard blocks.",
	price_usd: 30,
	active: true,
	billing_mode: "monthly" as const,
	billing: {
		monthly: { price_usd: 30, period_days: 30 },
		lifetime: { enabled: true, price_usd: 99, slot_limit: 75 },
	},
	entitlements: [
		{ code: "run_vector_backtest_per_day", label: "50 backtests per day", included: true },
		{ code: "use_ai_assistant_per_day", label: "20 AI Assistant queries per day", included: true },
		{ code: "permission_run_backtest", label: "Strategy backtesting", included: true },
		{ code: "permission_run_portfolio_backtest", label: "Portfolio backtesting", included: true },
		{ code: "permission_run_genetic_search", label: "Genetic strategy search", included: false },
		{ code: "permission_train_model", label: "ML Model Laboratory", included: false },
		{ code: "live_trading", label: "Live trading on OKX, Binance, Bybit", included: true },
		{ code: "live_strategies", label: "10 live trading strategies", included: true },
		{ code: "history", label: "1-year backtest history", included: true },
		{ code: "pro_blocks", label: "PRO blocks (Tape, Book, Open Interest)", included: false },
		{ code: "standard_blocks", label: "Standard blocks (Logic, Indicators, Proximity)", included: true },
		{ code: "paper_trading", label: "Paper trading", included: true },
	],
};

const proPlan = {
	key: "pro",
	name: "Professional",
	description: "Maximum power. Access to all PRO blocks and genetic search.",
	price_usd: 100,
	active: true,
	billing_mode: "monthly" as const,
	billing: {
		monthly: { price_usd: 100, period_days: 30 },
		lifetime: { enabled: true, price_usd: 299, slot_limit: 25 },
	},
	entitlements: [
		{ code: "run_vector_backtest_per_day", label: "Unlimited backtests", included: true },
		{ code: "use_ai_assistant_per_day", label: "50 AI Assistant queries per day", included: true },
		{ code: "permission_run_genetic_search", label: "Genetic strategy search", included: true },
		{ code: "live_trading", label: "Live trading on OKX, Binance, Bybit", included: true },
		{ code: "live_strategies", label: "30 live trading strategies", included: true },
		{ code: "history", label: "Unlimited backtest history", included: true },
		{ code: "pro_blocks", label: "PRO blocks (Tape, Book, Open Interest)", included: true },
		{ code: "standard_blocks", label: "Standard blocks (Logic, Indicators, Proximity)", included: true },
		{ code: "paper_trading", label: "Paper trading", included: true },
	],
};

const catalog = (
	trialOverrides: Partial<PublicPlanCatalog["trial"]> = {},
): PublicPlanCatalog => ({
	plans: [freePlan, standardPlan, proPlan],
	// This is what the backend actually reports today: a trial that is
	// enabled but lasts ZERO days, which means there is no trial.
	trial: { enabled: true, days: 0, plan: "standard", active: false, ...trialOverrides },
	billing_mode: "monthly",
});

describe("pricing page (FIX 1)", () => {
	const cards = buildPricingCards(catalog());
	const standard = cards.find((c) => c.key === "standard")!;
	const free = cards.find((c) => c.key === "free")!;
	const pro = cards.find((c) => c.key === "pro")!;

	it("prices the $30 tier at $30", () => {
		expect(standard.price).toBe("$30");
	});

	it("does NOT promise unlimited backtests on the $30 tier", () => {
		expect(standard.included).not.toContain("Unlimited backtests");
		expect(standard.included).toContain("50 backtests per day");
	});

	it("does NOT promise 30 live strategies on the $30 tier", () => {
		expect(standard.included).not.toContain("30 live trading strategies");
		expect(standard.included).toContain("10 live trading strategies");
	});

	it("does NOT promise genetic strategy search on the $30 tier", () => {
		expect(standard.included).not.toContain("Genetic strategy search");
		expect(standard.excluded).toContain("Genetic strategy search");
	});

	it("does NOT promise PRO blocks on the $30 tier", () => {
		expect(standard.included).not.toContain(
			"PRO blocks (Tape, Book, Open Interest)",
		);
		expect(standard.excluded).toContain(
			"PRO blocks (Tape, Book, Open Interest)",
		);
	});

	it("does NOT promise a 14-day trial when the backend grants none", () => {
		const allText = cards
			.map((c) => `${c.cta} ${c.included.join(" ")} ${c.excluded.join(" ")}`)
			.join(" ");
		expect(allText).not.toMatch(/14-day/i);
		expect(allText).not.toMatch(/trial/i);
	});

	it("says what a new account actually starts on", () => {
		const note = buildSignupNote(catalog());
		expect(note).toContain("Free");
		expect(note).not.toMatch(/trial/i);
	});

	it("keeps the free tier accurate", () => {
		expect(free.price).toBe("$0");
		expect(free.included).toContain("20 backtests per day");
		expect(free.included).toContain("10 AI Assistant queries per day");
		expect(free.included).toContain("90-day backtest history");
	});

	it("shows the real $100 Professional tier, which was hidden before", () => {
		expect(pro).toBeDefined();
		expect(pro.price).toBe("$100");
		expect(pro.included).toContain("Unlimited backtests");
		expect(pro.included).toContain("PRO blocks (Tape, Book, Open Interest)");
	});

	it("shows the lifetime prices that were hidden before", () => {
		expect(standard.lifetimePrice).toBe("$99");
		expect(pro.lifetimePrice).toBe("$299");
	});

	it("advertises a trial only when the backend really grants one", () => {
		const withTrial = buildPricingCards(
			catalog({ enabled: true, days: 14, plan: "standard", active: true }),
		);
		const trialCard = withTrial.find((c) => c.key === "standard")!;
		expect(trialCard.cta).toBe("Start 14-day trial");
		expect(trialCard.ctaTo).toBe("/register?plan=standard");
		expect(buildSignupNote(catalog({ enabled: true, days: 14, plan: "standard", active: true }))).toContain(
			"14-day",
		);
	});

	it("never renders a card for a plan that is not active", () => {
		const withInactive = buildPricingCards({
			...catalog(),
			plans: [...catalog().plans, { ...proPlan, key: "legacy", active: false }],
		});
		expect(withInactive.find((c) => c.key === "legacy")).toBeUndefined();
	});
});
