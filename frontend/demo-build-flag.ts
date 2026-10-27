// frontend/demo-build-flag.ts
//
// WHY THIS FILE LIVES OUTSIDE src/
// ---------------------------------
// The seeded demo account's username and password used to live in
// src/pages/Login.tsx as module-level constants. The demo panel was already
// gated behind `isDemoCredentialsEnabled()`, so the panel was not RENDERED
// in a production build -- but the strings were still compiled into the
// shipped JavaScript. Anyone who opened devtools on the public login page
// could read the demo password without clicking anything.
//
// They live here instead. This file is imported ONLY by vite.config.ts and
// vitest.config.ts -- it is not reachable from the application entry point,
// so the bundler never sees it. The credentials reach the app as a BUILD-TIME
// `define`, which Vite substitutes with the literal `null` unless the build
// explicitly asks for the demo helper. A production bundle therefore does
// not contain the strings AT ALL, rather than containing them behind a
// runtime `if`.

/** The environment variable that opts a build into showing the demo helper. */
export const DEMO_ENV_FLAG = "VITE_SHOW_DEMO";

const TRUTHY = new Set(["true", "1", "yes", "on"]);

/** True when the given raw env value opts the build into the demo helper. */
export const isDemoEnabled = (raw: unknown): boolean => {
	if (typeof raw === "boolean") return raw;
	if (typeof raw !== "string") return false;
	return TRUTHY.has(raw.trim().toLowerCase());
};

/**
 * The seeded demo account (created by scripts/seed_demo_data.py).
 *
 * These are NOT secrets -- they are a published demo login -- but they must
 * not be readable by every visitor to the public login page.
 */
export const DEMO_CREDENTIALS = {
	username: "alex_trader",
	password: "DemoPassword123!",
};

/**
 * The Vite `define` entry that carries the demo credentials into the app.
 *
 * When the build does not ask for the demo helper the value is the literal
 * `null`, so the credential strings never enter the bundle.
 */
export const demoDefine = (raw: unknown): Record<string, string> => ({
	__DEMO_CREDENTIALS__: JSON.stringify(
		isDemoEnabled(raw) ? DEMO_CREDENTIALS : null,
	),
});
