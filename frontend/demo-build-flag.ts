// frontend/demo-build-flag.ts
//
// WHY THE CREDENTIALS ARE NOT IN THIS FILE
// -----------------------------------------
// The seeded demo account's username and password used to live in
// src/pages/Login.tsx as module-level constants. The demo panel was already
// gated behind `isDemoCredentialsEnabled()`, so the panel was not RENDERED
// in a production build -- but the strings were still compiled into the
// shipped JavaScript. Anyone who opened devtools on the public login page
// could read the demo password without clicking anything.
//
// They then moved here, as literals in this file. That was better, but not
// good enough. docker-compose.yml bind-mounts the WHOLE frontend/ directory
// read-only into the Caddy container that serves the public site
// (docker-compose.yml:289, mounted at /srv/frontend), and Caddy serves
// frontend/welcome.html out of that mount. A plaintext credential sitting in
// a directory that is mounted into a web-facing container is one handler
// edit away from being published.
//
// So the credential VALUES come from the BUILD ENVIRONMENT now:
//
//   VITE_DEMO_USERNAME   the seeded demo username
//   VITE_DEMO_PASSWORD   the seeded demo password
//
// They are read from the environment OF THE BUILD, never from a file under
// frontend/, so no plaintext credential sits in the served tree at all. The
// demo account itself is a PUBLISHED login (docs/quick-start.html:125 and the
// signup email, api/routes/auth.py:61-63), not a secret -- but "published" is
// not the same as "sitting in the directory the web server reads".
//
// WHY THE OPT-IN COMES FROM AN ENV FILE, NOT FROM process.env
// -----------------------------------------------------------
// Vite's `loadEnv()` merges `process.env` OVER the .env files, so a stray
// VITE_SHOW_DEMO=true in the build environment -- a CI variable, a shell
// export, a compose build arg -- would turn the demo helper on and pull the
// credentials into the bundle. That is exactly what the .dockerignore fix
// was meant to prevent, and a .dockerignore can only exclude FILES.
//
// So the opt-in is read from the env FILE only, through `readEnvFiles()`
// below, which never looks at `process.env`. The build therefore opts in
// like this:
//
//   frontend/.env.production.local   ->   VITE_SHOW_DEMO=true
//   the build environment            ->   VITE_DEMO_USERNAME=... VITE_DEMO_PASSWORD=...
//
// and the credentials are injected only when BOTH the opt-in and both values
// are present. Otherwise the define is the literal `null` and the credential
// strings never enter the bundle.
//
// WHY THIS FILE LIVES OUTSIDE src/ AND INSIDE frontend/
// ---------------------------------------------------
// It is imported ONLY by vite.config.ts and vitest.config.ts -- it is not
// reachable from the application entry point, so the bundler never sees it.
// The credentials reach the app as a BUILD-TIME `define`, which Vite
// substitutes with the literal `null` unless the build explicitly asks for
// the demo helper. A production bundle therefore does not contain the
// strings AT ALL, rather than containing them behind a runtime `if`.
//
// It stays INSIDE frontend/ on purpose: docker-compose.yml builds the
// frontend image with `context: ./frontend` (the frontend service, line 260).
// A module outside that directory is not in the build, so `docker compose
// build frontend` would fail on an import it cannot resolve -- while the
// bundle test, which builds in the checkout, would still pass.
// tests/test_production_bundle_excludes_demo_credentials.py pins that every
// import of the Vite config stays inside the build context.

import { readFileSync } from "node:fs";
import { join } from "node:path";

/** The environment variable that opts a build into showing the demo helper. */
export const DEMO_ENV_FLAG = "VITE_SHOW_DEMO";

/**
 * The build-environment variables that carry the demo credentials.
 *
 * They are read from the environment OF THE BUILD. They are never read from
 * a file under frontend/, because that whole directory is bind-mounted into
 * the container that serves the public site.
 */
export const DEMO_USERNAME_ENV = "VITE_DEMO_USERNAME";
export const DEMO_PASSWORD_ENV = "VITE_DEMO_PASSWORD";

const TRUTHY = new Set(["true", "1", "yes", "on"]);

/** True when the given raw env value opts the build into the demo helper. */
export const isDemoEnabled = (raw: unknown): boolean => {
	if (typeof raw === "boolean") return raw;
	if (typeof raw !== "string") return false;
	return TRUTHY.has(raw.trim().toLowerCase());
};

/**
 * The env files Vite reads for a mode, in increasing precedence.
 *
 * Mirrors Vite's own `getEnvFilesForMode`, so the file the demo opt-in is
 * read from is the file Vite would have read it from.
 */
export const envFileNames = (mode: string): string[] => [
	".env",
	".env.local",
	`.env.${mode}`,
	`.env.${mode}.local`,
];

/**
 * Parse the contents of one dotenv-style file.
 *
 * Deliberately small: `KEY=value`, optional `export `, `#` comments and
 * quoted values. That is all an opt-in flag needs.
 */
export const parseEnvFile = (text: string): Record<string, string> => {
	const parsed: Record<string, string> = {};
	for (const rawLine of text.split(/\r?\n/)) {
		const line = rawLine.trim();
		if (!line || line.startsWith("#")) continue;

		const assignment = line.startsWith("export ")
			? line.slice("export ".length).trim()
			: line;
		const separator = assignment.indexOf("=");
		if (separator === -1) continue;

		const key = assignment.slice(0, separator).trim();
		if (!key) continue;

		let value = assignment.slice(separator + 1).trim();
		if (
			value.length > 1 &&
			((value.startsWith('"') && value.endsWith('"')) ||
				(value.startsWith("'") && value.endsWith("'")))
		) {
			value = value.slice(1, -1);
		}
		parsed[key] = value;
	}
	return parsed;
};

/**
 * Read the env FILES for a mode.
 *
 * `process.env` is deliberately NOT consulted. Vite's `loadEnv()` merges it
 * over the files, so a stray VITE_SHOW_DEMO in the build environment would
 * otherwise decide whether the demo helper ships.
 */
export const readEnvFiles = (dir: string, mode: string): Record<string, string> => {
	const merged: Record<string, string> = {};
	for (const name of envFileNames(mode)) {
		let text = "";
		try {
			text = readFileSync(join(dir, name), "utf-8");
		} catch {
			continue;
		}
		Object.assign(merged, parseEnvFile(text));
	}
	return merged;
};

/**
 * The demo credentials the build environment supplies, or null.
 *
 * Both values are required: a username without a password (or the other way
 * round) is not a usable login, and half a credential is still a credential
 * in the bundle.
 */
export const demoCredentialsFromEnv = (
	username: string | undefined,
	password: string | undefined,
): { username: string; password: string } | null => {
	const name = (username ?? "").trim();
	const secret = password ?? "";
	if (!name || !secret) return null;
	return { username: name, password: secret };
};

/**
 * The Vite `define` entry that carries the demo credentials into the app.
 *
 * The credentials are injected ONLY when the build explicitly asked for the
 * demo helper (the opt-in, read from an env FILE) AND the build environment
 * supplied both values. Otherwise the value is the literal `null`, so the
 * credential strings never enter the bundle.
 */
export const demoDefine = (options: {
	flag: unknown;
	username?: string | undefined;
	password?: string | undefined;
}): Record<string, string> => {
	const credentials = isDemoEnabled(options.flag)
		? demoCredentialsFromEnv(options.username, options.password)
		: null;
	return { __DEMO_CREDENTIALS__: JSON.stringify(credentials) };
};

/**
 * The `define` entries that pin the demo variables in the BUNDLE.
 *
 * Vite builds `import.meta.env` from `loadEnv()`, which merges
 * `process.env` over the env files. So without these the built JavaScript
 * would carry whatever the build environment happened to hold -- including
 * the demo PASSWORD, in a build that was never asked for the demo.
 *
 * These entries make the bundle say exactly what the env FILE says:
 * the opt-in is the file's value, and the credentials are blank, because
 * they travel in `__DEMO_CREDENTIALS__` and nowhere else.
 */
export const demoEnvDefine = (flag: unknown): Record<string, string> => ({
	[`import.meta.env.${DEMO_ENV_FLAG}`]: JSON.stringify(
		isDemoEnabled(flag) ? "true" : "",
	),
	[`import.meta.env.${DEMO_USERNAME_ENV}`]: JSON.stringify(""),
	[`import.meta.env.${DEMO_PASSWORD_ENV}`]: JSON.stringify(""),
});
