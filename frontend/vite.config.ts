import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import { demoDefine } from "../tools/demo-build-flag";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
	// The seeded demo credentials are injected at BUILD time, not fetched at
	// runtime. Unless this build explicitly asks for the demo helper
	// (VITE_SHOW_DEMO=true) the define is the literal `null`, so the
	// credential strings are not present in the production bundle at all.
	// See frontend/demo-build-flag.ts.
	const env = loadEnv(mode, process.cwd(), "VITE_");

	return {
		plugins: [react(), tailwindcss()],
		define: demoDefine(env.VITE_SHOW_DEMO),
		resolve: {
			alias: {
				"@": path.resolve(__dirname, "./src"),
			},
		},
		optimizeDeps: {
			include: [
				"@radix-ui/react-slider",
				"react-joyride",
				"@tanstack/react-resizable-panels",
			],
		},
		server: {
			host: "0.0.0.0",
			port: 5173,
			proxy: {
				"/api": {
					target: "http://api:8000",
					changeOrigin: true,
					secure: false,
				},
				"/ws": {
					target: "http://api:8765",
					ws: true,
				},
				"/og-image": {
					target: "http://localhost:8000",
					changeOrigin: true,
				},
			},
		},
	};
});
