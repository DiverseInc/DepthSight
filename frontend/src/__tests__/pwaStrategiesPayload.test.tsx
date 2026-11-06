// @vitest-environment jsdom
// src/__tests__/pwaStrategiesPayload.test.tsx
//
// THE PAYLOAD THE SERVER RECEIVES MUST NOT CARRY AN INVENTED NUMBER.
//
// The PWA's Strategies screen is the place where the launch payload is
// actually built, and it is the LAST place a fabricated oracle number can
// be put back:
//
//     params.oracle_settings =
//         settings.regime === null
//             ? null
//             : { regime: settings.regime, confidence: settings.confidence };
//     params.oracle_regime = settings.regime;
//
// `settings` comes from the shared decision module, which decides the
// numbers honestly. The screen then copies them. A fallback added to
// either line -- `confidence: settings.confidence || 95`,
// `params.oracle_regime = settings.regime ?? 1` -- puts the fiction back
// exactly where it was removed from, and nothing else in the product
// notices.
//
// This is a RENDERED test of the SCREEN, and it asserts on the PAYLOAD
// THE SERVER RECEIVES, not on the source text.
//
// The launch modal is STUBBED here on purpose: the code under test is the
// screen's payload builder, and stubbing the modal lets the test state
// exactly what the customer chose (a regime, and no confidence) instead of
// driving the modal's whole form.
//
// The PWA has no test runner of its own, so -- like `pwaOracleLaunch
// .test.tsx` -- this test lives in the repository's only front-end runner
// and imports the PWA component across the project boundary. That is a
// test-only dependency; the frontend's product code does not depend on the
// PWA.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { FC } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import PwaStrategiesScreen from "../../../pwa/screens/StrategiesScreen";

/**
 * The PWA is a SEPARATE application with its OWN React (19) and its own
 * `@types/react`; this project is on React 18.
 *
 * At RUNTIME there is one React: `frontend/vitest.config.ts` sets
 * `dedupe: ["react", "react-dom", "react-i18next"]`, so the PWA screen
 * rendered here runs on THIS project's React.
 *
 * At TYPE level the two do not meet, so the screen is re-typed AT THE
 * BOUNDARY, with its props spelled out. The cast is a TYPE-level
 * re-typing only: it does not change the function object the test
 * renders, and it does not hide a behaviour difference.
 */
const StrategiesScreenUnderTest =
	PwaStrategiesScreen as unknown as FC<{
		onInitiateBacktest: (strategy: unknown) => void;
		onEditStrategy: (strategy: unknown) => void;
	}>;

/**
 * `t` MUST be a STABLE function.
 *
 * The screen's data-loading effect depends on a `useCallback` that depends
 * on `t`. A mock that returns a NEW `t` on every render makes that effect
 * re-run forever: the screen re-fetches, re-renders, and replaces the DOM
 * node the test is about to click -- so the click lands on a detached node
 * and nothing happens. `vi.hoisted` gives the function a stable identity
 * that also exists before the mock factories run.
 */
const { translate, Icon } = vi.hoisted(() => ({
	translate: (key: string) => key.split(".").pop(),
	Icon: () => null,
}));

/** What the server says. Flipped by the tests. */
const serverSays = {
	oracleAvailable: true,
	defaultConfidence: null as number | null,
};

const startStrategy = vi.fn();

/** A strategy the customer saved, with the Oracle filter on it. */
const savedStrategy = {
	id: "strategy-1",
	name: "Oracle strategy",
	symbols: ["BTCUSDT"],
	symbol_selection_mode: "DYNAMIC",
	config_data: {
		oracle_settings: { regime: 2, confidence: 60 },
	},
};

/**
 * What the customer chose: a REGIME, and NO confidence -- the server
 * declared no default threshold either, so there is no number to send.
 */
const customerChose = {
	symbolSelectionMode: "DYNAMIC",
	dynamicMode: "DYNAMIC_ORACLE",
	oracleRegime: 2,
	oracleConfidence: undefined,
	maxConcurrentSymbols: 5,
	minNatr: 0.5,
	mode: "paper",
	symbols: "",
	useMlConfirmation: false,
	breakevenOnRegimeChange: false,
};

vi.mock("react-i18next", () => ({
	useTranslation: () => ({ t: translate }),
}));

// The PWA has its OWN node_modules, so `react-swipeable` resolves to the
// PWA's copy -- which imports the PWA's React (19) and would break the
// hooks. It is mocked by its RESOLVED path, so the mock and the screen's
// import are the same module.
vi.mock("../../../pwa/node_modules/react-swipeable", () => ({
	useSwipeable: () => ({ onSwipedLeft: undefined, onSwipedRight: undefined }),
}));

vi.mock("../../../pwa/services/api", () => ({
	api: {
		getSavedStrategies: async () => [savedStrategy],
		getRunningStrategies: async () => [],
		startStrategy: (...args: unknown[]) => startStrategy(...args),
		stopStrategy: vi.fn(),
		deleteStrategyConfig: vi.fn(),
	},
}));

vi.mock("../../../pwa/lib/useOracleAvailability", () => ({
	useOracleAvailability: () => ({
		oracleAvailable: serverSays.oracleAvailable,
		loaded: true,
		defaultConfidence: serverSays.defaultConfidence,
	}),
}));

// The modal is STUBBED. The code under test is the screen's payload.
vi.mock("../../../pwa/components/LaunchStrategyModal", () => ({
	default: ({
		isOpen,
		onSubmit,
	}: {
		isOpen: boolean;
		onSubmit: (details: unknown) => void;
	}) =>
		isOpen ? (
			<button type="button" onClick={() => onSubmit(customerChose)}>
				launch
			</button>
		) : null,
}));

// The PWA's ICONS and Logo are React elements built by the PWA's OWN
// React (19). This project renders with React 18, which cannot render an
// element created by the other major version. They are decoration: they
// carry no behaviour, so they are stubbed out. The component identities
// are STABLE, so a re-render does not replace the DOM around them.
vi.mock("../../../pwa/constants", () => ({
	ICONS: {
		Play: Icon,
		History: Icon,
		Edit: Icon,
		Trash: Icon,
		Stop: Icon,
		Delete: Icon,
	},
}));

vi.mock("../../../pwa/components/ui/logo", () => ({
	Logo: Icon,
}));

let alertSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
	startStrategy.mockClear();
	serverSays.oracleAvailable = true;
	serverSays.defaultConfidence = null;
	alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(() => {
	cleanup();
	alertSpy.mockRestore();
});

/** Open the saved strategy and confirm the launch. */
async function launchSavedStrategy() {
	render(
		<StrategiesScreenUnderTest
			onInitiateBacktest={() => {}}
			onEditStrategy={() => {}}
		/>,
	);
	// The screen opens on the RUNNING tab; the saved strategy is on the
	// other one. The tab is a BUTTON whose name starts with "stopped";
	// the strategy's own status badge is a span, so it cannot collide.
	fireEvent.click(screen.getByRole("button", { name: /stopped/ }));
	fireEvent.click(await screen.findByRole("button", { name: "run" }));
	fireEvent.click(await screen.findByRole("button", { name: "launch" }));
	await waitFor(() => expect(startStrategy).toHaveBeenCalled());
	return startStrategy.mock.calls[0] as unknown[];
}

describe("the PWA's launch payload", () => {
	it("carries the regime the customer chose and NO confidence they did not", async () => {
		const params = (await launchSavedStrategy())[4] as Record<string, unknown>;

		// The regime is the customer's own.
		expect(params.oracle_regime).toBe(2);
		expect(params.oracle_settings).toEqual({ regime: 2, confidence: null });

		// And there is no 95 anywhere: the server declared no default, and
		// the customer chose none. A 95 here would be a number no model
		// ever produced.
		expect(params.oracle_settings).not.toMatchObject({ confidence: 95 });
		expect(JSON.stringify(params)).not.toContain("95");
	});

	it("carries NO oracle numbers at all when the server has no oracle", async () => {
		serverSays.oracleAvailable = false;

		const params = (await launchSavedStrategy())[4] as Record<string, unknown>;

		expect(params.oracle_regime).toBeNull();
		expect(params.oracle_settings).toBeNull();
		// 0 means "not chosen" -- the server reads it as such and falls
		// back to its DECLARED threshold. It is not a regime, and it is
		// not a 95.
		expect(params.oracle_confidence).toBe(0);
		expect(JSON.stringify(params)).not.toContain("95");
	});
});
