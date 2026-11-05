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
import { dirname, resolve } from "node:path";
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
	GOOGLE_OAUTH_SUBMITTED_KEY,
	PENDING_MAX_AGE_MS,
	claimGoogleSignInSubmission,
	lookupPendingGoogleRequest,
	readPendingGoogleRequest,
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

/**
 * The REAL jsdom sessionStorage, captured before any test stubs it, so a
 * test that blocks storage cannot leak into the next one.
 */
const realSessionStorage = sessionStorage;

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

/**
 * Write a sign-in request that is OLDER than the window, the way a slow
 * device or a long hesitation at Google's consent screen would leave it.
 *
 * It is written to BOTH stores, exactly as the app writes it: the shared
 * copy and the per-tab copy.
 */
const writeAgedMarker = (
	state: string,
	ageMs: number = PENDING_MAX_AGE_MS + 5 * 60 * 1000,
) => {
	const payload = JSON.stringify({ state, at: Date.now() - ageMs });
	localStorage.setItem(GOOGLE_OAUTH_PENDING_KEY, payload);
	sessionStorage.setItem(GOOGLE_OAUTH_PENDING_KEY, payload);
};

/**
 * A browser that blocks storage for the sign-in marker.
 *
 * The marker cannot be read or written, while the rest of the app's storage
 * still works. This is the shape of the failure the app has to survive: it
 * cannot tell "no sign-in was started" from "I cannot look".
 */
const installMarkerStorageBlocked = () => {
	const real = localStorage as Storage;
	const realSession = sessionStorage as Storage;
	const block = (key: string) => key === GOOGLE_OAUTH_PENDING_KEY;
	vi.stubGlobal("localStorage", {
		getItem: (key: string) => {
			if (block(key)) throw new Error("storage is blocked");
			return real.getItem(key);
		},
		setItem: (key: string, value: string) => {
			if (block(key)) throw new Error("storage is blocked");
			real.setItem(key, value);
		},
		removeItem: (key: string) => {
			if (block(key)) throw new Error("storage is blocked");
			real.removeItem(key);
		},
		clear: () => real.clear(),
	});
	vi.stubGlobal("sessionStorage", {
		getItem: (key: string) => {
			if (block(key)) throw new Error("storage is blocked");
			return realSession.getItem(key);
		},
		setItem: (key: string, value: string) => {
			if (block(key)) throw new Error("storage is blocked");
			realSession.setItem(key, value);
		},
		removeItem: (key: string) => {
			if (block(key)) throw new Error("storage is blocked");
			realSession.removeItem(key);
		},
		clear: () => realSession.clear(),
	});
};

/**
 * A browser that blocks storage entirely: writes throw, so a sign-in cannot
 * record its marker.
 */
const installStorageWritesBlocked = () => {
	const blocked = {
		getItem: () => null,
		setItem: () => {
			throw new Error("storage is blocked");
		},
		removeItem: () => {
			throw new Error("storage is blocked");
		},
		clear: () => {},
		key: () => null,
		length: 0,
	};
	vi.stubGlobal("localStorage", blocked);
	vi.stubGlobal("sessionStorage", blocked);
};

/** Let every pending promise and timer settle before asserting a count. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

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
	// The real sessionStorage: a test that blocks storage must not leak
	// into the next one.
	vi.stubGlobal("sessionStorage", realSessionStorage);
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
	// The URL is reset before every test. The fragment is cleared FIRST: a
	// `replaceState` that changes the fragment as well as the path makes
	// jsdom attempt a navigation it cannot implement, and the error it
	// raises from a timer leaves the worker with no verdict at all -- so a
	// test that leaves a credential in the address bar would hang the whole
	// suite instead of failing it.
	window.location.hash = "";
	window.history.replaceState({}, "", "/");
	sessionStorage.clear();
});

afterEach(() => {
	cleanup();
	vi.unstubAllEnvs();
	vi.unstubAllGlobals();
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

		// The SHARED store is the one that makes the sign-in verifiable in
		// another tab, so the marker has to be in it.
		const pending = JSON.parse(
			localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);
		expect(pending).not.toBeNull();
		expect(pending.state).toBe(state);
		// And in the PER-TAB store, which is what tells THIS tab that it
		// started the sign-in.
		expect(
			JSON.parse(sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null")
				.state,
		).toBe(state);
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
		expect(body).toEqual({
			token: "google-id-token",
			signin_nonce: expect.any(String),
		});
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

describe("the credential never stays in the address bar", () => {
	it("is removed even when it has already been sent", async () => {
		rememberPendingGoogleRequest("state-abc");
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();
		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

		// The page is reloaded and the browser restores the URL with the
		// credential still in it. The claim says it was already sent, so the
		// handler returns early -- but it must scrub the URL FIRST: the ID
		// token is a BEARER credential, and it must not stay in the address
		// bar or in that tab's history.
		window.location.hash = "#id_token=google-id-token&state=state-abc";
		cleanup();
		renderHandler();
		await settle();

		expect(window.location.hash).toBe("");
		expect(window.location.href).not.toContain("google-id-token");
	});
});

describe("a sign-in that comes back in ANOTHER tab", () => {
	it("is accepted, because the marker is reachable across tabs", async () => {
		// Tab A started the sign-in. In tab B the PER-TAB store is empty and
		// only the SHARED store holds the marker -- which is exactly what a
		// second tab, a reopened tab or a duplicated tab sees. A marker that
		// only lived in sessionStorage is missing here, and the LEGITIMATE
		// sign-in is rejected.
		localStorage.setItem(
			GOOGLE_OAUTH_PENDING_KEY,
			JSON.stringify({ state: "state-abc", at: Date.now() }),
		);
		expect(sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY)).toBeNull();
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();

		await waitFor(() =>
			expect(fetchMock).toHaveBeenCalledWith(
				"/api/v1/auth/google",
				expect.objectContaining({ method: "POST" }),
			),
		);
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it("still refuses a credential that has no marker at all", async () => {
		// The marker is the proof that THIS browser started the sign-in.
		// Without it the credential is a login CSRF attempt, and it is
		// refused.
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();

		await waitFor(() =>
			expect(toastSpy).toHaveBeenCalledWith(
				expect.objectContaining({ variant: "destructive" }),
			),
		);
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it("reports a blocked storage browser instead of failing in silence", async () => {
		installMarkerStorageBlocked();
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();

		await waitFor(() =>
			expect(toastSpy).toHaveBeenCalledWith(
				expect.objectContaining({ variant: "destructive" }),
			),
		);
		const description = String(toastSpy.mock.calls[0][0].description);
		expect(description).toBe(
			(loginEn as unknown as Record<string, string>).googleSignInStorageBlocked,
		);
		expect(fetchMock).not.toHaveBeenCalled();
	});
});

describe("a browser that blocks storage", () => {
	it("cannot record the marker, so the sign-in is reported and not started", async () => {
		installStorageWritesBlocked();
		renderLogin();
		const link = screen.getByTestId("google-signin");

		// The click is cancelled: a sign-in whose marker cannot be recorded
		// is a sign-in that is guaranteed to be rejected on the way back.
		const notCancelled = fireEvent.click(link);

		expect(notCancelled).toBe(false);
		await waitFor(() =>
			expect(toastSpy).toHaveBeenCalledWith(
				expect.objectContaining({ variant: "destructive" }),
			),
		);
		const description = String(toastSpy.mock.calls[0][0].description);
		expect(description).toBe(
			(loginEn as unknown as Record<string, string>).googleSignInStorageBlocked,
		);
	});

	it("reports that the marker cannot be read at all", () => {
		// Both stores refuse to be read: the app cannot even tell whether a
		// sign-in was started, and that has to be said out loud rather than
		// turned into a silent rejection.
		const blocked = {
			getItem: () => {
				throw new Error("storage is blocked");
			},
			setItem: () => {
				throw new Error("storage is blocked");
			},
			removeItem: () => {
				throw new Error("storage is blocked");
			},
			clear: () => {},
		};
		vi.stubGlobal("localStorage", blocked);
		vi.stubGlobal("sessionStorage", blocked);

		const lookup = lookupPendingGoogleRequest();

		expect(lookup.kind).toBe("unavailable");
	});
});

describe("one redirect produces exactly one session", () => {
	it("does not send the credential twice when the handler is remounted", async () => {
		rememberPendingGoogleRequest("state-abc");
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();
		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

		// The handler is unmounted and mounted again: a fresh component
		// instance, with a fresh `handled` ref. The storage is the same.
		cleanup();
		renderHandler();
		await settle();

		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it("does not send the credential twice after a reload", async () => {
		rememberPendingGoogleRequest("state-abc");
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();
		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

		// The page is reloaded and the browser restores the URL with the
		// credential still in it. The storage survives the reload; a React
		// ref does not, and the credential lives in the URL fragment, which
		// a reload keeps.
		window.location.hash = "#id_token=google-id-token&state=state-abc";
		cleanup();
		renderHandler();
		await settle();

		expect(fetchMock).toHaveBeenCalledTimes(1);
		// The restored page must not report a failure either: the sign-in
		// this credential belongs to is under way.
		expect(toastSpy).not.toHaveBeenCalledWith(
			expect.objectContaining({ variant: "destructive" }),
		);
	});

	it("claims the one-time value in storage, so the claim survives a reload", () => {
		// The claim lives in localStorage, which a reload keeps. A React ref
		// survives neither a remount nor a reload.
		expect(claimGoogleSignInSubmission("state-abc")).toBe(true);
		expect(claimGoogleSignInSubmission("state-abc")).toBe(false);
		expect(
			JSON.parse(localStorage.getItem(GOOGLE_OAUTH_SUBMITTED_KEY) || "{}")
				.states,
		).toContain("state-abc");
	});

	it("sends the one-time value to the server, so the server can consume it once", async () => {
		rememberPendingGoogleRequest("state-abc");
		const marker = JSON.parse(
			localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();

		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
		const body = JSON.parse(fetchMock.mock.calls[0][1].body);
		expect(body.signin_nonce).toBe(marker.nonce);
	});

	it("generates a fresh one-time value for every attempt", () => {
		// The server consumes the one-time value exactly once, so a second
		// attempt must carry a NEW one. The OAuth `state` cannot play that
		// role: it is built once per page render, so a second click on the
		// same page would reuse a value the server has already consumed --
		// and the user's second attempt would be refused as a replay.
		rememberPendingGoogleRequest("state-abc");
		const first = JSON.parse(
			localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);
		rememberPendingGoogleRequest("state-abc");
		const second = JSON.parse(
			localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);

		expect(second.nonce).toBeTruthy();
		expect(second.nonce).not.toBe(first.nonce);
	});

	it("lets the user try again after a sign-in was already sent", () => {
		// The claim stops the SAME credential from being sent twice. It must
		// not stop the user from signing in again.
		expect(claimGoogleSignInSubmission("state-abc")).toBe(true);
		expect(claimGoogleSignInSubmission("state-abc")).toBe(false);

		rememberPendingGoogleRequest("state-abc");

		expect(claimGoogleSignInSubmission("state-abc")).toBe(true);
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

describe("a sign-in that takes longer than the window", () => {
	it("accepts a credential whose state matches a marker older than the window", async () => {
		// A slow device, a hesitation at the consent screen, a phone in the
		// background: the credential is still perfectly valid, and the age of
		// the request is not a reason to throw it away.
		writeAgedMarker("state-abc");
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();

		await waitFor(() =>
			expect(fetchMock).toHaveBeenCalledWith(
				"/api/v1/auth/google",
				expect.objectContaining({ method: "POST" }),
			),
		);
		const body = JSON.parse(fetchMock.mock.calls[0][1].body);
		expect(body).toEqual({
			token: "google-id-token",
			signin_nonce: expect.any(String),
		});
	});

	it("rejects a credential whose state does not match an old marker", async () => {
		writeAgedMarker("state-abc");
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

	it("keeps an old marker, so the state can still be compared", () => {
		writeAgedMarker("state-abc");

		const pending = readPendingGoogleRequest();

		expect(pending).not.toBeNull();
		expect(pending?.state).toBe("state-abc");
	});

	it("says the sign-in took too long when there is no marker at all", async () => {
		window.location.hash = "#id_token=google-id-token&state=state-abc";

		renderHandler();

		await waitFor(() =>
			expect(toastSpy).toHaveBeenCalledWith(
				expect.objectContaining({ variant: "destructive" }),
			),
		);
		const description = String(toastSpy.mock.calls[0][0].description);
		// "took too long" is the accurate message here -- not "unverified".
		expect(description).not.toBe(loginEn.googleSignInUnverified);
		expect(description).toBe(
			(loginEn as unknown as Record<string, string>).googleSignInExpired,
		);
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it("does not let a stale marker fail an unrelated later visit", async () => {
		writeAgedMarker("state-abc");
		window.location.hash = "";

		renderHandler();

		await waitFor(() => expect(screen.getByTestId("pathname")).toBeTruthy());
		expect(toastSpy).not.toHaveBeenCalled();
		expect(fetchMock).not.toHaveBeenCalled();
		// The stale marker is cleaned up, so it cannot linger.
		expect(sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY)).toBeNull();
	});
});

/**
 * Locate frontend/src/App.tsx from wherever the runner was started: the
 * front-end directory, the repository root, or a directory below them.
 */
const findAppSource = (): string | undefined => {
	let dir = process.cwd();
	for (let depth = 0; depth < 6; depth += 1) {
		for (const candidate of [
			resolve(dir, "src/App.tsx"),
			resolve(dir, "frontend/src/App.tsx"),
		]) {
			if (existsSync(candidate)) return candidate;
		}
		const parent = dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}
	return undefined;
};

describe("the handler is mounted above the router", () => {
	it("sits inside the auth provider and before the route table", () => {
		// The sign-in completes on the SITE ROOT, so the handler must not live
		// inside a route element. This pins where it is mounted.
		const appPath = findAppSource();
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

/**
 * The Google link can be followed in more ways than a primary click.
 *
 * The marker is what proves the credential belongs to a sign-in THIS browser
 * started: a credential that arrives with no marker is REFUSED. So the marker
 * has to exist for EVERY way the link can be followed -- a middle click, the
 * context menu's "Open link in new tab", a ctrl/cmd+click -- and not only in
 * the anchor's `onClick`.
 *
 * A middle click dispatches `pointerdown` and `auxclick`, and NO `click`.
 * The context menu dispatches `pointerdown` and `contextmenu`, and no `click`
 * either. A marker written only in `onClick` is missing in both cases: the
 * customer is sent to Google, comes back with a perfectly valid credential,
 * and it is refused -- the customer cannot sign in at all.
 */
describe("the Google link followed in EVERY way a browser can follow it", () => {
	const stateOf = (link: HTMLElement) =>
		new URL(link.getAttribute("href") || "").searchParams.get("state");

	it("records the sign-in on a MIDDLE click, and accepts the credential in the new tab", async () => {
		renderLogin();
		const link = screen.getByTestId("google-signin");
		const state = stateOf(link);

		// A middle click: pointerdown and auxclick -- and NO click event.
		fireEvent.pointerDown(link, { button: 1 });
		fireEvent(
			link,
			new MouseEvent("auxclick", {
				bubbles: true,
				cancelable: true,
				button: 1,
			}),
		);

		// The marker is in the SHARED store: that is what the new tab reads.
		const shared = JSON.parse(
			localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);
		expect(shared?.state).toBe(state);
		// This tab did not follow the link, so it must not claim the sign-in.
		expect(sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY)).toBeNull();

		// The sign-in comes back in ANOTHER tab: the per-tab store is empty.
		window.location.hash = `#id_token=google-id-token&state=${state}`;
		renderHandler();

		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
	});

	it("records the sign-in when the context menu opens it in a new tab", async () => {
		renderLogin();
		const link = screen.getByTestId("google-signin");
		const state = stateOf(link);

		// The context menu: pointerdown and contextmenu -- and NO click event.
		fireEvent.pointerDown(link, { button: 2 });
		fireEvent.contextMenu(link);

		const shared = JSON.parse(
			localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);
		expect(shared?.state).toBe(state);
		expect(sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY)).toBeNull();

		window.location.hash = `#id_token=google-id-token&state=${state}`;
		renderHandler();

		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
	});

	it("records the sign-in on a ctrl/cmd+click, which opens a new tab", async () => {
		renderLogin();
		const link = screen.getByTestId("google-signin");
		const state = stateOf(link);

		// ctrl+click (cmd+click on a Mac) opens a new tab. The browser still
		// dispatches a click event, with the modifier held down.
		fireEvent.pointerDown(link, { button: 0, ctrlKey: true });
		fireEvent.click(link, { button: 0, ctrlKey: true });

		const shared = JSON.parse(
			localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);
		expect(shared?.state).toBe(state);
		// The tab the user stayed in is not the one that follows the link, so
		// it must not claim the sign-in: a sign-in it is not following is
		// not a failure it may report.
		expect(sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY)).toBeNull();

		window.location.hash = `#id_token=google-id-token&state=${state}`;
		renderHandler();

		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
	});

	it("records the sign-in when the KEYBOARD's context menu opens it in a new tab", async () => {
		renderLogin();
		const link = screen.getByTestId("google-signin");
		const state = stateOf(link);

		// Shift+F10 (or the Menu key) opens the keyboard's context menu: a
		// keydown, and NO click event. "Open link in new tab" from that menu
		// follows the link exactly like the mouse's one does.
		fireEvent.keyDown(link, { key: "F10", shiftKey: true });

		const shared = JSON.parse(
			localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY) || "null",
		);
		expect(shared?.state).toBe(state);
		// The tab the user stayed in is not the one that follows the link, so
		// it must not claim the sign-in.
		expect(sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY)).toBeNull();

		window.location.hash = `#id_token=google-id-token&state=${state}`;
		renderHandler();

		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
	});
});

/**
 * A sign-in that is in flight must survive anything that is NOT it.
 *
 * The marker is the proof that the credential belongs to a sign-in this
 * browser started. A failure that is not ours -- a foreign credential that
 * lands on the origin, an unrelated page load -- used to destroy that proof,
 * so the credential that came back afterwards was refused and the customer
 * could not sign in.
 */
describe("a sign-in in flight is not destroyed by something else", () => {
	it("survives a foreign credential that lands on the origin", async () => {
		// A sign-in is in flight: the marker is in both stores.
		rememberPendingGoogleRequest("state-abc");

		// Something else lands on the origin with a credential that is NOT
		// ours: a plain link, a cross-site redirect, a pasted URL.
		window.location.hash =
			"#id_token=foreign-token&state=someone-elses-state";
		renderHandler();

		await waitFor(() =>
			expect(toastSpy).toHaveBeenCalledWith(
				expect.objectContaining({ variant: "destructive" }),
			),
		);
		// The foreign credential is refused.
		expect(fetchMock).not.toHaveBeenCalled();

		// The sign-in this browser started is still in flight.
		expect(readPendingGoogleRequest()?.state).toBe("state-abc");

		// And the credential that comes back for it is accepted.
		cleanup();
		window.location.hash = "#id_token=google-id-token&state=state-abc";
		renderHandler();
		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
	});

	it("survives a page load with no credential at all", async () => {
		// The user clicked Google and is on their way to it. They land back
		// on the site -- the root, a sub-route -- before the credential
		// arrives, or the credential arrives in another tab.
		rememberPendingGoogleRequest("state-abc");

		renderHandler();
		await settle();

		expect(readPendingGoogleRequest()?.state).toBe("state-abc");

		cleanup();
		window.location.hash = "#id_token=google-id-token&state=state-abc";
		renderHandler();
		await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
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
