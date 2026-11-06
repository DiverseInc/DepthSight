// @vitest-environment jsdom
// src/__tests__/pwaOracleAvailability.test.tsx
//
// THE PWA'S ORACLE AVAILABILITY MUST NOT DRIFT FROM THE SERVER.
//
// The PWA's hook (`pwa/lib/useOracleAvailability.ts`) used to fetch the
// capability ONCE per mount -- a plain `useEffect` with an empty
// dependency array -- and never again. The answer could therefore drift
// away from the server for the whole life of the mount: an open screen
// could keep saying "no oracle here" after a real model was deployed, and
// could keep saying "the oracle is available" after the model file was
// taken away. The feature could not light up by itself on the day a
// model was deployed.
//
// These tests are BEHAVIOURAL. They mount the REAL PWA hook, with the
// PWA's own api service mocked, and watch what the hook REPORTS as time
// passes and as the app comes back to the foreground. They are not greps
// of the hook's source text.
//
// The PWA has no test runner of its own, so -- like
// `pwaOracleCapability.test.ts` and `pwaOracleLaunch.test.tsx` -- this
// test lives in the repository's only front-end runner and imports the
// PWA code across the project boundary. That is a test-only dependency;
// the product code of the frontend does not depend on the PWA.

import { act, cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../pwa/services/api", () => ({
	api: {
		getCapabilities: vi.fn(),
	},
}));

import { api } from "../../../pwa/services/api";
import { useOracleAvailability } from "../../../pwa/lib/useOracleAvailability";

const getCapabilities = vi.mocked(api.getCapabilities);

/**
 * How often the client must re-ask the server.
 *
 * Spelled out HERE, and not read from the hook, on purpose: a test that
 * takes its expectation from the code it is testing cannot catch that
 * code shrinking the interval. The web client already refreshes on this
 * cadence, and the PWA must not be slower than the client beside it.
 */
const REFRESH_MS = 60_000;

/** What the server says RIGHT NOW. Flipped by the tests. */
const serverSays = { oracleAvailable: false, oracleDefaultConfidence: 95 };

let queryClient: QueryClient;

/**
 * The PWA mounts a `QueryClientProvider` at its root (`pwa/index.tsx`), so
 * the hook is rendered inside one here too.
 */
function wrapper({ children }: { children: ReactNode }) {
	return (
		<QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
	);
}

/**
 * Let the timers, the promises and React's state updates settle.
 *
 * The fake clock is what makes "60 seconds later" a fact rather than a
 * wait: the refresh interval is proven by the clock, not by patience.
 */
async function settle(ms = 0): Promise<void> {
	await act(async () => {
		await vi.advanceTimersByTimeAsync(ms);
	});
}

beforeEach(() => {
	vi.useFakeTimers();
	queryClient = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	serverSays.oracleAvailable = false;
	serverSays.oracleDefaultConfidence = 95;
	getCapabilities.mockReset();
	getCapabilities.mockImplementation(async () => ({
		oracleAvailable: serverSays.oracleAvailable,
		oracleDefaultConfidence: serverSays.oracleDefaultConfidence,
	}));
});

afterEach(() => {
	cleanup();
	vi.useRealTimers();
	// `visibilityState` is restored to the prototype's own value.
	Reflect.deleteProperty(document, "visibilityState");
});

describe("the PWA asks the server again instead of serving a stale answer", () => {
	it("re-asks when the refresh interval elapses, and reports the NEW answer", async () => {
		const { result } = renderHook(() => useOracleAvailability(), { wrapper });

		await settle(1);

		// The first answer: this deployment has no oracle model.
		expect(getCapabilities).toHaveBeenCalledTimes(1);
		expect(result.current.loaded).toBe(true);
		expect(result.current.oracleAvailable).toBe(false);

		// A real oracle model is deployed on the server.
		serverSays.oracleAvailable = true;

		await settle(REFRESH_MS);
		await settle(1);

		// The client ASKED AGAIN, and it reports the NEW answer -- not the
		// one it happened to receive when it mounted.
		expect(getCapabilities).toHaveBeenCalledTimes(2);
		expect(result.current.oracleAvailable).toBe(true);
	});

	it("re-asks when the app comes back to the foreground", async () => {
		const { result } = renderHook(() => useOracleAvailability(), { wrapper });

		await settle(1);

		expect(getCapabilities).toHaveBeenCalledTimes(1);
		expect(result.current.oracleAvailable).toBe(false);

		// The app goes to the background, and a model is deployed while it
		// is there.
		serverSays.oracleAvailable = true;
		Object.defineProperty(document, "visibilityState", {
			configurable: true,
			value: "hidden",
		});
		await act(async () => {
			window.dispatchEvent(new Event("visibilitychange"));
		});

		// The app comes back to the foreground.
		Object.defineProperty(document, "visibilityState", {
			configurable: true,
			value: "visible",
		});
		await act(async () => {
			window.dispatchEvent(new Event("visibilitychange"));
		});
		await settle(1);

		expect(getCapabilities).toHaveBeenCalledTimes(2);
		expect(result.current.oracleAvailable).toBe(true);
	});
});

describe("the PWA fails CLOSED", () => {
	it("says NO when the probe fails", async () => {
		getCapabilities.mockRejectedValue(new Error("the server did not answer"));

		const { result } = renderHook(() => useOracleAvailability(), { wrapper });

		await settle(1);

		// A probe that could not be completed is NOT an available oracle.
		expect(result.current.oracleAvailable).toBe(false);
		// ...and the probe HAS answered, so the screen can say "this
		// deployment has no oracle" rather than "still loading".
		expect(result.current.loaded).toBe(true);
	});

	it("says NO while the answer is still unknown", async () => {
		// A probe that never answers.
		getCapabilities.mockImplementation(() => new Promise(() => {}));

		const { result } = renderHook(() => useOracleAvailability(), { wrapper });

		await settle(1);

		expect(result.current.oracleAvailable).toBe(false);
		expect(result.current.loaded).toBe(false);
	});

	it("carries no oracle confidence it did not get from the server", async () => {
		const { result } = renderHook(() => useOracleAvailability(), { wrapper });

		await settle(1);

		expect(result.current.defaultConfidence).toBe(95);

		// The server stops declaring a threshold.
		serverSays.oracleDefaultConfidence = 0;
		await settle(REFRESH_MS);
		await settle(1);

		expect(result.current.defaultConfidence).toBeNull();
	});
});
