// src/lib/backtestDataWindow.ts
//
// What the customer has to be told about the window a backtest was run over.
//
// The loader records, on every frame it returns, where the candles came
// from and which window they cover.  The backtest turns those candles
// into numbers, so the numbers are what the customer reads -- and a number
// that was computed over a SHORTER window than the one that was asked for
// has to say so.
//
// THE SENTENCE IS BUILT BY THE SCREEN, NOT BY THE BACKEND.  The backend
// sends FACTS (which days are missing, which source the candles came from,
// whether the window was covered) because a screen that speaks two
// languages needs the facts: a sentence that arrives ready-made can only
// be shown in the language it was written in.
//
// The helper is pure on purpose: it is the part that can be tested without
// a browser, and it is the ONLY place that decides what a disclosure says.

import type { BacktestDataWindowRecord } from "@/types/api";

export interface DataWindowDisclosure {
	/** TRUE when at least one frame does not cover the window that was asked for. */
	incomplete: boolean;
	/** The days that are missing from the window, as YYYY-MM-DD. */
	missingDays: string[];
	/** Where the candles came from: "rest_api", "bulk_archive" or "local_cache". */
	source: string | null;
	/** TRUE when the candles are not the ones the REST path would have returned. */
	differsFromRest: boolean;
	/** How many frames the disclosure is about. */
	frameCount: number;
}

/** The calendar day of an ISO timestamp, as YYYY-MM-DD. */
function calendarDay(iso: string | null | undefined): string | null {
	if (!iso || typeof iso !== "string") return null;
	const day = iso.slice(0, 10);
	return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : null;
}

/**
 * Reads the disclosure out of the KPI payload of a backtest.
 *
 * `null` when the payload carries none -- a run made before the backend
 * started recording it.  There is nothing to say then, and nothing is
 * said.
 */
export function readDataWindowDisclosure(
	kpis: { data_window?: BacktestDataWindowRecord[] } | null | undefined,
): DataWindowDisclosure | null {
	const records = kpis?.data_window;
	if (!Array.isArray(records) || records.length === 0) return null;

	const missingDays: string[] = [];
	let incomplete = false;
	let source: string | null = null;
	let differsFromRest = false;

	for (const record of records) {
		if (!record) continue;
		if (record.complete === false) incomplete = true;
		if (!source && record.source) source = record.source;
		if (record.differs_from_rest) differsFromRest = true;
		for (const bucket of record.missing_buckets ?? []) {
			const day = calendarDay(bucket?.start);
			if (day && !missingDays.includes(day)) missingDays.push(day);
		}
	}

	missingDays.sort();

	return {
		incomplete,
		missingDays,
		source,
		differsFromRest,
		frameCount: records.length,
	};
}

/** The i18n key that names a source, or null when the source is unknown. */
export function sourceLabelKey(source: string | null): string | null {
	switch (source) {
		case "rest_api":
			return "sourceRestApi";
		case "bulk_archive":
			return "sourceBulkArchive";
		case "local_cache":
			return "sourceLocalCache";
		default:
			return null;
	}
}
