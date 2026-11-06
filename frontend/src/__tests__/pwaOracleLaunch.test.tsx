// @vitest-environment jsdom
// src/__tests__/pwaOracleLaunch.test.tsx
//
// A SAVED ORACLE STRATEGY MUST NOT BE RELAUNCHED UNDER ANOTHER MODE.
//
// The PWA's launch modal used to do this:
//
//     dynamicMode: canUseOracle ? dynamicMode : "DYNAMIC_NATR",
//
// so a strategy the customer had saved as DYNAMIC_ORACLE was quietly
// relaunched as a NATR strategy -- a DIFFERENT strategy from the one
// they chose, launched without a word. The customer believed they
// started their oracle strategy; they started a NATR one.
//
// The launch now FAILS LOUDLY: the modal says the oracle is not
// available and the launch does not happen.
//
// This is a RENDERED test: it drives the real component and asserts
// what the customer experiences -- that the launch does not go through
// -- not what the source text says.
//
// The PWA has no test runner of its own, so -- like the existing
// `pwaOracleCapability.test.ts` -- this test lives in the repository's
// only front-end runner and imports the PWA component across the
// project boundary. That is a test-only dependency; the product code
// of the frontend does not depend on the PWA.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { FC } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** What the server says. Flipped by the tests. */
const serverSays = { oracleAvailable: false, oracleDefaultConfidence: 95 };

vi.mock("react-i18next", () => ({
	useTranslation: () => ({
		t: (key: string, fallback?: string) =>
			fallback ?? key.split(".").pop(),
	}),
}));

vi.mock("../../../pwa/contexts/AuthContext", () => ({
	useAuth: () => ({
		user: {
			id: 1,
			username: "tester",
			email: "tester@example.com",
			plan: "pro",
			role: "user",
		},
		isLoading: false,
	}),
}));

vi.mock("../../../pwa/lib/useOracleAvailability", () => ({
	useOracleAvailability: () => ({
		oracleAvailable: serverSays.oracleAvailable,
		loaded: true,
		defaultConfidence: serverSays.oracleDefaultConfidence,
	}),
}));

import PwaLaunchStrategyModal from "../../../pwa/components/LaunchStrategyModal";
import type { DisplayStrategy } from "../../../pwa/types";

/**
 * The PWA is a SEPARATE application with its OWN React (19) and its own
 * `@types/react`; this project is on React 18.
 *
 * At RUNTIME there is one React: `frontend/vitest.config.ts` sets
 * `dedupe: ["react", "react-dom", "react-i18next"]`, so the PWA
 * component rendered here runs on THIS project's React and the test
 * drives the real component.
 *
 * At TYPE level the two do not meet: the PWA component's type is React
 * 19's `FC`, whose `ReactNode` includes `Promise<ReactNode>`, and React
 * 18's JSX element type does not accept it. So the component is re-typed
 * AT THE BOUNDARY, with its props spelled out.
 *
 * The cast is a TYPE-level re-typing only. It does not change the
 * function object the test renders, and it does not hide a behaviour
 * difference -- the assertions below are on what the component DOES.
 */
interface PwaLaunchModalProps {
	isOpen: boolean;
	onClose: () => void;
	onSubmit: (details: unknown) => void;
	strategy: unknown;
}
const LaunchStrategyModal =
	PwaLaunchStrategyModal as unknown as FC<PwaLaunchModalProps>;

/** A strategy the customer SAVED with the Oracle filter. */
// biome-ignore lint/suspicious/noExplicitAny: the PWA's own test seam
const savedOracleStrategy = {
	id: "strategy-1",
	name: "Oracle strategy",
	symbols: ["BTCUSDT"],
	symbol_selection_mode: "DYNAMIC",
	config_data: {
		oracle_settings: { regime: 1, confidence: 95 },
	},
} as unknown as DisplayStrategy;

let onSubmit: ReturnType<typeof vi.fn>;
let alertSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
	onSubmit = vi.fn();
	alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(() => {
	cleanup();
	serverSays.oracleAvailable = false;
	alertSpy.mockRestore();
});

/** Wait until the modal has loaded the strategy's own settings. */
async function waitForStrategyLoaded() {
	await waitFor(() => {
		// The dynamic-settings block (which holds "Max Concurrent
		// Symbols") is rendered only once the symbol-selection mode is
		// DYNAMIC -- that is, once the strategy has been loaded into the
		// form.
		expect(screen.getByText("Max Concurrent Symbols")).toBeTruthy();
	});
}

/** The launch button. The modal's TITLE carries the same label, so the
 * role is what makes the query unambiguous. */
function launchButton(): HTMLElement {
	return screen.getByRole("button", { name: "launchStrategy" });
}

describe("a saved oracle strategy on a server with no oracle model", () => {
	it("refuses the launch instead of relaunching it as a NATR strategy", async () => {
		serverSays.oracleAvailable = false;

		render(
			<LaunchStrategyModal
				isOpen={true}
				onClose={vi.fn()}
				onSubmit={onSubmit}
				strategy={savedOracleStrategy}
			/>,
		);

		await waitForStrategyLoaded();

		fireEvent.click(launchButton());

		expect(onSubmit).not.toHaveBeenCalled();
		expect(alertSpy).toHaveBeenCalledTimes(1);
		expect(String(alertSpy.mock.calls[0][0])).toMatch(/oracle/i);
	});

	it("still launches the customer's own strategy when the oracle is there", async () => {
		serverSays.oracleAvailable = true;

		render(
			<LaunchStrategyModal
				isOpen={true}
				onClose={vi.fn()}
				onSubmit={onSubmit}
				strategy={savedOracleStrategy}
			/>,
		);

		await waitFor(() => {
			const selects = Array.from(
				document.querySelectorAll("select"),
			) as HTMLSelectElement[];
			// The regime the strategy was saved with is on the screen.
			expect(selects.some((s) => s.value === "1")).toBe(true);
		});

		fireEvent.click(launchButton());

		expect(onSubmit).toHaveBeenCalledTimes(1);
		const payload = onSubmit.mock.calls[0][0];
		// The mode the customer saved, NOT a substituted one.
		expect(payload.dynamicMode).toBe("DYNAMIC_ORACLE");
		expect(payload.oracleRegime).toBe("1");
		expect(payload.oracleConfidence).toBe(95);
	});
});
