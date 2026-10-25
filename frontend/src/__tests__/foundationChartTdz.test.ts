// src/__tests__/foundationChartTdz.test.ts
//
// P0-B (Linear DIV-11) -- FoundationChart threw on every render.
//
// `uniqueKlines` is a `const` that was declared ~15 lines BELOW its first
// use, so the first use hit the temporal dead zone:
//   ReferenceError: Cannot access 'uniqueKlines' before initialization
// The chart creation effect therefore never completed.
//
// A DOM render of the chart is not practical in a unit test (it needs a
// real canvas), so this pins the exact invariant that broke: every `const`
// must be declared before it is used.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(
	join(__dirname, "../components/diagnostics/FoundationChart.tsx"),
	"utf8",
);

describe("FoundationChart has no temporal dead zone (P0-B / DIV-11)", () => {
	it("declares uniqueKlines before it is used", () => {
		const declaration = SOURCE.indexOf("const uniqueKlines");
		const usage = SOURCE.indexOf("estimateTickSize(uniqueKlines)");

		expect(declaration).toBeGreaterThan(-1);
		expect(usage).toBeGreaterThan(-1);
		expect(declaration).toBeLessThan(usage);
	});

	it("declares sortedKlines before it is used", () => {
		const declaration = SOURCE.indexOf("const sortedKlines");
		const usage = SOURCE.indexOf("[...klines].sort(");

		expect(declaration).toBeGreaterThan(-1);
		expect(usage).toBeGreaterThan(-1);
		expect(declaration).toBeLessThan(usage);
	});
});
