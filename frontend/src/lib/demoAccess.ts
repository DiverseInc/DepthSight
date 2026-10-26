// src/lib/demoAccess.ts
//
// Controls whether the seeded demo account helper is shown on the public
// login page.
//
// WHY THIS IS GATED
// -----------------
// The login page is public. Showing a ready-made username and password to
// every visitor hands anyone who finds the page a logged-in session on a
// platform that holds real customer accounts, and it advertises the demo
// accounts that the public statistics now deliberately exclude.
//
// The demo helper is still there for demos and investor walkthroughs -- it
// is simply OFF unless the build explicitly asks for it.
//
// Set VITE_SHOW_DEMO=true in the environment of the build that should show
// it (e.g. a demo deployment, or `npm run dev` locally). The default is OFF,
// so the production build never renders it.

export const DEMO_ENV_FLAG = "VITE_SHOW_DEMO";

const TRUTHY = new Set(["true", "1", "yes", "on"]);

/**
 * True when the demo credentials panel should be rendered.
 *
 * Takes the environment object as an argument so it can be tested directly
 * instead of depending on how the bundler injects env vars.
 */
export const isDemoCredentialsEnabled = (
	env: Record<string, unknown> | undefined | null,
): boolean => {
	if (!env) return false;
	const raw = env[DEMO_ENV_FLAG];
	if (typeof raw === "boolean") return raw;
	if (typeof raw !== "string") return false;
	return TRUTHY.has(raw.trim().toLowerCase());
};
