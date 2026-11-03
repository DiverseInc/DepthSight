// @vitest-environment jsdom
// src/__tests__/googleRedirectSignIn.test.tsx
//
// The Google sign-in flow, REDIRECT edition.
//
// The login page used to render <GoogleLogin> from @react-oauth/google, which
// opens the Google Identity Services POPUP. That popup was what hung for
// users.
//
// Google sends the user back to the only registered redirect URI -- the bare
// origin https://depthsight.diverseinc.net -- so the sign-in has to complete
// on the SITE ROOT and the credential arrives in the URL fragment. These
// tests drive that flow end to end:
//
//   * the login page starts a REDIRECT (the popup component is not rendered)
//     and pins the redirect URI to the registered bare origin,
//   * the route-independent handler signs in from the fragment on the root,
//   * the credential is removed from the URL,
//   * a redirect that comes back without a usable credential is a VISIBLE
//     failure, and
//   * the email/password login still works.

import {
	afterEach,
	beforeAll,
	beforeEach,
	describe,
	expect,
	it,
	vi,
} from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
	cleanup,
	fireEvent,
	render,
	screen,
	waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, useLocation } from "react-router-dom";
import type React from "react";
import loginEn from "@/locales/en/login.json";
import commonEn from "@/locales/en/common.json";

// The toast store is a module-level singleton, so the toasts it holds leak
// from one test into the next. Every toast raised during a test is recorded
// here instead, which makes "no failure was reported" assertable.
//
// If the login page renders <GoogleLogin> again, the popup flow is back and
// this spy records it.
const { toastSpy, googleLoginRendered } = vi.hoisted(() => ({
	toastSpy: vi.fn(),
	googleLoginRendered: vi.fn(),
}));

vi.mock("@/components/ui/use-toast", async () => {
	const actual = await vi.importActual<
		typeof import("@/components/ui/use-toast")
	>("@/components/ui/use-toast");
	return {
		...actual,
		useToast: () => {
			const toastState = actual.useToast();
			return {
				...toastState,
				toast: (args: Parameters<typeof toastState.toast>[0]) => {
					toastSpy(args);
					return toastState.toast(args);
				},
			};
		},
	};
});

vi.mock("@react-oauth/google", () => ({
	GoogleLogin: () => {
		googleLoginRendered();
		return null;
	},
}));

const apiClientMock = vi.fn();
vi.mock("@/lib/apiClient", () => ({
	apiClient: (...args: unknown[]) => apiClientMock(...args),
	refreshAccessToken: vi.fn(),
}));

import { AuthProvider } from "@/context/AuthContext";
import { GoogleRedirectHandler } from "@/components/auth/GoogleRedirectHandler";
import LoginPage from "@/pages/Login";
import {
	GOOGLE_OAUTH_PENDING_KEY,
	rememberPendingGoogleRequest,
} from "@/lib/googleOAuth";

const REGISTERED_REDIRECT_URI = "https://depthsight.diverseinc.net";

const fetchMock = vi.fn();

/**
 * jsdom in this runner does not expose `localStorage` (the auth bootstrap
 * reads it), so it is stubbed with the small surface the app actually uses.
 */
const tokenStore = new Map<string, string>();
const installLocalStorage = () => {
	vi.stubGlobal("localStorage", {
		getItem: (key: string) => tokenStore.get(key) ?? null,
		setItem: (key: string, value: string) => {
			tokenStore.set(key, value);
		},
		removeItem: (key: string) => {
			tokenStore.delete(key);
		},
		clear: () => tokenStore.clear(),
	});
};

const LocationProbe = () => {
	const location = useLocation();
	return <span data-testid="pathname">{location.pathname}</span>;
};

const wrapper = ({ children }: { children: React.ReactNode }) => (
	<QueryClientProvider client={new QueryClient()}>
		<MemoryRouter initialEntries={["/"]}>{children}</MemoryRouter>
	</QueryClientProvider>
);

/** Render the handler the way App.tsx mounts it: inside the auth provider. */
const renderHandler = () =>
	render(
		<AuthProvider>
			<GoogleRedirectHandler />
			<LocationProbe />
		</AuthProvider>,
		{ wrapper },
	);

const renderLogin = () =>
	render(
		<AuthProvider>
			<LoginPage />
		</AuthProvider>,
		{ wrapper },
	);

beforeAll(async () => {
	await i18n.use(initReactI18next).init({
		resources: { en: { login: loginEn, common: commonEn } },
		lng: "en",
		fallbackLng: "en",
		defaultNS: "common",
		interpolation: { escapeValue: false },
	});
});

beforeEach(() => {
	tokenStore.clear();
	installLocalStorage();
	toastSpy.mockClear();
	fetchMock.mockReset();
	fetchMock.mockResolvedValue({
		ok: true,
		status: 200,
		json: async () => ({
			token: { access_token: "ds-access", refresh_token: "ds-refresh" },
			user: {
				id: 1,
				username: "trader",
				email: "trader@example.com",
				plan: "free",
			},
		}),
	});
	vi.stubGlobal("fetch", fetchMock);
	apiClientMock.mockReset();
	apiClientMock.mockResolvedValue({
		id: 1,
		username: "trader",
		email: "trader@example.com",
		plan: "free",
	});
	vi.stubEnv(
		"VITE_GOOGLE_CLIENT_ID",
		"test-client-id.apps.googleusercontent.com",
	);
	window.history.replaceState({}, "", "/");
	sessionStorage.clear();
});

afterEach(() => {
	cleanup();
	vi.unstubAllEnvs();
});

describe("the login page starts the REDIRECT flow (no popup)", () => {
	it("does not render the popup button any more", () => {
		renderLogin();
		// A Google control IS on the page ...
		expect(screen.getByTestId("google-signin")).toBeTruthy();
		// ... and it is not the Google Identity Services popup.
		expect(googleLoginRendered).not.toHaveBeenCalled();
	});

	it("points at Google with the redirect URI pinned to the registered bare origin", () => {
		renderLogin();
		const link = screen.getByTestId("google-signin");
		const url = new URL(link.getAttribute("href") || "");

		expect(url.origin + url.pathname).toBe(
			"https://accounts.google.com/o/oauth2/v2/auth",
		);
		expect(url.searchParams.get("client_id")).toBe(
			"test-client-id.apps.googleusercontent.com",
		);
		// Exactly the registered URI: no trailing slash, no path.
		expect(url.searchParams.get("redirect_uri")).toBe(REGISTERED_REDIRECT_URI);
		expect(url.searchParams.get("response_type")).toBe("id_token");
		expect(url.searchParams.get("scope")).toBe("openid email profile");
		expect(url.searchParams.get("state")).toBeTruthy();
	});

	it("remembers the sign-in request when the button is used", () => {
		renderLogin();
		const link = screen.getByTestId("google-signin");
		const state = new URL(link.getAttribute("href") || "").searchParams.get(
			"state",
		);

		fireEvent.click(link, { preventDefault: true });

		const pending = JSON.parse(
			sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);
		expect(pending).not.toBeNull();
		expect(pending.state).toBe(state);
	});
});

describe("the redirect handler signs in on the site root", () => {
	it("posts the credential from the URL fragment to the Google endpoint", async () => {
		rememberPendingGoogleRequest("state-abc");
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();

		await waitFor(() =>
			expect(fetchMock).toHaveBeenCalledWith(
				"/api/v1/auth/google",
				expect.objectContaining({ method: "POST" }),
			),
		);
		const body = JSON.parse(fetchMock.mock.calls[0][1].body);
		expect(body).toEqual({ token: "google-id-token" });
	});

	it("removes the credential from the URL", async () => {
		rememberPendingGoogleRequest("state-abc");
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();

		await waitFor(() => expect(fetchMock).toHaveBeenCalled());
		expect(window.location.hash).toBe("");
		expect(window.location.href).not.toContain("google-id-token");
	});

	it("ignores a normal page load with no credential", async () => {
		renderHandler();

		await waitFor(() => expect(screen.getByTestId("pathname")).toBeTruthy());
		expect(fetchMock).not.toHaveBeenCalled();
		expect(toastSpy).not.toHaveBeenCalled();
	});
});

describe("a redirect without a usable credential is a VISIBLE failure", () => {
	it("reports an error returned by Google", async () => {
		rememberPendingGoogleRequest("state-abc");
		window.location.hash = "#error=access_denied&error_description=Denied";

		renderHandler();

		await waitFor(() =>
			expect(toastSpy).toHaveBeenCalledWith(
				expect.objectContaining({ variant: "destructive" }),
			),
		);
		expect(fetchMock).not.toHaveBeenCalled();
		expect(screen.getByTestId("pathname").textContent).toBe("/login");
		// The error must not be left in the address bar.
		expect(window.location.hash).toBe("");
	});

	it("reports a sign-in that came back without any credential", async () => {
		rememberPendingGoogleRequest("state-abc");
		window.location.hash = "";

		renderHandler();

		await waitFor(() =>
			expect(toastSpy).toHaveBeenCalledWith(
				expect.objectContaining({ variant: "destructive" }),
			),
		);
		expect(fetchMock).not.toHaveBeenCalled();
		expect(screen.getByTestId("pathname").textContent).toBe("/login");
	});

	it("refuses a credential that does not belong to this sign-in request", async () => {
		rememberPendingGoogleRequest("state-abc");
		window.location.hash = "#id_token=google-id-token&state=someone-elses-state";

		renderHandler();

		await waitFor(() =>
			expect(toastSpy).toHaveBeenCalledWith(
				expect.objectContaining({ variant: "destructive" }),
			),
		);
		expect(fetchMock).not.toHaveBeenCalled();
		expect(window.location.hash).toBe("");
	});
});

describe("the handler is mounted above the router", () => {
	it("sits inside the auth provider and before the route table", () => {
		// The sign-in completes on the SITE ROOT, so the handler must not live
		// inside a route element. This pins where it is mounted.
		const candidates = [
			resolve(process.cwd(), "src/App.tsx"),
			resolve(process.cwd(), "frontend/src/App.tsx"),
		];
		const appPath = candidates.find((path) => existsSync(path));
		expect(appPath).toBeDefined();
		// Comments are dropped first: the wiring is commented in App.tsx, and a
		// comment mentioning the route table must not be mistaken for it.
		const source = readFileSync(appPath as string, "utf-8").replace(
			/\/\*[\s\S]*?\*\//g,
			"",
		);
		const provider = source.search(/<AuthProvider\s*>/);
		const handler = source.search(/<GoogleRedirectHandler\s*\/>/);
		const routes = source.search(/<Routes\s*>/);
		expect(provider).toBeGreaterThan(-1);
		expect(handler).toBeGreaterThan(provider);
		expect(routes).toBeGreaterThan(handler);
	});
});

describe("the email and password login is untouched", () => {
	it("still posts the password form to the token endpoint", async () => {
		const { container } = renderLogin();

		const username = container.querySelector<HTMLInputElement>(
			'input[name="username"]',
		);
		const password = container.querySelector<HTMLInputElement>(
			'input[name="password"]',
		);
		expect(username).not.toBeNull();
		expect(password).not.toBeNull();

		fireEvent.change(username as HTMLInputElement, {
			target: { value: "trader" },
		});
		fireEvent.change(password as HTMLInputElement, {
			target: { value: "hunter2" },
		});
		fireEvent.click(screen.getByRole("button", { name: /log in/i }));

		await waitFor(() =>
			expect(fetchMock).toHaveBeenCalledWith(
				"/api/v1/token",
				expect.objectContaining({ method: "POST" }),
			),
		);
		const body = fetchMock.mock.calls[0][1].body;
		expect(body).toContain("username=trader");
		expect(body).toContain("password=hunter2");
	});
});
