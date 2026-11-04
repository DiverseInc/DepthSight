// src/__tests__/formatSystemMetric.test.ts
//
// The admin "Platform Health" card must never show a number nobody
// measured. The API returns `null` for any metric it could not measure, and
// the client has to render that as an explicit "Not measured" -- not as 0,
// not as a "--" that looks like a reading, and not with a green verdict.

import { describe, expect, it } from "vitest";
import {
	METRIC_TONE_CLASS,
	NOT_MEASURED_LABEL,
	errorRateTone,
	formatMetric,
	isMeasured,
	uptimeTone,
} from "../lib/formatSystemMetric";

describe("formatMetric", () => {
	it("says plainly that an unmeasured metric was not measured", () => {
		expect(formatMetric(null, { digits: 2, suffix: "%" })).toBe(
			NOT_MEASURED_LABEL,
		);
		expect(formatMetric(undefined, { digits: 2, suffix: "%" })).toBe(
			NOT_MEASURED_LABEL,
		);
	});

	it("never renders an unmeasured metric as zero", () => {
		// The bug this guards: a null turned into "0.00%" reads as a
		// perfect score rather than as a missing measurement.
		expect(formatMetric(null, { digits: 2, suffix: "%" })).not.toBe("0.00%");
		expect(formatMetric(null, { groupThousands: true })).not.toBe("0");
		expect(formatMetric(null)).not.toMatch(/\d/);
	});

	it("renders a measured metric with its suffix", () => {
		expect(formatMetric(22.456, { digits: 2, suffix: " ms" })).toBe("22.46 ms");
		expect(formatMetric(99.981, { digits: 2, suffix: "%" })).toBe("99.98%");
		expect(formatMetric(1234567, { groupThousands: true })).toBe("1,234,567");
	});

	it("treats a non-finite number as unmeasured", () => {
		expect(formatMetric(NaN, { digits: 2 })).toBe(NOT_MEASURED_LABEL);
		expect(formatMetric(Infinity, { digits: 2 })).toBe(NOT_MEASURED_LABEL);
	});
});

describe("isMeasured", () => {
	it("accepts only a finite number", () => {
		expect(isMeasured(0)).toBe(true);
		expect(isMeasured(null)).toBe(false);
		expect(isMeasured(undefined)).toBe(false);
		expect(isMeasured(NaN)).toBe(false);
	});
});

describe("metric tone", () => {
	it("gives an unmeasured metric no verdict at all", () => {
		// Not "good": an unmeasured metric must never be shown as healthy.
		expect(uptimeTone(null)).toBe("unknown");
		expect(errorRateTone(null)).toBe("unknown");
		expect(METRIC_TONE_CLASS.unknown).toBe("text-muted-foreground");
	});

	it("keeps the existing uptime thresholds", () => {
		expect(uptimeTone(99.99)).toBe("good");
		expect(uptimeTone(95)).toBe("warn");
		expect(uptimeTone(12.5)).toBe("bad");
	});

	it("keeps the existing error-rate thresholds", () => {
		expect(errorRateTone(0.02)).toBe("good");
		expect(errorRateTone(4.9)).toBe("warn");
		expect(errorRateTone(5)).toBe("bad");
	});
});
