// src/lib/pricing.ts
//
// Turns the plan catalog served by the backend into the cards the marketing
// pricing page renders.
//
// WHY THIS EXISTS
// ---------------
// The pricing page used to carry a HAND-WRITTEN list of plans and features.
// It advertised a $30 "Pro" tier with unlimited backtests, 30 live strategies,
// PRO blocks and genetic strategy search, none of which the $30 tier
// actually grants, and it advertised a 14-day trial the backend never gives.
//
// Everything on the page is now derived from the backend's own catalog
// (`GET /api/v1/public/plans`), which is built from api/plans_config.yml.
// The page can therefore never drift away from what the backend sells.

export interface PlanEntitlement {
	code: string;
	label: string;
	included: boolean;
}

export interface PublicPlan {
	key: string;
	name: string;
	description: string;
	price_usd: number;
	active: boolean;
	billing_mode?: "monthly" | "lifetime";
	billing?: {
		monthly?: { price_usd?: number; period_days?: number };
		lifetime?: {
			enabled?: boolean;
			price_usd?: number;
			slot_limit?: number;
		};
	};
	quotas?: Record<string, number>;
	permissions?: string[];
	limits?: Record<string, unknown>;
	entitlements: PlanEntitlement[];
}

export interface TrialInfo {
	enabled: boolean;
	days: number;
	plan: string | null;
	/** True only when the trial is enabled AND lasts more than zero days. */
	active: boolean;
}

export interface PublicPlanCatalog {
	plans: PublicPlan[];
	trial: TrialInfo;
	billing_mode?: "monthly" | "lifetime";
}

export interface PricingCard {
	key: string;
	name: string;
	tagline: string;
	price: string;
	period: string;
	/** What the tier DOES include. */
	included: string[];
	/** What the tier does NOT include, stated plainly. */
	excluded: string[];
	/** Lifetime price, when the tier is sold as a one-off. */
	lifetimePrice: string | null;
	cta: string;
	ctaTo: string;
	highlighted: boolean;
	badge?: string;
}

/** The tier a brand new account starts on. */
export const FREE_PLAN_KEY = "free";

export const formatUsd = (amount: number): string => {
	if (!Number.isFinite(amount)) return "$0";
	return `$${Math.round(amount)}`;
};

/**
 * The lifetime price for a plan, or null when the tier is not sold as a
 * one-off. Comes from the backend catalog, so it cannot be stale.
 */
export const lifetimePriceFor = (plan: PublicPlan): string | null => {
	const lifetime = plan.billing?.lifetime;
	if (!lifetime?.enabled) return null;
	if (typeof lifetime.price_usd !== "number") return null;
	return formatUsd(lifetime.price_usd);
};

/**
 * Build the cards for the pricing page.
 *
 * The trial claim is derived from the backend's `trial` block. A trial is
 * only advertised when the backend says one is really running
 * (`active` = enabled AND more than zero days) -- so the page can never
 * promise a trial the backend does not grant.
 */
export const buildPricingCards = (
	catalog: PublicPlanCatalog | null | undefined,
): PricingCard[] => {
	if (!catalog || !Array.isArray(catalog.plans)) return [];

	const trial = catalog.trial;
	const trialIsReal = Boolean(trial?.active && trial.days > 0);

	return catalog.plans
		.filter((plan) => plan.active !== false)
		.map((plan, index) => {
			const included = (plan.entitlements ?? [])
				.filter((ent) => ent.included)
				.map((ent) => ent.label);
			const excluded = (plan.entitlements ?? [])
				.filter((ent) => !ent.included)
				.map((ent) => ent.label);

			const isFree = plan.key === FREE_PLAN_KEY;
			const isTrialTier = trialIsReal && trial.plan === plan.key;

			let cta: string;
			let ctaTo: string;
			if (isFree) {
				cta = "Start free";
				ctaTo = "/register";
			} else if (isTrialTier) {
				cta = `Start ${trial.days}-day trial`;
				ctaTo = `/register?plan=${encodeURIComponent(plan.key)}`;
			} else {
				cta = `Choose ${plan.name}`;
				ctaTo = `/register?plan=${encodeURIComponent(plan.key)}`;
			}

			return {
				key: plan.key,
				name: plan.name,
				tagline: plan.description ?? "",
				price: formatUsd(plan.price_usd),
				period: plan.price_usd === 0 ? "forever" : "per month",
				included,
				excluded,
				lifetimePrice: lifetimePriceFor(plan),
				cta,
				ctaTo,
				// Highlight the tier a visitor is most likely to want: the paid
				// tier that is not the most expensive one.
				highlighted: !isFree && index === 1,
				badge: !isFree && index === 1 ? "Most popular" : undefined,
			};
		});
};

/**
 * The sentence shown under the pricing grid.
 *
 * It states what a new account actually gets, so the page never implies a
 * trial (or a paid tier at signup) that the backend does not grant.
 */
export const buildSignupNote = (
	catalog: PublicPlanCatalog | null | undefined,
): string => {
	if (!catalog || !Array.isArray(catalog.plans)) return "";
	const trial = catalog.trial;
	if (trial?.active && trial.days > 0) {
		const trialPlan = catalog.plans.find((p) => p.key === trial.plan);
		const trialName = trialPlan?.name ?? trial.plan;
		return `Every new account starts with a ${trial.days}-day ${trialName} trial. No credit card required.`;
	}
	const paid = catalog.plans.filter(
		(p) => p.active !== false && p.key !== FREE_PLAN_KEY,
	);
	if (paid.length === 0) {
		return "New accounts start on the Free plan.";
	}
	const names = paid.map((p) => p.name).join(" or ");
	return `New accounts start on the Free plan. You can upgrade to ${names} at any time from your account.`;
};
