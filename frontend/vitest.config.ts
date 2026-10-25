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

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
	plugins: [react()],
	resolve: {
		alias: {
			"@": path.resolve(__dirname, "./src"),
		},
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
