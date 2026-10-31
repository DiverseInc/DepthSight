// @vitest-environment jsdom
// src/__tests__/strategyRiskSettings.test.tsx
//
// GATE 4 (frontend half) -- the client's STRATEGY PICK and RISK SETTINGS have
// to be real: typed by the client, POSTed to the server, and reflected in the
// saved strategy.
//
// Before this change the strategy editor had NO risk controls at all, so
// nothing about risk ever reached `config_data`, and the picked strategy
// class was an implicit store value that was never visible to the client.
//
// These tests drive the REAL `ConfigAndLaunchPanel` and assert on the payload
// the mutation actually receives -- not on a rendered label.

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

// The panel derives `activeApiKeys` from `useConfig()` in a render-phase sync,
// so the mocked config has to be a STABLE object: a fresh one on every render
// makes the panel re-render forever.
//
// `registryMock` is the server's own answer: the client no longer carries a
// hand-written list of strategy classes, it asks the server which classes
// this server can run (`GET /api/v1/strategies/registry`).
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
import {
	SERVER_RISK_FLOOR_PCT,
	applyStrategyRegistry,
	validateStrategyRiskSettings,
} from "@/components/strategies/strategyEditorSettings";
import { useStrategyEditorStore } from "@/stores/strategyEditorStore";

/**
 * The floor the CLIENT is enforcing for a field.
 *
 * The client's copy of the server's floors comes from the server's registry
 * (`applyStrategyRegistry`) and is empty until it arrives, so this throws
 * instead of comparing against nothing when it has not.
 */
const floorOf = (
	field: keyof typeof registryMock.server_floor,
): number => {
	const value = SERVER_RISK_FLOOR_PCT[field];
	if (typeof value !== "number") {
		throw new Error(`the server's floor for ${field} has not been applied`);
	}
	return value;
};

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

const setNumber = (label: string, value: string) => {
	const input = screen.getByLabelText(label) as HTMLInputElement;
	fireEvent.change(input, { target: { value } });
};

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
	// The client's copy of the server's floors comes from the server's own
	// registry, so it is applied here: these tests must not depend on
	// another test having rendered the panel first.
	applyStrategyRegistry(registryMock);
	// The editor store's default name comes from i18n at module load time, which
	// is not initialised yet when the store module is evaluated. Pin it so the
	// deploy button is reachable.
	useStrategyEditorStore.setState({ name: "Gate 4 Strategy", symbol: "BTCUSDT" });
});

afterEach(() => {
	cleanup();
});

describe("the risk limits the client types (GATE 4)", () => {
	it("sends the typed risk limits to the server inside config_data", async () => {
		renderPanel();

		setNumber("Risk Per Trade (%)", "0.8");
		setNumber("Daily Loss Limit (%)", "2");
		setNumber("Max Drawdown (%)", "7.5");
		setNumber("Position Size (%)", "0.5");

		clickDeploy();

		await waitFor(() => expect(saveStrategyConfigMock).toHaveBeenCalled());

		expect(savedPayload().config_data.risk_settings).toEqual({
			risk_per_trade_pct: 0.8,
			max_drawdown_pct: 7.5,
			daily_loss_limit_pct: 2,
			position_size_pct: 0.5,
		});
	});

	it("refuses to deploy a daily loss limit looser than the server floor", async () => {
		renderPanel();

		setNumber(
			"Daily Loss Limit (%)",
			String(floorOf("daily_loss_limit_pct") + 1),
		);

		clickDeploy();

		await waitFor(() =>
			expect(screen.getByText(/rejects a value above/i).textContent).toBeTruthy(),
		);
		expect(saveStrategyConfigMock).not.toHaveBeenCalled();
	});

	it("refuses to deploy a risk per trade looser than the server floor", async () => {
		renderPanel();

		setNumber(
			"Risk Per Trade (%)",
			String(floorOf("risk_per_trade_pct") + 1),
		);

		clickDeploy();

		await waitFor(() =>
			expect(screen.getByText(/rejects a value above/i).textContent).toBeTruthy(),
		);
		expect(saveStrategyConfigMock).not.toHaveBeenCalled();
	});

	it("refuses to deploy risk limits that are not positive numbers", async () => {
		renderPanel();

		setNumber("Risk Per Trade (%)", "0");

		clickDeploy();

		await waitFor(() =>
			expect(screen.getByText(/greater than zero/i).textContent).toBeTruthy(),
		);
		expect(saveStrategyConfigMock).not.toHaveBeenCalled();
	});
});

describe("the strategy class the client picks (GATE 4)", () => {
	it("sends the picked strategy class to the server inside config_data", async () => {
		renderPanel();

		const select = screen.getByLabelText("Strategy Class") as HTMLSelectElement;
		fireEvent.change(select, { target: { value: "VolumeBreakout" } });

		clickDeploy();

		await waitFor(() => expect(saveStrategyConfigMock).toHaveBeenCalled());

		expect(savedPayload().config_data.strategy_name).toBe("VolumeBreakout");
	});

	it("only offers strategy classes the trading bot registers", () => {
		renderPanel();

		const select = screen.getByLabelText("Strategy Class") as HTMLSelectElement;
		const options = Array.from(select.options).map((option) => option.value);

		expect(options).toContain("VisualBuilderStrategy");
		expect(options).toContain("VolumeBreakout");
		// Classes the bot rejects cannot start, so they must not be offered.
		expect(options).not.toContain("ReverseUnknownStrategy");
	});
});

describe("the client-side risk validation mirrors the server floor", () => {
	it("accepts a daily loss limit at the server floor", () => {
		expect(
			validateStrategyRiskSettings({
				risk_per_trade_pct: 0.5,
				max_drawdown_pct: 10,
				daily_loss_limit_pct: floorOf("daily_loss_limit_pct"),
				position_size_pct: 0.5,
			}),
		).toEqual({});
	});

	it("rejects a daily loss limit looser than the server floor", () => {
		expect(
			validateStrategyRiskSettings({
				risk_per_trade_pct: 0.5,
				max_drawdown_pct: 10,
				daily_loss_limit_pct:
					floorOf("daily_loss_limit_pct") + 0.1,
				position_size_pct: 0.5,
			}),
		).toEqual({ daily_loss_limit_pct: "aboveFloor" });
	});

	it("rejects a risk per trade looser than the server floor", () => {
		expect(
			validateStrategyRiskSettings({
				risk_per_trade_pct: floorOf("risk_per_trade_pct") + 0.1,
				max_drawdown_pct: 10,
				daily_loss_limit_pct: 5,
				position_size_pct: 0.5,
			}),
		).toEqual({ risk_per_trade_pct: "aboveFloor" });
	});

	it("rejects a max drawdown looser than the server floor", () => {
		expect(
			validateStrategyRiskSettings({
				risk_per_trade_pct: 0.5,
				max_drawdown_pct: floorOf("max_drawdown_pct") + 0.1,
				daily_loss_limit_pct: 5,
				position_size_pct: 0.5,
			}),
		).toEqual({ max_drawdown_pct: "aboveFloor" });
	});

	it("rejects a position size looser than the server floor", () => {
		expect(
			validateStrategyRiskSettings({
				risk_per_trade_pct: 1,
				max_drawdown_pct: 10,
				daily_loss_limit_pct: 5,
				position_size_pct: floorOf("position_size_pct") + 0.1,
			}),
		).toEqual({ position_size_pct: "aboveFloor" });
	});

	it("rejects a non-positive limit", () => {
		expect(
			validateStrategyRiskSettings({
				risk_per_trade_pct: 0,
				max_drawdown_pct: 10,
				daily_loss_limit_pct: 5,
				position_size_pct: 1,
			}),
		).toEqual({ risk_per_trade_pct: "notPositive" });
	});

	it("rejects a position size that does not fit in the risk per trade", () => {
		expect(
			validateStrategyRiskSettings({
				risk_per_trade_pct: 0.4,
				max_drawdown_pct: 10,
				daily_loss_limit_pct: 5,
				position_size_pct: 0.8,
			}),
		).toEqual({ position_size_pct: "positionExceedsRisk" });
	});
});
