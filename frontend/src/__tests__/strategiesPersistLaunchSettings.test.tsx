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
