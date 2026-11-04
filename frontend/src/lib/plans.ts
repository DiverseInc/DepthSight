// src/lib/plans.ts
//
// The plan keys the UI needs. The backend is the source of truth
// (api/plans_config.yml); this file only mirrors the parts the frontend
// cannot read from the catalog: the canonical key of a tier and the legacy
// key that is still stored on real rows.

/**
 * Legacy plan keys that are still stored on real rows (user accounts and the
 * seeded strategy templates). Each one points at the canonical plan that
 * carries the tier today. Mirrors `plan_aliases` in api/plans_config.yml.
 */
export const PLAN_ALIASES: Record<string, string> = {
	premium: "managed",
};

/**
 * The canonical key for a plan. A legacy alias resolves to the plan that
 * carries the tier today; an unknown key is returned unchanged so the caller
 * can fail closed on it.
 */
export const canonicalPlanKey = (plan?: string | null): string => {
	const key = (plan ?? "").trim().toLowerCase();
	return PLAN_ALIASES[key] ?? key;
};

/**
 * The tiers that may use the `pro_only` strategy blocks.
 * Mirrors `_PRO_BLOCK_PLANS` in api/plans.py.
 */
export const PRO_BLOCK_TIER_KEYS = ["pro", "managed", "institutional"] as const;

/** True when this plan may use the `pro_only` strategy blocks. */
export const hasProBlockAccess = (plan?: string | null): boolean =>
	(PRO_BLOCK_TIER_KEYS as readonly string[]).includes(canonicalPlanKey(plan));

/**
 * The tiers that carry the Professional feature set (e.g. the Oracle filter).
 * A managed account buys everything Professional includes, so it is in here
 * too -- and the legacy `premium` key resolves onto `managed` first.
 */
export const PROFESSIONAL_TIER_KEYS = ["pro", "managed"] as const;

/** True when this plan carries the Professional feature set. */
export const hasProfessionalTierAccess = (plan?: string | null): boolean =>
	(PROFESSIONAL_TIER_KEYS as readonly string[]).includes(canonicalPlanKey(plan));

/**
 * The name a tier is sold under. `premium` is the legacy spelling of the
 * $499 Managed tier: a template that still asks for `premium` must show the
 * customer the name of the tier they would actually buy.
 */
export const PLAN_DISPLAY_NAMES: Record<string, string> = {
	free: "Free",
	standard: "Standard",
	pro: "Professional",
	managed: "Managed",
	premium: "Managed",
};
