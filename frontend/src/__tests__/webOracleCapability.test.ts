// src/__tests__/webOracleCapability.test.ts
//
// THE WEB CLIENT MUST NOT FABRICATE AN ORACLE -- AND IT MUST AGREE
// WITH THE PWA.
//
// The primary web client carried the same fiction the PWA carried:
//
//   const canUseOracle = isAdmin || isPro;   // Oracle is available to
//                                            // admins and Pro users
//   ...?.toString() || "1"                    // a regime nobody measured
//   (…oracle_settings?.confidence as number) || 95
//   defaultValue={[field.value || 95]}       // a confidence nobody measured
//
// and SENT those numbers to the server:
//
//   const regime = parseInt(formData.oracleRegime || "1", 10);
//   const confidence = formData.oracleConfidence || 95;
//
// on a deployment whose api container logs, once per worker:
//
//   WARNING [api.simulation_router:122] - Oracle model not found at
//   /app/data/oracle_model.joblib
//
// The decision logic now lives in a dependency-free module per app
// (`frontend/src/lib/oracleCapability.ts` and
// `pwa/lib/oracleCapability.ts`). The two apps cannot import each
// other's product code, so their agreement is PROVED here: the same
// inputs are given to both modules and the answers must be identical.

import { describe, expect, it } from "vitest";

import * as web from "@/lib/oracleCapability";
import * as pwa from "../../../pwa/lib/oracleCapability";

const CAPABILITIES = [
	{ oracleAvailable: true, oracleDefaultConfidence: 95 },
	{ oracleAvailable: false, oracleDefaultConfidence: 95 },
	{ oracleAvailable: true, oracleDefaultConfidence: null },
	{ oracleAvailable: false, oracleDefaultConfidence: null },
];

describe("the web client believes the server about the oracle", () => {
	it("says the oracle is usable only when the server says a model is loaded", () => {
		expect(web.oracleIsUsable({ oracleAvailable: true })).toBe(true);
		expect(web.oracleIsUsable({ oracleAvailable: false })).toBe(false);
	});

	it("fails CLOSED when the server's answer is missing or unknown", () => {
		expect(web.oracleIsUsable(null)).toBe(false);
		expect(web.oracleIsUsable(undefined)).toBe(false);
		expect(web.oracleIsUsable({} as { oracleAvailable: boolean })).toBe(false);
	});

	it("uses the threshold the SERVER declared, never one it made up", () => {
		const capability = { oracleAvailable: true, oracleDefaultConfidence: 95 };
		expect(web.oracleDefaultConfidencePercent(capability)).toBe(95);
		// No declaration -> no threshold. The client sends nothing.
		expect(
			web.oracleDefaultConfidencePercent({ oracleAvailable: true }),
		).toBeNull();
		expect(
			web.effectiveOracleConfidence(undefined, null),
		).toBeNull();
	});

	it("shows the customer's own choice when they made one", () => {
		expect(web.effectiveOracleConfidence(60, 95)).toBe(60);
		expect(web.effectiveOracleConfidence(95, 95)).toBe(95);
	});

	it("falls back to the server's declared threshold when none was chosen", () => {
		// The customer has not chosen one: the number on screen must be the
		// number the server will apply.
		expect(web.effectiveOracleConfidence(undefined, 95)).toBe(95);
		// 0 is "not chosen" on the server too, so the client must not
		// show 0% while the filter runs at 95%.
		expect(web.effectiveOracleConfidence(0, 95)).toBe(95);
	});

	it("never invents a regime", () => {
		expect(web.parseOracleRegime(undefined)).toBeNull();
		expect(web.parseOracleRegime("")).toBeNull();
		expect(web.parseOracleRegime(null)).toBeNull();
		expect(web.parseOracleRegime("1")).toBe(1);
		expect(web.parseOracleRegime(2)).toBe(2);
		// A regime the server does not accept is not sent.
		expect(web.parseOracleRegime("7")).toBeNull();
	});

	it("sends no oracle numbers at all when there is no oracle", () => {
		const settings = web.buildOracleLaunchSettings({
			oracleAvailable: false,
			regime: "1",
			confidence: 95,
			defaultConfidence: 95,
		});
		expect(settings.regime).toBeNull();
		expect(settings.confidence).toBeNull();
	});

	it("sends the customer's numbers when there is an oracle", () => {
		const settings = web.buildOracleLaunchSettings({
			oracleAvailable: true,
			regime: "2",
			confidence: 70,
			defaultConfidence: 95,
		});
		expect(settings.regime).toBe(2);
		expect(settings.confidence).toBe(70);
	});

	it("refuses to launch a saved oracle strategy when the oracle is gone", () => {
		expect(
			web.oracleLaunchRefusal("DYNAMIC_ORACLE", false),
		).toBe("ORACLE_UNAVAILABLE");
		expect(web.oracleLaunchRefusal("DYNAMIC_ORACLE", true)).toBeNull();
		expect(web.oracleLaunchRefusal("DYNAMIC_NATR", false)).toBeNull();
		expect(web.oracleLaunchRefusal(null, false)).toBeNull();
	});
});

describe("the PWA and the web client agree", () => {
	it("decide the same way about availability", () => {
		for (const capability of CAPABILITIES) {
			expect(web.oracleIsUsable(capability)).toBe(
				pwa.oracleIsUsable(capability),
			);
			expect(web.oracleIsUsable(null)).toBe(pwa.oracleIsUsable(null));
			expect(web.oracleIsUsable(undefined)).toBe(
				pwa.oracleIsUsable(undefined),
			);
		}
	});

	it("read the same declared threshold", () => {
		for (const capability of CAPABILITIES) {
			expect(web.oracleDefaultConfidencePercent(capability)).toBe(
				pwa.oracleDefaultConfidencePercent(capability),
			);
		}
	});

	it("build the same launch payload", () => {
		const inputs = [
			{
				oracleAvailable: true,
				regime: "1",
				confidence: 80,
				defaultConfidence: 95,
			},
			{
				oracleAvailable: true,
				regime: undefined,
				confidence: undefined,
				defaultConfidence: 95,
			},
			{
				oracleAvailable: false,
				regime: "1",
				confidence: 95,
				defaultConfidence: 95,
			},
		];
		for (const input of inputs) {
			expect(web.buildOracleLaunchSettings(input)).toEqual(
				pwa.buildOracleLaunchSettings(input),
			);
		}
	});

	it("refuse the same launches", () => {
		const modes = ["DYNAMIC_ORACLE", "DYNAMIC_NATR", null] as const;
		for (const mode of modes) {
			expect(web.oracleLaunchRefusal(mode, false)).toBe(
				pwa.oracleLaunchRefusal(mode, false),
			);
			expect(web.oracleLaunchRefusal(mode, true)).toBe(
				pwa.oracleLaunchRefusal(mode, true),
			);
		}
	});

	it("read the same saved mode off a strategy", () => {
		const configs = [
			{ natr_settings: { min_natr: 1.5 } },
			{ oracle_settings: { regime: 1, confidence: 95 } },
			{},
			null,
		];
		for (const config of configs) {
			expect(web.savedDynamicMode(config)).toBe(pwa.savedDynamicMode(config));
		}
	});
});
