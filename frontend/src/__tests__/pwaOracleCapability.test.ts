// src/__tests__/pwaOracleCapability.test.ts
// src/__tests__/pwaOracleCapability.test.ts
//
// THE PWA'S ORACLE DECISION, TESTED.
//
// The customer-facing PWA has NO test runner of its own: `pwa/package.json`
// declares no `test` script, no vitest/jest dependency, and no vitest or
// jest config exists under `pwa/`. `npm --prefix pwa test` therefore fails
// with "Missing script: test" -- there is nothing to run.
//
// The honest alternative used here: the PWA's oracle decision is factored
// into a DEPENDENCY-FREE module (`pwa/lib/oracleCapability.ts`), and that
// module is exercised by the repository's only frontend runner (vitest,
// `frontend/vitest.config.ts`). The import crosses the project boundary on
// PURPOSE and only in this test: the product code of the frontend does not
// depend on the PWA.
//
// This is a real behavioural test of the PWA's decision function -- not a
// grep of its source text. The limitation, stated plainly: it does not
// render the PWA's React components, so the RENDERED output of
// `LaunchStrategyModal.tsx` is covered by the source-level assertions in
// `tests/test_oracle_availability.py` (server side), not by a DOM test.

import { describe, expect, it } from "vitest";

// Cross-project import: the PWA's pure decision module.
import {
	NO_ORACLE_SETTINGS,
	buildOracleLaunchSettings,
	effectiveOracleConfidence,
	oracleDefaultConfidencePercent,
	oracleIsUsable,
	oracleLaunchRefusal,
	parseOracleRegime,
	savedDynamicMode,
} from "../../../pwa/lib/oracleCapability";

describe("the PWA's oracle decision", () => {
	it("says the oracle is usable only when the server says a model is loaded", () => {
		expect(oracleIsUsable({ oracleAvailable: true })).toBe(true);
		expect(oracleIsUsable({ oracleAvailable: false })).toBe(false);
	});

	it("fails CLOSED when the server's answer is missing or unknown", () => {
		// A probe that could not be completed must not turn the oracle back
		// on. This is the case that decides whether a customer is shown an
		// oracle regime and a 95% confidence that no model produced.
		expect(oracleIsUsable(null)).toBe(false);
		expect(oracleIsUsable(undefined)).toBe(false);
		expect(oracleIsUsable({} as { oracleAvailable: boolean })).toBe(false);
	});

	it("carries no oracle numbers when there is no oracle", () => {
		expect(NO_ORACLE_SETTINGS.oracleRegime).toBeNull();
		expect(NO_ORACLE_SETTINGS.oracleConfidence).toBeNull();
	});
});

describe("the PWA's oracle THRESHOLD comes from the server", () => {
	it("reads the server's declared threshold", () => {
		expect(
			oracleDefaultConfidencePercent({
				oracleAvailable: true,
				oracleDefaultConfidence: 95,
			}),
		).toBe(95);
		expect(
			oracleDefaultConfidencePercent({ oracleAvailable: true }),
		).toBeNull();
	});

	it("shows the customer's own choice when they made one", () => {
		expect(effectiveOracleConfidence(60, 95)).toBe(60);
	});

	it("falls back to the declared threshold when none was chosen", () => {
		// 0 is "not chosen" on the server too. Showing 0% while the filter
		// runs at 95% is the lie this removes.
		expect(effectiveOracleConfidence(undefined, 95)).toBe(95);
		expect(effectiveOracleConfidence(0, 95)).toBe(95);
		expect(effectiveOracleConfidence(undefined, null)).toBeNull();
	});

	it("never invents a regime", () => {
		expect(parseOracleRegime(undefined)).toBeNull();
		expect(parseOracleRegime("")).toBeNull();
		expect(parseOracleRegime("1")).toBe(1);
		expect(parseOracleRegime("9")).toBeNull();
	});

	it("builds a launch payload with no invented numbers", () => {
		expect(
			buildOracleLaunchSettings({
				oracleAvailable: false,
				regime: "1",
				confidence: 95,
				defaultConfidence: 95,
			}),
		).toEqual({ regime: null, confidence: null });

		expect(
			buildOracleLaunchSettings({
				oracleAvailable: true,
				regime: "2",
				confidence: 70,
				defaultConfidence: 95,
			}),
		).toEqual({ regime: 2, confidence: 70 });
	});
});

describe("a saved oracle strategy is not relaunched under another mode", () => {
	it("refuses the launch when the oracle is gone", () => {
		expect(oracleLaunchRefusal("DYNAMIC_ORACLE", false)).toBe(
			"ORACLE_UNAVAILABLE",
		);
		expect(oracleLaunchRefusal("DYNAMIC_ORACLE", true)).toBeNull();
		expect(oracleLaunchRefusal("DYNAMIC_NATR", false)).toBeNull();
	});

	it("recognises the mode a strategy was saved with", () => {
		expect(savedDynamicMode({ oracle_settings: { regime: 1 } })).toBe(
			"DYNAMIC_ORACLE",
		);
		expect(savedDynamicMode({ natr_settings: { min_natr: 1.5 } })).toBe(
			"DYNAMIC_NATR",
		);
		expect(savedDynamicMode({})).toBeNull();
		expect(savedDynamicMode(null)).toBeNull();
	});
});
