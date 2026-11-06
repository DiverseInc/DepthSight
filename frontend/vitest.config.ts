// frontend/vitest.config.ts
//
// Minimal test setup for the frontend.
//
// The repo had no frontend test runner, which is part of why a dead
// `isAuthenticated` and a temporal-dead-zone crash in FoundationChart both
// shipped: there was no typecheck and no test to catch them.
//
// Tests run in the `node` environment: the components under test are
// rendered with `react-dom/server`, so no DOM shim is needed.

import path from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import {
	DEMO_ENV_FLAG,
	DEMO_PASSWORD_ENV,
	DEMO_USERNAME_ENV,
	demoDefine,
	demoEnvDefine,
	readEnvFiles,
} from "./demo-build-flag";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// The demo opt-in comes from an ENV FILE, exactly as it does in the app
// build -- never from `process.env`. See frontend/demo-build-flag.ts.
const demoFlag = readEnvFiles(process.cwd(), "test")[DEMO_ENV_FLAG];

export default defineConfig({
	plugins: [react()],
	// The same build-time define the app build uses, so the demo credentials
	// are `null` in the test runner too unless the test build asks for them.
	// Without this the `__DEMO_CREDENTIALS__` global would be undefined here.
	define: {
		...demoDefine({
			flag: demoFlag,
			username: process.env[DEMO_USERNAME_ENV],
			password: process.env[DEMO_PASSWORD_ENV],
		}),
		...demoEnvDefine(demoFlag),
	},
	resolve: {
		alias: {
			"@": path.resolve(__dirname, "./src"),
		},
		// The PWA has its OWN node_modules, and its own React (19). A
		// component imported from `pwa/` into a test in this project would
		// otherwise pull in a SECOND React instance, and the hooks would
		// break with "Cannot read properties of null (reading 'useContext')".
		// `dedupe` makes one React serve the whole test run.
		//
		// `@tanstack/react-query` is in the list for the same reason, and
		// one step further on: the PWA has its OWN copy of it, and that
		// copy imports React from the PWA's own node_modules. Dedupe only
		// the package the hook imports -- so without this entry the PWA's
		// React Query would still pull in the PWA's React.
		dedupe: [
			"react",
			"react-dom",
			"react-i18next",
			"@tanstack/react-query",
		],
	},
	test: {
		environment: "node",
		include: ["src/**/*.test.{ts,tsx}"],
		environmentOptions: {
			// jsdom only exposes `localStorage` for a real origin. Without this
			// the auth tests cannot exercise the token bootstrap.
			jsdom: { url: "http://localhost:3000" },
		},
	},
});
