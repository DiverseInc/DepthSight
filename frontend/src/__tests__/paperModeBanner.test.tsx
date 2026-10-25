// @vitest-environment jsdom
// src/__tests__/paperModeBanner.test.tsx
//
// P0-A (Linear DIV-12) -- `useAuth().isAuthenticated` was `undefined` at
// runtime, because the AuthContext interface never declared it and the
// provider never supplied it. The global "you are in paper trading mode"
// banner therefore returned null for EVERY client: a customer looking at a
// simulated $10,000 balance saw no paper-trading warning at all.
//
// This mounts the REAL AuthProvider (not a mock) so the test proves the
// banner is actually live for a signed-in user.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type React from "react";

const apiClientMock = vi.fn();

vi.mock("@/lib/apiClient", () => ({
	apiClient: (...args: unknown[]) => apiClientMock(...args),
}));

vi.mock("@/context/PortfolioModeContext", () => ({
	usePortfolioMode: () => ({ mode: "paper" }),
}));

import { AuthProvider } from "@/context/AuthContext";
import { PaperModeBanner } from "@/components/shared/PaperModeBanner";

const tokenStore = new Map<string, string>();

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

const wrapper = ({ children }: { children: React.ReactNode }) => (
	<QueryClientProvider client={new QueryClient()}>
		<MemoryRouter>{children}</MemoryRouter>
	</QueryClientProvider>
);

describe("PaperModeBanner for a signed-in paper trader (P0-A / DIV-12)", () => {
	afterEach(() => {
		cleanup();
	});

	beforeEach(() => {
		tokenStore.clear();
		installLocalStorage();
		apiClientMock.mockReset();
	});

	it("tells a signed-in paper trader that the trades are simulated", async () => {
		tokenStore.set("authToken", "test-token");
		apiClientMock.mockResolvedValue({
			id: 1,
			username: "someone",
			email: "someone@example.com",
			plan: "free",
		});

		render(
			<AuthProvider>
				<PaperModeBanner />
			</AuthProvider>,
			{ wrapper },
		);

		await waitFor(() =>
			expect(screen.getByText(/Paper Trading mode/i)).toBeTruthy(),
		);
		expect(screen.getByText(/simulated/i)).toBeTruthy();
	});
});
