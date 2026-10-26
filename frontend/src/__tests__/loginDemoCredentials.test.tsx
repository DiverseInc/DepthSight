// src/__tests__/loginDemoCredentials.test.tsx
//
// FIX 3 -- the PUBLIC login page must not hand a ready-made username and
// password to every visitor.
//
// The demo helper is useful for demos, so it still exists -- but only when
// the build explicitly asks for it (VITE_SHOW_DEMO=true). The default build
// must not render it.
//
// This drives the real login page, so it fails against the old login page
// (which rendered the demo card unconditionally).

import { beforeAll, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import type React from "react";
import loginEn from "@/locales/en/login.json";
import commonEn from "@/locales/en/common.json";

vi.mock("@/context/AuthContext", () => ({
	useAuth: () => ({ user: null, token: null, isLoading: false, login: vi.fn(), logout: vi.fn() }),
}));

vi.mock("@react-oauth/google", () => ({
	GoogleLogin: () => null,
}));

vi.mock("react-router-dom", () => ({
	Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
		<a href={to}>{children}</a>
	),
}));

import LoginPage from "@/pages/Login";

beforeAll(async () => {
	await i18n.use(initReactI18next).init({
		resources: { en: { login: loginEn, common: commonEn } },
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

const renderLogin = () =>
	renderToStaticMarkup(
		<QueryClientProvider client={new QueryClient()}>
			<LoginPage />
		</QueryClientProvider>,
	);

describe("the public login page (FIX 3)", () => {
	it("does NOT offer demo credentials on the default build", () => {
		const html = renderLogin();
		expect(html).not.toContain("Use demo credentials");
		expect(html).not.toContain("alex_trader");
		expect(html).not.toContain("DemoPassword123!");
	});
});
