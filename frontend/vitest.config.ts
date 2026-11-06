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
		// The PWA has NO test runner of its own, so the tests that pin its
		// behaviour live in this runner and import the PWA code across the
		// project boundary (`../../pwa/...`). That is a test-only dependency.
		//
		// The PWA has its OWN node_modules, so a bare import inside a PWA
		// file ("react", "zustand", ...) would resolve to the PWA's copy --
		// or fail outright, because the PWA's dependencies are not installed
		// in this worktree. `dedupe` forces every one of them to the copy in
		// THIS project, so a PWA component and a frontend component run
		// against one React and one React Query.
		dedupe: [
			"react",
			"react-dom",
			"react-i18next",
			"i18next",
			"i18next-browser-languagedetector",
			"@tanstack/react-query",
			"lucide-react",
			"zustand",
			"immer",
			"uuid",
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
