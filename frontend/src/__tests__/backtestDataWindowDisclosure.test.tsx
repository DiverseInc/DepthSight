// src/__tests__/backtestDataWindowDisclosure.test.tsx
//
// The backtest result must TELL the customer when part of the requested
// candle window was missing.
//
// The backend records, for every frame a backtest loaded, where the
// candles came from and whether the window they cover is the window that
// was asked for -- and which days are missing when it is not.  The KPI
// panel is where the customer reads the numbers, so the panel is where
// the disclosure has to be shown.
//
// What was missing is the SHOWING.  The record reached the API and nobody
// rendered it, so a backtest computed over a shorter window looked
// exactly like a backtest of the window that was asked for.
//
// This file pins the RENDERED output, because that is what a customer
// reads.  The facts themselves are pinned against the source by
// tests/test_backtest_data_window_disclosure.py.

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { BacktestProgressKpiPanel } from "@/components/research/BacktestProgressKpiPanel";
import {
	readDataWindowDisclosure,
	sourceLabelKey,
} from "@/lib/backtestDataWindow";
import commonEn from "@/locales/en/common.json";
import commonRu from "@/locales/ru/common.json";
import researchEn from "@/locales/en/research.json";
import researchRu from "@/locales/ru/research.json";
import type {
	BacktestDataWindowRecord,
	BacktestRunDetailsData,
} from "@/types/api";

void i18n.use(initReactI18next).init({
	lng: "en",
	fallbackLng: "en",
	resources: {
		en: { research: researchEn, common: commonEn },
		ru: { research: researchRu, common: commonRu },
	},
	interpolation: { escapeValue: false },
});

const INCOMPLETE: BacktestDataWindowRecord[] = [
	{
		data_key: "kline_1h",
		source: "bulk_archive",
		complete: false,
		differs_from_rest: true,
		missing_buckets: [
			{ start: "2024-01-10T00:00:00+00:00", end: "2024-01-11T00:00:00+00:00" },
			{ start: "2024-01-17T00:00:00+00:00", end: "2024-01-18T00:00:00+00:00" },
		],
		reason: "the archive is missing 2 day(s) inside the window",
	},
];

const COMPLETE: BacktestDataWindowRecord[] = [
	{
		data_key: "kline_1h",
		source: "rest_api",
		complete: true,
		differs_from_rest: false,
	},
];

function runWith(dataWindow?: BacktestDataWindowRecord[]): BacktestRunDetailsData {
	return {
		id: "run-1",
		task_id: "task-1",
		strategy_name: "VolumeBreakout",
		symbol: "BTCUSDT",
		status: "COMPLETED",
		created_at: "2024-01-31T00:00:00Z",
		start_date: "2024-01-01T00:00:00Z",
		end_date: "2024-01-31T00:00:00Z",
		parameters_json: {},
		kpi_results_json: {
			total_pnl: 12.5,
			sharpe_ratio: 1.2,
			win_rate: 50,
			max_drawdown: 3,
			trades: 4,
			data_window: dataWindow,
		},
		trades: [],
	};
}

describe("the backtest result tells the customer about its data window", () => {
	it("says the window is incomplete and names the days that are missing", () => {
		const markup = renderToStaticMarkup(
			<BacktestProgressKpiPanel run={runWith(INCOMPLETE)} />,
		);

		expect(markup).toContain("Incomplete data window");
		expect(markup).toContain(
			"The candles for this backtest do not cover the whole period you asked for",
		);
		// The days are named, in the words of the customer.
		expect(markup).toMatch(/Missing from the window:[^<]*10[^<]*17/);
		// And where the candles came from.
		expect(markup).toContain("Data source: Binance bulk archive");
		expect(markup).toContain(
			"These candles are not the ones the REST API would have returned.",
		);
	});

	it("says nothing about a window that was complete", () => {
		const markup = renderToStaticMarkup(
			<BacktestProgressKpiPanel run={runWith(COMPLETE)} />,
		);

		expect(markup).not.toContain("Incomplete data window");
		expect(markup).not.toContain("Missing from the window");
		// The source is still said: the customer is told where the candles
		// came from even when nothing was missing.
		expect(markup).toContain("Data source: Binance REST API");
		expect(markup).toContain(
			"The candles cover the whole period you asked for.",
		);
	});

	it("shows no disclosure for a run made before the backend recorded one", () => {
		const markup = renderToStaticMarkup(
			<BacktestProgressKpiPanel run={runWith(undefined)} />,
		);

		expect(markup).not.toContain("Incomplete data window");
		expect(markup).not.toContain("Data source:");
	});
});

describe("the disclosure reads the facts, not a ready-made sentence", () => {
	it("names every missing day, once, in order", () => {
		const disclosure = readDataWindowDisclosure({
			data_window: [
				{
					data_key: "kline_4h",
					source: "bulk_archive",
					complete: false,
					differs_from_rest: true,
					missing_buckets: [
						{ start: "2024-01-17T00:00:00+00:00", end: "2024-01-18T00:00:00+00:00" },
					],
				},
				{
					data_key: "kline_1h",
					source: "bulk_archive",
					complete: false,
					differs_from_rest: true,
					missing_buckets: [
						{ start: "2024-01-10T00:00:00+00:00", end: "2024-01-11T00:00:00+00:00" },
						{ start: "2024-01-10T00:00:00+00:00", end: "2024-01-11T00:00:00+00:00" },
					],
				},
			],
		});

		expect(disclosure?.incomplete).toBe(true);
		expect(disclosure?.missingDays).toEqual(["2024-01-10", "2024-01-17"]);
		expect(disclosure?.source).toBe("bulk_archive");
		expect(disclosure?.differsFromRest).toBe(true);
	});

	it("reports a complete window as complete, and says nothing is missing", () => {
		const disclosure = readDataWindowDisclosure({ data_window: COMPLETE });

		expect(disclosure?.incomplete).toBe(false);
		expect(disclosure?.missingDays).toEqual([]);
		expect(disclosure?.source).toBe("rest_api");
		expect(disclosure?.differsFromRest).toBe(false);
	});

	it("has nothing to say about a run that carries no disclosure", () => {
		expect(readDataWindowDisclosure(undefined)).toBeNull();
		expect(readDataWindowDisclosure(null)).toBeNull();
		expect(readDataWindowDisclosure({ data_window: [] })).toBeNull();
	});
});

describe("the warning is written in every language the app speaks", () => {
	const en = researchEn.dataWindowDisclosure;
	const ru = researchRu.dataWindowDisclosure;

	it("carries the same keys in English and in Russian", () => {
		expect(Object.keys(ru).sort()).toEqual(Object.keys(en).sort());
	});

	it("says the same thing in both languages", () => {
		expect(ru.incompleteTitle).not.toBe(en.incompleteTitle);
		expect(ru.incompleteBody).not.toBe(en.incompleteBody);
		expect(ru.missingDaysLabel).toContain("{{days}}");
		expect(en.missingDaysLabel).toContain("{{days}}");
		expect(ru.sourceLabel).toContain("{{source}}");
		expect(en.sourceLabel).toContain("{{source}}");
		expect(ru.sourceBulkArchive).not.toBe(en.sourceBulkArchive);
	});
});

describe("the source is named in the language the customer reads", () => {
	it("knows the three sources the loader can deliver", () => {
		expect(sourceLabelKey("rest_api")).toBe("sourceRestApi");
		expect(sourceLabelKey("bulk_archive")).toBe("sourceBulkArchive");
		expect(sourceLabelKey("local_cache")).toBe("sourceLocalCache");
		expect(sourceLabelKey("something-else")).toBeNull();
		expect(sourceLabelKey(null)).toBeNull();
	});
});
