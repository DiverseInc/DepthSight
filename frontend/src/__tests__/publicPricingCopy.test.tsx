// src/__tests__/publicPricingCopy.test.tsx
//
// The PUBLIC pricing copy must be TRUE on every surface that carries it.
//
// Three surfaces carry it:
//
//   * frontend/src/components/marketing/Pricing.tsx -- the React pricing
//     section, rendered by frontend/src/pages/LandingPage.tsx;
//   * frontend/src/lib/pricing.ts -- the helper that builds the React
//     cards and the sentence under them;
//   * frontend/src/locales/en/register.json -- the copy the register page
//     shows a visitor who arrived from a pricing call to action
//     (frontend/welcome.html links to /register?plan=standard and
//     /register?plan=pro, and /register is a public route).
//
// The product cannot do two of the things the old copy promised:
//
//   * there is no SELF-SERVE upgrade. A paid tier is granted by the
//     payment webhook or by an operator, and the webhook needs a payment
//     provider the default deployment does not run:
//     api/routes/payments.py:121-125 raises "Payment provider is not
//     configured." without it, and deploy.sh:135 ships Bitcart disabled;
//   * there is no subscription and no route that cancels one.
//
// So the copy may promise neither. tests/test_public_pricing_matches_catalog.py
// pins the same rules against the source; this file pins them against
// the RENDERED output, which is what a visitor actually reads.

import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type React from "react";
import type { PublicPlanCatalog } from "@/lib/pricing";
import registerEn from "@/locales/en/register.json";

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
				{ code: "run_vector_backtest_day", label: "20 backtests per day", included: true },
				{ code: "use_ai_assistant_day", label: "10 AI Assistant queries per day", included: true },
				{ code: "permission_run_backtest", label: "Strategy backtesting", included: true },
				{ code: "paper_trading", label: "Paper trading", included: true },
			],
		},
		{
			key: "standard",
			name: "Standard",
			description: "For active traders. Live trading and standard blocks.",
			price_usd: 30,
			active: true,
			billing_mode: "monthly",
			billing: { monthly: { price_usd: 30, period_days: 30 } },
			entitlements: [
				{ code: "run_vector_backtest_day", label: "50 backtests per day", included: true },
				{ code: "use_ai_assistant_day", label: "20 AI Assistant queries per day", included: true },
				{ code: "permission_run_backtest", label: "Strategy backtesting", included: true },
				{ code: "live_trading", label: "Live trading on OKX, Binance, Bybit", included: true },
				{ code: "paper_trading", label: "Paper trading", included: true },
			],
		},
		{
			key: "pro",
			name: "Professional",
			description: "Maximum power. Access to all PRO blocks and genetic search.",
			price_usd: 100,
			active: true,
			billing_mode: "monthly",
			billing: { monthly: { price_usd: 100, period_days: 30 } },
			entitlements: [
				{ code: "run_vector_backtest_day", label: "Unlimited backtests", included: true },
				{ code: "permission_run_genetic_search", label: "Genetic strategy search", included: true },
				{ code: "permission_train_model", label: "ML Model Laboratory", included: true },
				{ code: "live_trading", label: "Live trading on OKX, Binance, Bybit", included: true },
				{ code: "paper_trading", label: "Paper trading", included: true },
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
import { buildSignupNote, lifetimePriceFor } from "@/lib/pricing";

const html = renderToStaticMarkup(<Pricing />);
const note = buildSignupNote(catalog);
const registerCopy = registerEn.selectedPlan;

/** Every piece of copy a visitor reads on the public pricing surfaces. */
const copy = [html, note, registerCopy].join(" ");

describe("the public pricing copy", () => {
	it("does NOT promise a self-serve upgrade", () => {
		expect(copy).not.toMatch(/\bupgrad\w*/i);
	});

	it("does NOT promise a cancellation", () => {
		expect(copy).not.toMatch(/\bcancel\w*/i);
	});

	it("says what a new account actually starts on", () => {
		expect(note).toContain("New accounts start on the Free plan");
	});

	it("says the paid tier is set up for the customer", () => {
		// There is no self-serve upgrade to promise, so the copy has to
		// say what actually happens instead.
		expect(note).toMatch(/we will set up|activate the paid tier/i);
		expect(registerCopy).toMatch(/contact us and we will set it up for you/i);
	});

	it("keeps naming the plan the visitor selected", () => {
		expect(registerCopy).toContain("{{plan}}");
	});

	it("keeps saying the account starts on the tier it is entitled to", () => {
		expect(registerCopy).toContain("start on the plan they are entitled to");
	});

	it("keeps the free tier's promise that no card is needed", () => {
		expect(html).toContain("No credit card to start");
	});
});

describe("the one-off lifetime price", () => {
	// The catalog exposes BOTH the lifetime block (enabled, price_usd) and
	// the active billing_mode. This deployment bills MONTHLY, so the
	// one-off price is configured but NOT sold. The live catalog proves
	// it: /api/v1/public/plans returns, for the standard plan,
	// "billing_mode": "monthly" together with
	// "lifetime": { "enabled": true, "price_usd": 99 }.
	const standard = catalog.plans[1];
	const withLifetimeOffer = {
		...standard,
		billing: { lifetime: { enabled: true, price_usd: 99, slot_limit: 75 } },
	};

	it("does NOT advertise a one-off price while the deployment bills monthly", () => {
		expect(standard.billing_mode).toBe("monthly");
		expect(lifetimePriceFor(withLifetimeOffer)).toBeNull();
	});

	it("advertises the one-off price when the deployment actually sells one", () => {
		expect(
			lifetimePriceFor({ ...withLifetimeOffer, billing_mode: "lifetime" as const }),
		).toBe("$99");
	});
});
