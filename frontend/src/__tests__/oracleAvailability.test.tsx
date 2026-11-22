// @vitest-environment jsdom
// src/__tests__/oracleAvailability.test.tsx
//
// THE ORACLE MUST BE HONEST ON EVERY SURFACE.
//
// The HFT dashboard carried the same fiction as the PWA:
//
//   * `StrategyControls.tsx` shipped `use_oracle: true` in its local
//     config, so the Oracle toggle read ON while the api container logged,
//     once per worker: "Oracle model not found at
//     /app/data/oracle_model.joblib". The toggle was a claim that a regime
//     filter was running when nothing could produce a regime to filter on.
//   * `OracleScanner.tsx` printed "Active" with a green dot, ALWAYS --
//     even with no oracle signal in sight. A status line that can never go
//     red is worse than no status line.
//
// These tests assert the CUSTOMER-VISIBLE TRUTH: the client believes the
// server, and the server is the only thing that knows whether a real
// oracle model exists.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

/** What the server says. Flipped by the tests. */
const serverSays = { oracleAvailable: false };

vi.mock("@/lib/api", () => ({
	useOracleAvailability: () => ({
		data: { oracleAvailable: serverSays.oracleAvailable },
		isLoading: false,
		isError: false,
		error: null,
	}),
}));

vi.mock("@/context/AuthContext", () => ({
	useAuth: () => ({
		token: "test-token",
		user: { id: 1, username: "tester", email: "t@example.com", plan: "pro" },
		isLoading: false,
	}),
}));

import { OracleScanner } from "@/features/hft-dashboard/components/OracleScanner";
import { StrategyControls } from "@/features/hft-dashboard/components/StrategyControls";
import { useHftStore } from "@/features/hft-dashboard/hooks/useHftStore";
import { applyOracleAvailability } from "@/features/hft-dashboard/lib/oracleConfig";
import type { HftConfig } from "@/features/hft-dashboard/types/hft.types";

beforeAll(() => {
	// The radix Switch needs these to render at all.
	if (!("ResizeObserver" in globalThis)) {
		globalThis.ResizeObserver = class {
			observe() {}
			unobserve() {}
			disconnect() {}
		} as unknown as typeof ResizeObserver;
	}
	if (!Element.prototype.hasPointerCapture) {
		Element.prototype.hasPointerCapture = () => false;
	}
	if (!Element.prototype.setPointerCapture) {
		Element.prototype.setPointerCapture = () => {};
	}
	if (!Element.prototype.releasePointerCapture) {
		Element.prototype.releasePointerCapture = () => {};
	}
});

afterEach(() => {
	cleanup();
	serverSays.oracleAvailable = false;
	useHftStore.setState({ oracleSymbols: [], config: null });
});

describe("the HFT dashboard believes the server about the oracle", () => {
	it("does not offer an Oracle filter while the server has no oracle model", () => {
		serverSays.oracleAvailable = false;

		render(<StrategyControls />);

		// The label is still there -- the feature is not hidden, its
		// UNAVAILABILITY is stated.
		expect(screen.getByText("Oracle")).toBeTruthy();
		expect(screen.getByText("Unavailable")).toBeTruthy();
		// And there is no switch claiming the filter is on.
		expect(
			document.querySelector('[data-slot="switch"][data-state="checked"]'),
		).toBeNull();
	});

	it("offers the Oracle filter once the server says a model is loaded", () => {
		serverSays.oracleAvailable = true;

		render(<StrategyControls />);

		expect(screen.queryByText("Unavailable")).toBeNull();
		expect(screen.getByText("Oracle")).toBeTruthy();
	});
});

describe("the config the dashboard SAVES must not claim an oracle filter", () => {
	// The stored HFT config on the server still says `use_oracle: true` --
	// that is the server's default, and it is not this client's to change.
	// What the client must never do is SAVE that claim back while there is
	// no model to act on it.
	const storedConfig = { use_oracle: true } as HftConfig;

	it("turns the oracle filter OFF when the server has no oracle model", () => {
		const saved = applyOracleAvailability(storedConfig, false);

		expect(saved.use_oracle).toBe(false);
	});

	it("leaves the customer's own setting alone when a model IS loaded", () => {
		const saved = applyOracleAvailability(storedConfig, true);

		expect(saved.use_oracle).toBe(true);
	});

	it("does not mutate the config it was given", () => {
		const original = { use_oracle: true } as HftConfig;

		applyOracleAvailability(original, false);

		expect(original.use_oracle).toBe(true);
	});
});

describe("the Oracle scanner reports what it can see", () => {
	it("does not claim the pipeline is Active when no oracle signal arrived", () => {
		useHftStore.setState({ oracleSymbols: [] });

		render(<OracleScanner />);

		expect(screen.queryByText("Active")).toBeNull();
		expect(screen.getByText("No signal")).toBeTruthy();
	});

	it("says signals are arriving when they are", () => {
		useHftStore.setState({
			oracleSymbols: [
				{
					symbol: "BTCUSDT",
					regime_confidence: 0.9,
					duration_in_regime_sec: 30,
					volatility_natr: 0.01,
					price_change_percent: 1.5,
					confidence: 0.93,
					volume_24h: 1_000_000,
					timestamp: 1,
				},
			],
		});

		render(<OracleScanner />);

		expect(screen.getByText("Receiving")).toBeTruthy();
		expect(screen.queryByText("Active")).toBeNull();
	});
});
