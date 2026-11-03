// @vitest-environment jsdom
// src/__tests__/strategyRegistryAndRiskLimits.test.tsx
//
// GATE 4, Part C (frontend half): the client must take the contract from the
// SERVER, not from a copy of it.
//
// THE DEFECT
// ==========
// The strategy editor carried a hand-written list of the strategy classes the
// trading bot registers, and a hand-written copy of the server's risk floors.
// Both could only ever be right by luck: a class the bot added later was not
// offered until the copy was updated, and a class the bot removed stayed
// offered.
//
// The risk limits were seeded from hardcoded defaults too, so the editor
// showed a risk per trade of 1% and a drawdown of 10% whatever the ACCOUNT
// was actually running with -- and the account's own `risk_management` block
// (the only place the bot reads its limits from,
// `bot_module/risk_manager.py:179`) was never even looked at.
//
// WHAT THIS TEST PROVES
// =====================
//* the editor's option list is EXACTLY the server's list, in the server's
//  order: there is only one list now;
//* the risk limits are seeded from the account's live `risk_management`, and
//  what is shown is what is sent;
//* the two limits the server's floor also covers -- the stop distance and the
//  consecutive losses -- are part of the form;
//* a pick the server does not offer blocks the deploy instead of only
//  warning about it.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
	cleanup,
	fireEvent,
	render,
	screen,
	waitFor,
} from "@testing-library/react";
import i18n from "i18next";
import type React from "react";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import commonEn from "@/locales/en/common.json";
import strategyEditorEn from "@/locales/en/strategy-editor.json";

// --- API mocks --------------------------------------------------------------
const saveStrategyConfigMock = vi.fn();
const updateStrategyConfigMock = vi.fn();
const startStrategyMock = vi.fn();
const runBacktestMock = vi.fn();

// The account the bot is actually running for: its `risk_management` block
// holds the limits that are in force.
const { configMock, savedConfigsMock, registryMock } = vi.hoisted(() => ({
	configMock: {
		apiKeys: [{ id: 1, isActive: true, status: "valid" }],
		riskManagement: {
			maxDrawdown: 5,
			riskPerTradePercent: 0.8,
			dailyMaxLossPercent: 3,
			maxStopDistancePct: 4,
			maxConsecutiveLosses: 7,
			stopLossEnabled: true,
			defaultStopLossPercent: 2,
			maxConcurrentTrades: 3,
		},
	},
	savedConfigsMock: [] as unknown[],
	// The server's registry, in the server's order.
	registryMock: {
		strategy_pick: {
			field: "config_data.strategy_name",
			type: "string",
			required: true,
			available: [
				"AggTradeReversal",
				"ConsolidationImpulse",
				"DensityBounce",
				"FakeBreakout",
				"FirstPullbacksInTrend",
				"GeneticStrategy",
				"ReverseFakeBreakout",
				"ReverseVolumeBreakout",
				"VisualBuilderStrategy",
				"VolumeBreakout",
			],
		},
		strategy_params: {
			field: "config_data.strategy_params",
			type: "object",
			required: false,
		},
		risk_settings: {
			field: "config_data.risk_settings",
			type: "object",
			required: false,
			fields: {},
		},
		symbol_selection_mode: {
			field: "config_data.symbol_selection_mode",
			type: "string",
			required: false,
			allowed: ["manual", "auto", "all"],
		},
		symbols: {
			field: "config_data.symbols",
			type: "array<string>",
			required: "when symbol_selection_mode is 'manual'",
		},
		mode: {
			field: "config_data.mode",
			type: "string",
			required: false,
			allowed: ["paper", "live"],
		},
		server_floor: {
			risk_per_trade_pct: 1,
			max_drawdown_pct: 20,
			daily_loss_limit_pct: 5,
			position_size_pct: 1,
			max_stop_distance_pct: 5,
			max_consecutive_losses: 10,
		},
		rules: [],
	},
}));

vi.mock("@/lib/api", () => ({
	fetchSymbolSelectionSettings: vi.fn().mockResolvedValue({}),
	updateSymbolSelectionSettings: vi.fn().mockResolvedValue({}),
	useConfig: () => ({ data: configMock }),
	useStrategyRegistry: () => ({ data: registryMock }),
	useStrategyConfigsList: () => ({ data: savedConfigsMock }),
	useRunBacktest: () => ({ mutate: runBacktestMock, isPending: false }),
	useSaveStrategyConfig: () => ({
		mutate: saveStrategyConfigMock,
		isPending: false,
	}),
	useStartStrategy: () => ({ mutate: startStrategyMock, isPending: false }),
	useUpdateStrategyConfig: () => ({
		mutate: updateStrategyConfigMock,
		isPending: false,
	}),
	useSendTradingViewTestSignal: () => ({ mutate: vi.fn() }),
	useTradingViewWebhookInfo: () => ({ data: null }),
	useTradingViewWebhookStatus: () => ({ data: null, refetch: vi.fn() }),
	useBlockRestrictions: () => ({ data: { proOnly: [], klineOnly: [] } }),
	useGetAvailableSymbols: () => ({ data: [] }),
}));

vi.mock("@/context/AuthContext", () => ({
	useAuth: () => ({
		user: { id: 1, username: "tester", email: "t@example.com", plan: "pro" },
		isLoading: false,
	}),
}));

import { ConfigAndLaunchPanel } from "@/components/strategy-editor/ConfigAndLaunchPanel";
import { useStrategyEditorStore } from "@/stores/strategyEditorStore";

const wrapper = ({ children }: { children: React.ReactNode }) => (
	<QueryClientProvider client={new QueryClient()}>
		<MemoryRouter>{children}</MemoryRouter>
	</QueryClientProvider>
);

const renderPanel = () =>
	render(<ConfigAndLaunchPanel isSaving={false} />, { wrapper });

/** The payload the save mutation received. */
const savedPayload = () => {
	const call = saveStrategyConfigMock.mock.calls[0];
	if (!call) throw new Error("the save mutation was never called");
	return call[0] as {
		name: string;
		config_data: {
			strategy_name: string;
			risk_settings?: Record<string, number>;
		};
	};
};

const valueOf = (label: string) =>
	(screen.getByLabelText(label) as HTMLInputElement).value;

const clickDeploy = () => {
	fireEvent.click(screen.getByRole("button", { name: /deploy to live/i }));
};

beforeAll(async () => {
	await i18n.use(initReactI18next).init({
		resources: {
			en: { common: commonEn, "strategy-editor": strategyEditorEn },
		},
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

beforeEach(() => {
	saveStrategyConfigMock.mockReset();
	updateStrategyConfigMock.mockReset();
	startStrategyMock.mockReset();
	runBacktestMock.mockReset();
	useStrategyEditorStore.setState({ name: "Gate 4 Strategy", symbol: "BTCUSDT" });
});

afterEach(() => {
	cleanup();
});

describe("the strategy class list comes from the server (GATE 4)", () => {
	it("offers EXACTLY the classes the server published, in the server's order", () => {
		renderPanel();

		const select = screen.getByLabelText("Strategy Class") as HTMLSelectElement;
		const options = Array.from(select.options).map((option) => option.value);

		expect(options).toEqual(registryMock.strategy_pick.available);
	});

	it("refuses to deploy a class the server does not offer", async () => {
		renderPanel();

		const select = screen.getByLabelText("Strategy Class") as HTMLSelectElement;
		// A class the server removed: the client used to only warn about it.
		fireEvent.change(select, { target: { value: "RemovedStrategy" } });

		clickDeploy();

		await waitFor(() => expect(saveStrategyConfigMock).not.toHaveBeenCalled());
		expect(saveStrategyConfigMock).not.toHaveBeenCalled();
	});
});

describe("the risk limits come from the account (GATE 4)", () => {
	it("shows the account's live limits, not hardcoded defaults", () => {
		renderPanel();

		// The account runs a 5% drawdown limit. Before the fix the editor
		// showed its own hardcoded 10%.
		expect(valueOf("Max Drawdown (%)")).toBe("5");
		expect(valueOf("Risk Per Trade (%)")).toBe("0.8");
		expect(valueOf("Daily Loss Limit (%)")).toBe("3");
	});

	it("sends the account's live limits to the server", async () => {
		renderPanel();

		clickDeploy();

		await waitFor(() => expect(saveStrategyConfigMock).toHaveBeenCalled());

		expect(savedPayload().config_data.risk_settings).toMatchObject({
			max_drawdown_pct: 5,
			risk_per_trade_pct: 0.8,
			daily_loss_limit_pct: 3,
		});
	});

	it("offers the two limits the server's floor also covers", () => {
		renderPanel();

		expect(valueOf("Max Stop Distance (%)")).toBe("4");
		expect(valueOf("Max Consecutive Losses")).toBe("7");
	});

	it("sends the two extra limits to the server", async () => {
		renderPanel();

		clickDeploy();

		await waitFor(() => expect(saveStrategyConfigMock).toHaveBeenCalled());

		expect(savedPayload().config_data.risk_settings).toMatchObject({
			max_stop_distance_pct: 4,
			max_consecutive_losses: 7,
		});
	});
});
