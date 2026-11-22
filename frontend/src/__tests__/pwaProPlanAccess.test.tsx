// @vitest-environment jsdom
// src/__tests__/pwaProPlanAccess.test.tsx
//
// THE DEFECT THIS PINS
// =====================
// The $499 "Managed" tier sits ABOVE Professional ("pro"). The SERVER
// already grants it everything Professional has: the `managed:` block in
// api/plans_config.yml lists the same permissions as the `pro:` block, with
// larger quotas. The PWA asked a DIFFERENT question.
//
// `pwa/types.ts` decided "may this customer use the PRO blocks?" with
//
//     plan === "pro" || plan === "institutional"
//
// -- a question that was only correct while "pro" was the top tier. A
// customer who paid $499 was shown the padlock on every PRO block in the
// strategy editor and told to upgrade to a tier they already owned.
//
// THE LEGACY KEY IS NOT OPTIONAL
// ==============================
// `premium` is the legacy spelling of the Managed tier. The server resolves
// it at READ time (api/plans.py `resolve_plan_key`), but the rows are NOT
// migrated -- it shipped as an alias, not as a data migration -- and the
// client receives the RAW key: `/users/me` returns the ORM user unchanged
// (api/depthsight_api.py:1874) and `User.plan` is a plain string. So
// "managed" and "premium" are BOTH keys the PWA really receives, and a fix
// that only knew "managed" would still lock a legacy customer out.
//
// THESE TESTS ARE BEHAVIOURAL
// ===========================
// They mount the REAL PWA components and watch what the CUSTOMER SEES:
// whether a PRO block carries the padlock, whether clicking it produces the
// upgrade prompt, and whether the precision (Kline) backtest engine is
// offered. They are not greps of a helper's source text.
//
// The PWA has no test runner of its own, so -- like the other PWA tests --
// this test lives in the repository's only front-end runner and imports the
// PWA code across the project boundary. That is a test-only dependency; the
// product code of the frontend does not depend on the PWA.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type React from "react";
import {
	afterAll,
	afterEach,
	beforeEach,
	describe,
	expect,
	it,
	vi,
} from "vitest";

// The signed-in customer. The plan is swapped per test.
const authMock = vi.hoisted(() => ({
	user: {
		plan: "free",
		role: "user",
	} as { plan: string; role: string },
}));

vi.mock("../../../pwa/contexts/AuthContext", () => ({
	useAuth: () => authMock,
}));

// The PWA asks the server which blocks are PRO-only. The answer is
// deliberately EMPTY here, so the component falls back to the PRO_BLOCKS
// list that ships in the PWA's own store -- the list the customer is
// actually shown in the palette.
vi.mock("../../../pwa/services/api", () => ({
	api: {
		getBlockRestrictions: vi.fn().mockResolvedValue({
			proOnly: undefined,
			klineOnly: undefined,
		}),
	},
}));

// The PWA's i18n module turns on debug logging in a dev build
// (`debug: import.meta.env.DEV`) and its catalogue has no entry for the
// inline English defaults these two components fall back to, so it logs
// every missing key. That log is expected here, so it is switched off --
// before the PWA is even imported, which is when the i18n module logs.
// Warnings and errors still print -- they use console.warn / console.error.
vi.hoisted(() => {
	vi.spyOn(console, "log").mockImplementation(() => undefined);
});

afterAll(() => {
	vi.restoreAllMocks();
});

import BacktestModal from "../../../pwa/components/BacktestModal";
import ComponentPaletteModal from "../../../pwa/components/editor/ComponentPaletteModal";

/** The upgrade prompt a customer who may NOT use a PRO block is shown. */
const PRO_BLOCK_LOCKED_ALERT =
	"This block requires a PRO plan subscription. Please upgrade to unlock it.";

/** The upgrade prompt a customer who may NOT use the precision engine is shown. */
const KLINE_LOCKED_ALERT =
	"Precision Engine (Kline) is available for Pro users only. Please upgrade to unlock institutional-grade testing.";

/**
 * The tiers that carry the Professional feature set, and above it.
 *
 * `managed` is the $499 tier and `premium` is its legacy spelling; `pro` is
 * Professional itself and `institutional` has always been accepted.
 */
const PLANS_WITH_ACCESS = [
	{ plan: "managed", who: "a $499 Managed customer" },
	{ plan: "premium", who: "a legacy premium customer" },
	{ plan: "pro", who: "a Professional customer" },
	{ plan: "institutional", who: "an institutional customer" },
];

/**
 * The tiers that must NOT get the Professional feature set, plus a plan key
 * nobody has ever heard of -- an unknown key must behave exactly as it
 * always has.
 */
const PLANS_WITHOUT_ACCESS = [
	{ plan: "free", who: "a free customer" },
	{ plan: "standard", who: "a Standard customer" },
	{ plan: "wizard", who: "a customer on an unknown plan" },
];

const alertMock = vi.fn();

beforeEach(() => {
	authMock.user.plan = "free";
	authMock.user.role = "user";
	alertMock.mockClear();
	// The PWA tells the customer with `alert()`. jsdom has no alert, so the
	// prompt is captured instead of thrown.
	vi.stubGlobal("alert", alertMock);
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

/** The PRO block buttons in the rendered palette (they wear the PRO badge). */
const proBlockButtons = (container: HTMLElement): HTMLElement[] =>
	Array.from(container.querySelectorAll("button")).filter((button) =>
		Array.from(button.querySelectorAll("span")).some(
			(span) => span.textContent === "PRO",
		),
	);

/** True when the block is shown with the padlock over it. */
const showsTheLock = (button: HTMLElement): boolean =>
	button.querySelector(".lucide-lock") !== null;

/**
 * What a customer on `plan` SEES in the strategy editor's block palette.
 *
 * Measured from the rendered component: whether the PRO blocks carry the
 * padlock, and whether clicking one produces the upgrade prompt.
 */
const paletteExperience = (plan: string) => {
	authMock.user.plan = plan;
	const { container, unmount } = render(
		<QueryClientProvider
			client={
				new QueryClient({ defaultOptions: { queries: { retry: false } } })
			}
		>
			<ComponentPaletteModal
				isOpen={true}
				onClose={() => undefined}
				targetSection="filters"
				parentId={null}
			/>
		</QueryClientProvider>,
	);

	const buttons = proBlockButtons(container);
	if (buttons.length === 0) {
		unmount();
		throw new Error("the palette rendered no PRO blocks at all");
	}
	const locked = buttons.map(showsTheLock);

	fireEvent.click(buttons[0]);
	const prompt = (alertMock.mock.calls[0]?.[0] as string | undefined) ?? null;

	unmount();
	return { locked, prompt, proBlockCount: buttons.length };
};

/**
 * What a customer on `plan` SEES when they ask for the precision engine.
 *
 * Measured from the rendered component: whether the upgrade prompt is shown
 * and whether the backtest is allowed to run on the precision engine.
 */
const precisionExperience = async (plan: string) => {
	authMock.user.plan = plan;
	const onSubmit = vi.fn();
	render(
		<BacktestModal
			isOpen={true}
			onClose={() => undefined}
			onSubmit={onSubmit}
			strategy={null}
		/>,
	);

	// The modal fills the symbol and the dates in itself.
	await waitFor(() =>
		expect(screen.getByDisplayValue("BTCUSDT")).toBeTruthy(),
	);
	fireEvent.change(screen.getByLabelText("Backtest Engine"), {
		target: { value: "kline" },
	});
	fireEvent.click(screen.getByRole("button", { name: "Run Backtest" }));

	const prompt = (alertMock.mock.calls[0]?.[0] as string | undefined) ?? null;
	return { prompt, submitted: onSubmit.mock.calls.length > 0 };
};

describe("the PRO blocks in the strategy editor", () => {
	it.each(PLANS_WITH_ACCESS)(
		"gives $who the PRO blocks, unlocked and with no upgrade prompt",
		({ plan }) => {
			const { locked, prompt } = paletteExperience(plan);
			expect(locked).not.toContain(true);
			expect(prompt).toBeNull();
		},
	);

	it.each(PLANS_WITHOUT_ACCESS)(
		"still locks the PRO blocks for $who and tells them to upgrade",
		({ plan }) => {
			const { locked, prompt } = paletteExperience(plan);
			expect(locked).toContain(true);
			expect(prompt).toBe(PRO_BLOCK_LOCKED_ALERT);
		},
	);

	it("reads the plan key the way the server reads it, whatever the case", () => {
		// `resolve_plan_key` strips and lowercases the key, so the server
		// treats "PRO" as "pro". If the client did not agree, the client
		// would lock out a customer the server lets through.
		const { locked, prompt } = paletteExperience("PRO");
		expect(locked).not.toContain(true);
		expect(prompt).toBeNull();
	});
});

describe("the precision (Kline) backtest engine", () => {
	it.each(PLANS_WITH_ACCESS)(
		"lets $who run the precision engine, with no upgrade prompt",
		async ({ plan }) => {
			const { prompt, submitted } = await precisionExperience(plan);
			expect(prompt).toBeNull();
			expect(submitted).toBe(true);
		},
	);

	it.each(PLANS_WITHOUT_ACCESS)(
		"still locks the precision engine for $who",
		async ({ plan }) => {
			const { prompt, submitted } = await precisionExperience(plan);
			expect(prompt).toBe(KLINE_LOCKED_ALERT);
			expect(submitted).toBe(false);
		},
	);
});
