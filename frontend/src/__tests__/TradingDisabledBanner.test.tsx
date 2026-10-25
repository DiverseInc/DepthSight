// src/__tests__/TradingDisabledBanner.test.tsx
//
// FIX 2 -- when the backend blocks trading it already says WHY
// (`trading_disabled_reason`). The frontend used to throw that away, so a
// client whose bot hit the daily loss limit saw a normal-looking dashboard
// with no explanation at all.

import { beforeAll, describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import indexEn from "@/locales/en/index.json";
import commonEn from "@/locales/en/common.json";
import { TradingDisabledBanner } from "@/components/dashboard/TradingDisabledBanner";

beforeAll(async () => {
	// The real English copy, so the test asserts what a client actually sees.
	await i18n.use(initReactI18next).init({
		resources: { en: { index: indexEn, common: commonEn } },
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

describe("TradingDisabledBanner (FIX 2)", () => {
	it("shows the reason the backend gave when trading is blocked", () => {
		const html = renderToStaticMarkup(
			<TradingDisabledBanner
				isTradingAllowed={false}
				reason="DailyLoss: -120.00 USD <= -50.00 USD"
			/>,
		);
		expect(html).toContain("DailyLoss: -120.00 USD &lt;= -50.00 USD");
		// Same red badge the rest of the risk family uses.
		expect(html).toContain("cannot_trade");
	});

	it("renders nothing when trading is allowed", () => {
		const html = renderToStaticMarkup(
			<TradingDisabledBanner
				isTradingAllowed={true}
				reason="this reason should not be shown"
			/>,
		);
		expect(html).toBe("");
	});

	it("renders nothing when the trading state is unknown", () => {
		const html = renderToStaticMarkup(<TradingDisabledBanner />);
		expect(html).toBe("");
	});

	it("still explains itself when the backend sent no reason", () => {
		const html = renderToStaticMarkup(
			<TradingDisabledBanner isTradingAllowed={false} reason={null} />,
		);
		expect(html).toContain("cannot_trade");
		expect(html.length).toBeGreaterThan(0);
	});
});
