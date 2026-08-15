// src/main.tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";
import "./i18n";
import { HelmetProvider } from "react-helmet-async";
import { GoogleOAuthProvider } from "@react-oauth/google";

const googleClientId = import.meta.env.VITE_GOOGLE_CLIENT_ID || "";

const rootElement = document.getElementById("root")!;
if (!rootElement.innerHTML) {
	const root = ReactDOM.createRoot(rootElement);
	// Skip the GoogleOAuthProvider wrapper when no client id is configured
	// so a click on a stray Google button can't trigger a broken OAuth flow.
	const appTree = googleClientId ? (
		<GoogleOAuthProvider clientId={googleClientId}>
			<App />
		</GoogleOAuthProvider>
	) : (
		<App />
	);
	root.render(
		<React.StrictMode>
			<HelmetProvider>{appTree}</HelmetProvider>
		</React.StrictMode>,
	);
}
