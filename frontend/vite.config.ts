import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import {
	DEMO_ENV_FLAG,
	DEMO_PASSWORD_ENV,
	DEMO_USERNAME_ENV,
	demoDefine,
	demoEnvDefine,
	readEnvFiles,
} from "./demo-build-flag";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
	// The demo opt-in is read from an ENV FILE, never from `process.env`.
	// Vite's `loadEnv()` merges `process.env` over the .env files, so a
	// stray VITE_SHOW_DEMO=true in the build environment (a CI variable, a
	// shell export, a compose build arg) would otherwise turn the demo
	// helper on and pull the demo credentials into the production bundle.
	// See frontend/demo-build-flag.ts.
	const envFiles = readEnvFiles(process.cwd(), mode);
	const demoFlag = envFiles[DEMO_ENV_FLAG];

	// The demo credentials come from the BUILD ENVIRONMENT. They are never
	// read from a file under frontend/: docker-compose.yml bind-mounts that
	// whole directory into the Caddy container that serves the public site.
	const credentials = {
		username: process.env[DEMO_USERNAME_ENV],
		password: process.env[DEMO_PASSWORD_ENV],
	};

	return {
		plugins: [react(), tailwindcss()],
		define: {
			// The credentials: injected only when the build asked for the
			// demo helper AND the environment supplied both values.
			...demoDefine({ flag: demoFlag, ...credentials }),
			// And the bundle's own copy of the demo variables, pinned to the
			// env file so the ambient environment cannot leak into it.
			...demoEnvDefine(demoFlag),
		},
		resolve: {
			alias: {
				"@": path.resolve(__dirname, "./src"),
			},
		},
		optimizeDeps: {
			include: [
				"@radix-ui/react-slider",
				"react-joyride",
				"react-resizable-panels",
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
