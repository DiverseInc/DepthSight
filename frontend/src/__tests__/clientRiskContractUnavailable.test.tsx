// @vitest-environment jsdom
// src/__tests__/clientRiskContractUnavailable.test.tsx
//
// GATE 4 (frontend half): what the client does while the server's contract
// has NOT arrived.
//
// THE DEFECT THIS PINS
// =====================
// The client used to carry its own copy of the server's risk floors and used
// it whenever the server had not answered yet. A copy can only be right by
// luck: if the server's floor was tighter than the copy, the editor offered a
// limit the server refuses -- `_validate_risk_settings` in
// `bot_module/strategy_registry.py` answers "risk_settings.daily_loss_limit_pct
// = 5.0 is looser than the server limit of 2.0" and the whole save is
// refused.
//
// So while the contract is unknown the client may not CHECK anything and may
// not SEND anything: it does not know the server's floor, and it does not
// guess one.
//
// This file is separate from `clientRiskContract.test.tsx` on purpose: the
// contract is applied to the client's module state once and stays applied,
// so the "the contract has not arrived" state needs its own module.

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
const toastMock = vi.fn();

const { configMock, savedConfigsMock } = vi.hoisted(() => ({
	configMock: {
		apiKeys: [{ id: 1, isActive: true, status: "valid" }],
	},
	savedConfigsMock: [] as unknown[],
}));

vi.mock("@/lib/api", () => ({
	fetchSymbolSelectionSettings: vi.fn().mockResolvedValue({}),
	updateSymbolSelectionSettings: vi.fn().mockResolvedValue({}),
	useConfig: () => ({ data: configMock }),
	// The server's registry did NOT answer.
	useStrategyRegistry: () => ({ data: undefined, isError: true }),
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

vi.mock("@/components/ui/use-toast", () => ({
	useToast: () => ({ toast: toastMock }),
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
	toastMock.mockReset();
	useStrategyEditorStore.setState({ name: "Gate 4 Strategy", symbol: "BTCUSDT" });
});

afterEach(() => {
	cleanup();
});

describe("the server's contract has not arrived (GATE 4)", () => {
	it("refuses to deploy, because no limit can be checked without the server's floor", async () => {
		renderPanel();

		clickDeploy();

		await waitFor(() => expect(toastMock).toHaveBeenCalled());
		expect(saveStrategyConfigMock).not.toHaveBeenCalled();
		expect(startStrategyMock).not.toHaveBeenCalled();
	});

	it("tells the client WHY nothing was sent", async () => {
		renderPanel();

		clickDeploy();

		await waitFor(() => expect(toastMock).toHaveBeenCalled());
		const message = String(toastMock.mock.calls[0][0]?.description ?? "");
		expect(message).toMatch(/have not loaded yet/i);
	});

	it("shows no risk floor of its own while the server's floor is unknown", () => {
		renderPanel();

		// The client must not put a number on screen that it made up.
		expect(screen.queryByText(/Server floor: \d/)).toBeNull();
		expect(screen.getByText(/have not loaded yet/i).textContent).toBeTruthy();
	});
});
