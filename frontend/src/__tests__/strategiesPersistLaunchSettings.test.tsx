// @vitest-environment jsdom
// src/__tests__/strategiesPersistLaunchSettings.test.tsx
//
// GATE 4 (frontend half) -- "the Strategies page has no update mutation".
//
// The launch modal lets the client change the symbol-selection mode, the
// oracle/NATR filters and the ML confirmation of a saved strategy. Those
// changes were sent ONLY as `params` to `startStrategy`, and the backend
// merges them into the RUNNING instance without ever writing them back to the
// saved strategy config. The client's pick was therefore never recorded.
//
// These tests drive the REAL `Strategies` page and assert that the launch
// settings are persisted through the update mutation BEFORE the strategy is
// started.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18n from "i18next";
import type React from "react";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import commonEn from "@/locales/en/common.json";
import strategiesEn from "@/locales/en/strategies.json";

const startStrategyMock = vi.fn();
const updateStrategyConfigMock = vi.fn();
const stopStrategyMock = vi.fn();
const deleteStrategyConfigMock = vi.fn();
const runBacktestMock = vi.fn();
// The toast is the page's only way of saying WHY it refused, so it is
// captured instead of rendered.
const toastMock = vi.fn();

vi.mock("@/components/ui/use-toast", () => ({
	useToast: () => ({ toast: toastMock }),
}));

const savedConfig = {
	id: "cfg-1",
	name: "Gate 4 Strategy",
	description: "saved from the server",
	user_id: 1,
	config_data: {
		enabled: true,
		strategy_name: "VisualBuilderStrategy",
		symbol: "BTCUSDT",
		marketType: "FUTURES",
		filters: { id: "filters_root", type: "AND", children: [] },
		entryTrigger: { type: "on_candle_close", timeframe: "5m" },
		entryConditions: { id: "entry_root", type: "AND", children: [] },
		initialization: { id: "init_action_1", type: "open_position", params: {} },
		positionManagement: [],
		risk_settings: {
			risk_per_trade_pct: 0.5,
			max_drawdown_pct: 10,
			daily_loss_limit_pct: 2,
			position_size_pct: 1,
		},
	},
	symbol_selection_mode: "STATIC",
	symbols: ["BTCUSDT"],
	use_ml_confirmation: false,
	created_at: "2026-01-01T00:00:00Z",
	updated_at: "2026-01-01T00:00:00Z",
};

vi.mock("@/lib/api", () => ({
	useStrategies: () => ({
		data: [],
		isLoading: false,
		isError: false,
		error: null,
	}),
	useStrategyConfigsList: () => ({
		data: [savedConfig],
		isLoading: false,
		isError: false,
		error: null,
	}),
	useStartStrategy: () => ({ mutate: startStrategyMock, isPending: false }),
	useUpdateStrategyConfig: () => ({
		mutate: updateStrategyConfigMock,
		isPending: false,
	}),
	useStopStrategy: () => ({ mutate: stopStrategyMock, isPending: false }),
	useDeleteStrategyConfig: () => ({
		mutate: deleteStrategyConfigMock,
		isPending: false,
	}),
	useRunBacktest: () => ({ mutate: runBacktestMock, isPending: false }),
	useConfig: () => ({
		data: { apiKeys: [{ id: 1, isActive: true, status: "valid" }] },
	}),
	useMultiAccountBalances: () => ({ data: [] }),
	// The server's registry: the strategy classes it can run and its floors.
	// The Strategies page applies it so the card badge is right.
	useStrategyRegistry: () => ({
		data: {
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
			// The per-trade risk has its own floor, in its own unit: a
			// FRACTION of the balance (0.01 is 1%).
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
	}),
}));

vi.mock("@/context/AuthContext", () => ({
	useAuth: () => ({
		user: { id: 1, username: "tester", email: "t@example.com", plan: "pro" },
		isLoading: false,
	}),
}));

// The page layout needs the sidebar provider; it is not what is under test.
vi.mock("@/components/layout/PageLayout", () => ({
	PageLayout: ({ children }: { children: React.ReactNode }) => (
		<div>{children}</div>
	),
}));

import Strategies from "@/pages/Strategies";

const wrapper = ({ children }: { children: React.ReactNode }) => (
	<QueryClientProvider client={new QueryClient()}>
		<MemoryRouter>{children}</MemoryRouter>
	</QueryClientProvider>
);

const renderPage = () => render(<Strategies />, { wrapper });

const openLaunchModal = async () => {
	fireEvent.click(screen.getByRole("button", { name: "Start" }));
	await waitFor(() =>
		expect(screen.getByRole("button", { name: /launch strategy/i })).toBeTruthy(),
	);
};

const confirmLaunch = () => {
	fireEvent.click(screen.getByRole("button", { name: /launch strategy/i }));
};

const persistedPayload = () => {
	const call = updateStrategyConfigMock.mock.calls[0];
	if (!call) throw new Error("the update mutation was never called");
	return call[0] as {
		id: string;
		payload: {
			name: string;
			config_data: {
				strategy_name?: string;
				risk_settings?: Record<string, number>;
				use_ml_confirmation?: boolean;
				breakeven_on_regime_change?: boolean;
			};
		};
	};
};

/** Every message the page has shown the client. */
const toastMessages = () =>
	toastMock.mock.calls.map((call) => String(call[0]?.description ?? ""));

beforeAll(async () => {
	// jsdom has neither of these, and the radix controls the launch modal uses
	// (slider, radio group, checkbox) need them to render at all.
	if (!("ResizeObserver" in globalThis)) {
		globalThis.ResizeObserver = class {
			observe() {}
			unobserve() {}
			disconnect() {}
		} as unknown as typeof ResizeObserver;
	}
	if (!Element.prototype.hasPointerCapture) {
		Element.prototype.hasPointerCapture = () => false;
	}
	if (!Element.prototype.setPointerCapture) {
		Element.prototype.setPointerCapture = () => {};
	}
	if (!Element.prototype.releasePointerCapture) {
		Element.prototype.releasePointerCapture = () => {};
	}

	await i18n.use(initReactI18next).init({
		resources: {
			en: { common: commonEn, strategies: strategiesEn },
		},
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

beforeEach(() => {
	startStrategyMock.mockReset();
	updateStrategyConfigMock.mockReset();
	stopStrategyMock.mockReset();
	deleteStrategyConfigMock.mockReset();
	runBacktestMock.mockReset();
	toastMock.mockReset();
	updateStrategyConfigMock.mockImplementation(
		(
			_vars: unknown,
			options?: { onSuccess?: (data: unknown) => void },
		) => {
			options?.onSuccess?.({ id: "cfg-1" });
		},
	);
});

afterEach(() => {
	cleanup();
});

describe("the Strategies page records the client's launch settings (GATE 4)", () => {
	it("shows the risk limits that are saved on the strategy", () => {
		renderPage();

		expect(screen.getByText(/Risk Limits/)).toBeTruthy();
		expect(screen.getByText(/2% daily \/ 0\.5% per trade/)).toBeTruthy();
	});

	it("persists the launch settings on the saved strategy before starting it", async () => {
		renderPage();
		await openLaunchModal();

		confirmLaunch();

		await waitFor(() => expect(updateStrategyConfigMock).toHaveBeenCalled());

		const { id, payload } = persistedPayload();
		expect(id).toBe("cfg-1");
		// The saved risk limits survive the launch.
		expect(payload.config_data.risk_settings).toEqual({
			risk_per_trade_pct: 0.5,
			max_drawdown_pct: 10,
			daily_loss_limit_pct: 2,
			position_size_pct: 1,
		});
		// The launch settings are recorded on the saved strategy.
		expect(payload.config_data.use_ml_confirmation).toBe(false);
		expect(payload.config_data.breakeven_on_regime_change).toBe(false);
		expect(startStrategyMock).toHaveBeenCalled();
	});

	it("does not start the strategy when the launch settings could not be saved", async () => {
		updateStrategyConfigMock.mockImplementation(
			(
				_vars: unknown,
				options?: { onError?: (error: Error) => void },
			) => {
				options?.onError?.(new Error("save failed"));
			},
		);

		renderPage();
		await openLaunchModal();

		confirmLaunch();

		await waitFor(() => expect(updateStrategyConfigMock).toHaveBeenCalled());
		expect(startStrategyMock).not.toHaveBeenCalled();
	});
});

// The launch update carries the strategy's SAVED settings back to the server.
// The server refuses a configuration it will not accept -- a risk limit
// looser than its floor, a strategy class it cannot run -- and it refuses the
// WHOLE update, so the strategy would never start and the only sign of it
// would be a bare error. The client checks the same rules first.
describe("the Strategies page does not send what the server will refuse (GATE 4)", () => {
	it("refuses to launch a strategy whose saved risk limit is looser than the server's", async () => {
		const saved = savedConfig.config_data.risk_settings;
		savedConfig.config_data.risk_settings = {
			...saved,
			// The server's floor is 1% per trade. This strategy was saved when
			// the server allowed 3%.
			risk_per_trade_pct: 3,
		};

		try {
			renderPage();
			await openLaunchModal();

			confirmLaunch();

			await waitFor(() => expect(toastMock).toHaveBeenCalled());
			expect(updateStrategyConfigMock).not.toHaveBeenCalled();
			expect(startStrategyMock).not.toHaveBeenCalled();
			expect(toastMessages().some((m) => /looser than the server/i.test(m))).toBe(
				true,
			);
		} finally {
			savedConfig.config_data.risk_settings = saved;
		}
	});

	it("refuses to launch a strategy whose class the trading bot does not register", async () => {
		const saved = savedConfig.config_data.strategy_name;
		savedConfig.config_data.strategy_name = "RemovedStrategy";

		try {
			renderPage();
			await openLaunchModal();

			confirmLaunch();

			await waitFor(() => expect(toastMock).toHaveBeenCalled());
			expect(updateStrategyConfigMock).not.toHaveBeenCalled();
			expect(startStrategyMock).not.toHaveBeenCalled();
			expect(toastMessages().some((m) => /registers/i.test(m))).toBe(true);
		} finally {
			savedConfig.config_data.strategy_name = saved;
		}
	});

	it("refuses to launch a strategy whose saved per-trade risk the server would refuse", async () => {
		// The per-trade risk is a FRACTION of the balance in the strategy
		// parameter. A configuration saved before the server bounded it may
		// carry a PERCENT there (0.5 = 0.5%), which the server reads as 50%
		// and refuses.
		const saved = savedConfig.config_data as unknown as Record<string, unknown>;
		saved.risk_pct_per_trade = 0.5;

		try {
			renderPage();
			await openLaunchModal();

			confirmLaunch();

			await waitFor(() => expect(toastMock).toHaveBeenCalled());
			expect(updateStrategyConfigMock).not.toHaveBeenCalled();
			expect(startStrategyMock).not.toHaveBeenCalled();
			expect(toastMessages().some((m) => /per-trade risk/i.test(m))).toBe(true);
		} finally {
			delete saved.risk_pct_per_trade;
		}
	});
});
