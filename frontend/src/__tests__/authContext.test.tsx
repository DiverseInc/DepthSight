// @vitest-environment jsdom
// src/__tests__/authContext.test.tsx
//
// P0-A (Linear DIV-12) -- `useAuth().isAuthenticated` was `undefined` at
// runtime. The AuthContext interface never declared it and the provider
// never supplied it, so:
//   * PaperModeBanner returned null for every client (no paper-trading
//     warning on a simulated balance),
//   * LandingPage never redirected a signed-in visitor to their dashboard,
//   * MarketingNav always rendered the signed-out links.
//
// These tests mount the REAL provider and the REAL landing page.

import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import type React from "react";

const apiClientMock = vi.fn();

vi.mock("@/lib/apiClient", () => ({
	apiClient: (...args: unknown[]) => apiClientMock(...args),
}));

// The marketing sections are irrelevant to this test and pull in a lot of
// dependencies, so they are stubbed out.
vi.mock("@/components/marketing/MarketingNav", () => ({
	MarketingNav: () => <div data-testid="marketing-nav" />,
}));
vi.mock("@/components/marketing/Hero", () => ({ Hero: () => <div /> }));
vi.mock("@/components/marketing/Features", () => ({ Features: () => <div /> }));
vi.mock("@/components/marketing/Pricing", () => ({ Pricing: () => <div /> }));
vi.mock("@/components/marketing/CTAFooter", () => ({ CTAFooter: () => <div /> }));

import { AuthProvider, useAuth } from "@/context/AuthContext";
import LandingPage from "@/pages/LandingPage";

const AuthProbe = () => {
	const { isAuthenticated, user } = useAuth();
	return (
		<div>
			<span data-testid="is-authenticated">{String(isAuthenticated)}</span>
			<span data-testid="has-user">{String(!!user)}</span>
		</div>
	);
};

const LocationProbe = () => {
	const location = useLocation();
	return <span data-testid="pathname">{location.pathname}</span>;
};

const wrapper = ({ children }: { children: React.ReactNode }) => (
	<QueryClientProvider client={new QueryClient()}>
		<MemoryRouter initialEntries={["/welcome"]}>{children}</MemoryRouter>
	</QueryClientProvider>
);

const tokenStore = new Map<string, string>();

/**
 * jsdom in this runner does not expose `localStorage`, so it is stubbed with
 * the small surface the app actually uses.
 */
const installLocalStorage = () => {
	vi.stubGlobal("localStorage", {
		getItem: (key: string) => tokenStore.get(key) ?? null,
		setItem: (key: string, value: string) => {
			tokenStore.set(key, value);
		},
		removeItem: (key: string) => {
			tokenStore.delete(key);
		},
		clear: () => tokenStore.clear(),
	});
};

describe("AuthContext.isAuthenticated (P0-A / DIV-12)", () => {
	afterEach(() => {
		cleanup();
	});

	beforeEach(() => {
		tokenStore.clear();
		installLocalStorage();
		apiClientMock.mockReset();
	});

	it("supplies isAuthenticated as a real boolean when signed out", async () => {
		render(<AuthProvider><AuthProbe /></AuthProvider>, { wrapper });
		await waitFor(() =>
			expect(screen.getByTestId("has-user").textContent).toBe("false"),
		);
		// Before the fix this was the string "undefined".
		expect(screen.getByTestId("is-authenticated").textContent).toBe("false");
	});
	it("reports isAuthenticated true once the user is loaded", async () => {
		localStorage.setItem("authToken", "test-token");
		apiClientMock.mockResolvedValue({
			id: 1,
			username: "someone",
			email: "someone@example.com",
			plan: "free",
		});

		render(<AuthProvider><AuthProbe /></AuthProvider>, { wrapper });

		await waitFor(() =>
			expect(screen.getByTestId("is-authenticated").textContent).toBe("true"),
		);
		expect(screen.getByTestId("has-user").textContent).toBe("true");
	});
});

describe("LandingPage redirect (P0-A / DIV-12)", () => {
	afterEach(() => {
		cleanup();
	});

	beforeEach(() => {
		tokenStore.clear();
		installLocalStorage();
		apiClientMock.mockReset();
	});

	it("sends a signed-in visitor to their dashboard", async () => {
		localStorage.setItem("authToken", "test-token");
		apiClientMock.mockResolvedValue({
			id: 1,
			username: "someone",
			email: "someone@example.com",
			plan: "free",
		});

		render(
			<>
				<AuthProvider>
					<LandingPage />
					<LocationProbe />
				</AuthProvider>
			</>,
			{ wrapper },
		);

		await waitFor(() =>
			expect(screen.getByTestId("pathname").textContent).toBe("/dashboard"),
		);
	});
});
