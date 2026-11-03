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
//
// It is not enough for the panel to be OFF: the credential strings must not
// be SHIPPED either. The credentials are therefore injected at build time
// (see DEMO_CREDENTIALS below), so a production bundle does not contain
// them at all.

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

export interface DemoCredentials {
	username: string;
	password: string;
}

/**
 * The seeded demo account, injected at BUILD time.
 *
 * `__DEMO_CREDENTIALS__` is a Vite `define` (see frontend/demo-build-flag.ts
 * and frontend/vite.config.ts). The bundler replaces it with the literal
 * `null` for every build that does not ask for the demo helper, so the
 * credential strings are not compiled into a production bundle at all.
 *
 * The credentials used to be module-level constants in pages/Login.tsx. The
 * demo panel was already gated behind `isDemoCredentialsEnabled()`, so it was
 * not RENDERED in production -- but the strings were still readable in the
 * shipped JavaScript by anyone who opened devtools.
 *
 * This is `null` unless the build set VITE_SHOW_DEMO to a truthy value.
 */
declare const __DEMO_CREDENTIALS__: DemoCredentials | null;

export const DEMO_CREDENTIALS: DemoCredentials | null = __DEMO_CREDENTIALS__;

/**
 * True when this build actually carries the demo credentials.
 *
 * The panel is rendered only when BOTH the build asked for the demo helper
 * AND the credentials were compiled in, so the two can never disagree.
 */
export const areDemoCredentialsAvailable = DEMO_CREDENTIALS !== null;
