/**
 * Formatting for the admin "Platform Health" metrics.
 *
 * The API returns `null` for any metric the platform could not measure.
 * `null` must never be dressed up as a number: not as `0`, not as a `--`
 * that looks like a reading, and not as a stale figure. These helpers turn a
 * missing measurement into an explicit, visible "Not measured", and they keep
 * the colour of a metric honest too -- an unmeasured metric gets no
 * green/red verdict, because a verdict on a metric nobody measured is the
 * same class of lie as the number itself.
 */

export const NOT_MEASURED_LABEL = "Not measured";

export type MetricTone = "good" | "warn" | "bad" | "unknown";

export interface MetricFormatOptions {
	/** Decimal places to show. Defaults to 0. */
	digits?: number;
	/** Appended to the formatted number, e.g. " ms" or "%". */
	suffix?: string;
	/** Render with thousands separators, rounding to a whole number. */
	groupThousands?: boolean;
}

export function isMeasured(
	value: number | null | undefined,
): value is number {
	return typeof value === "number" && Number.isFinite(value);
}

/**
 * Format a measured metric, or say plainly that it was not measured.
 */
export function formatMetric(
	value: number | null | undefined,
	options: MetricFormatOptions = {},
): string {
	if (!isMeasured(value)) return NOT_MEASURED_LABEL;
	const { digits = 0, suffix = "", groupThousands = false } = options;
	const text = groupThousands
		? Math.round(value).toLocaleString()
		: value.toFixed(digits);
	return `${text}${suffix}`;
}

/**
 * The verdict colour for a metric. "unknown" is deliberately not a verdict:
 * an unmeasured metric must not be shown as healthy.
 */
export function metricTone(
	value: number | null | undefined,
	rules: { good: (v: number) => boolean; warn: (v: number) => boolean },
): MetricTone {
	if (!isMeasured(value)) return "unknown";
	if (rules.good(value)) return "good";
	if (rules.warn(value)) return "warn";
	return "bad";
}

/** Uptime thresholds: >= 99% good, >= 95% warn, below that bad. */
export function uptimeTone(value: number | null | undefined): MetricTone {
	return metricTone(value, {
		good: (v) => v >= 99,
		warn: (v) => v >= 95,
	});
}

/** Error-rate thresholds: < 1% good, < 5% warn, at or above that bad. */
export function errorRateTone(value: number | null | undefined): MetricTone {
	return metricTone(value, {
		good: (v) => v < 1,
		warn: (v) => v < 5,
	});
}

export const METRIC_TONE_CLASS: Record<MetricTone, string> = {
	good: "text-emerald-600",
	warn: "text-amber-600",
	bad: "text-red-600",
	unknown: "text-muted-foreground",
};
