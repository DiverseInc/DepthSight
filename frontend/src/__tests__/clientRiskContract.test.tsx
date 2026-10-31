// @vitest-environment jsdom
// src/__tests__/clientRiskContract.test.tsx
//
// GATE 4 (frontend half): the risk limits the client OFFERS are the
// server's.
//
// THE DEFECT THIS PINS
// =====================
// `GET /api/v1/strategies/registry` publishes the server's risk floor and the
// editor applies it -- but the numbers the editor STARTED from were a
// hand-written copy of the client's own (`DEFAULT_STRATEGY_RISK_SETTINGS`),
// captured before the server had answered. So a server whose daily-loss floor
// was TIGHTER than that copy got an editor that offered the looser limit, and
// the server refused the save: `_validate_risk_settings` in
// `bot_module/strategy_registry.py` answers "risk_settings.daily_loss_limit_pct
// = 5.0 is looser than the server limit of 2.0".
//
// The floors now come from the server, and the editor starts from the
// server's own numbers.

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

// The account carries NO `risk_management` of its own here, so the editor has
// to fall back to the limits the server publishes. The server's daily-loss
// floor is TIGHTER than the copy the client used to carry (5), which is the
// case that used to ship a limit the server refuses.
const { configMock, savedConfigsMock, registryMock } = vi.hoisted(() => ({
	configMock: {
		apiKeys: [{ id: 1, isActive: true, status: "valid" }],
	},
	savedConfigsMock: [] as unknown[],
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
			type: "string",
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
		// THE SERVER'S FLOOR. The daily-loss floor is 2, not the 5 the client
		// used to carry in its own copy.
		server_floor: {
			risk_per_trade_pct: 1,
			max_drawdown_pct: 20,
			daily_loss_limit_pct: 2,
			position_size_pct: 1,
			max_stop_distance_pct: 5,
			max_consecutive_losses: 10,
		},
		// The per-trade risk has its own floor, in its own unit: a FRACTION
		// of the balance (0.01 is 1%), where `risk_settings` is in PERCENT.
		per_trade_risk: {
			parameter: "risk_pct_per_trade",
			unit: "fraction",
			unit_note: "0.01 means 1% of the balance.",
			server_floor: 0.01,
			server_floor_percent: 1,
			paths: [
				"config_data.risk_pct_per_trade",
				"config_data.config.risk_pct_per_trade",
				"config_data.strategy_params.risk_pct_per_trade",
			],
			description:
				"The per-trade risk, in every place a strategy may read it from.",
		},
		rules: [],
	},
}));

vi.mock("@/lib/api", () => ({
	fetchSymbolSelectionSettings: vi.fn().mockResolvedValue({}),
	updateSymbolSelectionSettings: vi.fn().mockResolvedValue({}),
	useConfig: () => ({ data: configMock }),
	// The server's registry: the strategy classes it can run and its floors.
	useStrategyRegistry: () => ({ data: registryMock }),
	// No saved strategy yet: the editor is creating a new one.
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

describe("the risk limits the client offers are the server's (GATE 4)", () => {
	it("offers the server's daily loss limit, not the client's own copy of it", () => {
		renderPanel();

		// The server's floor is 2%. The copy the client used to carry said 5%.
		expect(valueOf("Daily Loss Limit (%)")).toBe("2");
	});

	it("offers no limit looser than the server's floor for that field", () => {
		renderPanel();

		const floor = registryMock.server_floor;
		expect(Number(valueOf("Risk Per Trade (%)"))).toBeLessThanOrEqual(
			floor.risk_per_trade_pct,
		);
		expect(Number(valueOf("Daily Loss Limit (%)"))).toBeLessThanOrEqual(
			floor.daily_loss_limit_pct,
		);
		expect(Number(valueOf("Max Drawdown (%)"))).toBeLessThanOrEqual(
			floor.max_drawdown_pct,
		);
		expect(Number(valueOf("Position Size (%)"))).toBeLessThanOrEqual(
			floor.position_size_pct,
		);
	});

	it("shows the server's floor as the ceiling, not a copy of it", () => {
		renderPanel();

		expect(screen.getByText(/Server floor: 2%/).textContent).toBeTruthy();
	});

	it("sends the server's limit to the server", async () => {
		renderPanel();

		clickDeploy();

		await waitFor(() => expect(saveStrategyConfigMock).toHaveBeenCalled());

		expect(savedPayload().config_data.risk_settings).toEqual({
			risk_per_trade_pct: 1,
			max_drawdown_pct: 10,
			daily_loss_limit_pct: 2,
			position_size_pct: 1,
		});
	});

	it("never sends a per-trade risk of its own", async () => {
		renderPanel();

		clickDeploy();

		await waitFor(() => expect(saveStrategyConfigMock).toHaveBeenCalled());

		// The per-trade risk is a FRACTION in the strategy parameter, and
		// the server REFUSES a value looser than its floor in every place it
		// may hide (`config_data.risk_pct_per_trade`,
		// `config_data.config.risk_pct_per_trade`,
		// `config_data.strategy_params.risk_pct_per_trade`). The client sets
		// the per-trade risk ONLY through `risk_settings`, which is in
		// PERCENT and is checked field by field.
		const configData = savedPayload().config_data as unknown as Record<
			string,
			unknown
		>;
		expect(configData.risk_pct_per_trade).toBeUndefined();
		expect(
			(configData.config as Record<string, unknown> | undefined)
				?.risk_pct_per_trade,
		).toBeUndefined();
		expect(
			(configData.strategy_params as Record<string, unknown> | undefined)
				?.risk_pct_per_trade,
		).toBeUndefined();
	});
});
