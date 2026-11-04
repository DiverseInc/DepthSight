// src/lib/googleOAuth.ts
//
// Google sign-in -- REDIRECT flow.
//
// WHY THIS FILE EXISTS (measured, not assumed)
// --------------------------------------------
// The login page used to render <GoogleLogin> from @react-oauth/google, which
// runs the Google Identity Services (GIS) POPUP flow. That popup was what hung
// for users. Two measured facts decide how the redirect flow has to be built:
//
// 1. @react-oauth/google@0.13.5 has NO `usePopup` prop. A case-insensitive
//    search for "usePopup" across the whole installed package returns NO
//    matches. Its props are `IdConfiguration` (minus client_id/callback) plus
//    `GsiButtonConfiguration` (dist/index.d.ts:351-357). `usePopup` belonged
//    to the previous `react-google-login` package, not to this one.
//
// 2. The library's own "redirect UX" cannot work on this deployment. It is
//    implemented in Google's client script (accounts.google.com/gsi/client),
//    in the function `Dv`:
//
//        b={as:..., client_id:..., scope:"openid email profile",
//           response_type:"id_token", nonce:..., gsiwebsdk:"gis_attributes",
//           redirect_uri:a.o.login_uri, response_mode:"form_post", display:"page"}
//
//    `response_mode:"form_post"` means Google POSTs the credential to
//    `login_uri` as a form; the receiving page must read the POSTed
//    `credential` field (the same script: `os=function(a,b,c){b.credential=c
//    ||"";...}` and `ps=function(a,b){return function(c){c&&c.credential?
//    os(a,b||{},c.credential):...}}`). Google also REQUIRES `login_uri` for
//    that mode:
//        if(a.login_uri){...} else a.ux_mode==="redirect"&&_.H("Missing
//        required login_uri parameter for the redirect flow.");
//
//    The only redirect URI registered on this OAuth client is the bare origin
//    https://depthsight.diverseinc.net, and that origin is served as a static
//    SPA (frontend/nginx.conf: `try_files $uri $uri/ /index.html`). A POST
//    there is answered by nginx's static module with 405, so the SPA never
//    runs and the credential would be lost.
//
// So the redirect is performed the way Google accepts for this client: the
// OpenID Connect IMPLICIT flow (`response_type=id_token`) against
// https://accounts.google.com/o/oauth2/v2/auth. That flow returns the Google
// ID token in the URL FRAGMENT of the redirect URI, which is exactly what
// lets the sign-in complete on the site root with no server-side endpoint.
//
// Measured against this OAuth client on 2026-11-03 (GET, no session):
//   response_type=id_token + redirect_uri=https://depthsight.diverseinc.net
//       -> 302 to accounts.google.com  (ACCEPTED, no error)
//   response_type=token   + the same redirect_uri
//       -> 400 invalid_request ("Parameter not allowed for this message type")
//   redirect_uri=https://depthsight.diverseinc.net/login
//       -> 400 redirect_uri_mismatch
//
// The backend (api/routes/auth.py:200) verifies the ID token with
// `id_token.verify_oauth2_token(payload.token, ...)`, so the token type the
// implicit flow returns is the same token type the old popup flow sent.

/**
 * Google OAuth client id. Public value, baked in at build time.
 *
 * It is read on demand rather than cached in a module constant, so the value
 * follows the environment the app is running in.
 */
export const getGoogleClientId = (): string =>
	import.meta.env.VITE_GOOGLE_CLIENT_ID || "";

/**
 * The ONLY redirect URI registered on the Google OAuth client.
 *
 * It is the bare origin -- no trailing slash, no path. Google compares the
 * redirect URI byte-for-byte, so `https://depthsight.diverseinc.net/` and
 * `https://depthsight.diverseinc.net/login` are both rejected with
 * `redirect_uri_mismatch` (measured 2026-11-03). Changing this value
 * requires registering a new URI in Google Cloud first, which is out of scope
 * for this change.
 */
export const GOOGLE_REDIRECT_URI = "https://depthsight.diverseinc.net";

/**
 * The scopes requested. Unchanged from what the GIS button asked for
 * (`scope:"openid email profile"` in the Google client script).
 */
export const GOOGLE_SCOPE = "openid email profile";

/** Google's OAuth 2.0 authorization endpoint. */
export const GOOGLE_AUTHORIZE_ENDPOINT =
	"https://accounts.google.com/o/oauth2/v2/auth";

/**
 * Key holding the sign-in request that is currently in flight. It carries the
 * `state` value that must come back with the credential, which is what proves
 * the response belongs to a sign-in THIS browser started.
 *
 * It is written to TWO stores, on purpose:
 *
 *   * `localStorage` -- the SHARED copy. A sign-in does not always come back
 *     in the tab that started it: the user may open a second tab, reopen a
 *     closed one, duplicate one, or come back through a restored session.
 *     `sessionStorage` is PER TAB, so a marker that only lived there was
 *     missing in every one of those cases and the LEGITIMATE sign-in was
 *     rejected -- silently, which is the failure this file exists to
 *     prevent.
 *
 *   * `sessionStorage` -- the PER-TAB copy. It records "THIS tab started a
 *     sign-in", which is what lets the tab that started it report a sign-in
 *     that came back without a credential, without every other open tab
 *     reporting a failure that is not its own.
 */
export const GOOGLE_OAUTH_PENDING_KEY = "depthsight:google-oauth-pending";

/**
 * Key holding the one-time values that have ALREADY been sent to the server.
 *
 * The value is the OAuth `state`: a random value this browser generated to
 * tie a response to a request. It is not a credential, so keeping it here
 * exposes nothing that was not already in the URL.
 */
export const GOOGLE_OAUTH_SUBMITTED_KEY = "depthsight:google-oauth-submitted";

/**
 * Query parameters that belong to an OAuth round-trip and must not be left
 * behind in the address bar. The ID token is a bearer credential: leaving it
 * in the URL leaves it in the browser history and in any referrer header.
 */
const OAUTH_QUERY_KEYS = [
	"code",
	"credential",
	"id_token",
	"error",
	"error_description",
	"state",
	"scope",
	"authuser",
	"prompt",
	"hd",
];

const randomToken = (): string => {
	const source = globalThis.crypto;
	if (source && typeof source.getRandomValues === "function") {
		const bytes = new Uint8Array(16);
		source.getRandomValues(bytes);
		return Array.from(bytes, (byte) =>
			byte.toString(16).padStart(2, "0"),
		).join("");
	}
	return `${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
};

export interface GoogleAuthorizeRequest {
	/** The URL the browser must be sent to. */
	url: string;
	/** The `state` value, which must come back with the credential. */
	state: string;
}

/**
 * Build the authorization request that starts the redirect flow.
 */
export const createGoogleAuthorizeRequest = (
	clientId: string,
	redirectUri: string = GOOGLE_REDIRECT_URI,
): GoogleAuthorizeRequest => {
	const state = randomToken();
	const params = new URLSearchParams({
		client_id: clientId,
		redirect_uri: redirectUri,
		response_type: "id_token",
		scope: GOOGLE_SCOPE,
		state,
	});
	return { url: `${GOOGLE_AUTHORIZE_ENDPOINT}?${params.toString()}`, state };
};

export type GoogleRedirectPayload =
	| { kind: "credential"; credential: string; state: string | null }
	| { kind: "error"; error: string; description: string; state: string | null }
	| { kind: "empty" };

/**
 * Read the result of the redirect out of the URL.
 *
 * The credential normally arrives in the fragment (the implicit flow puts it
 * there). The query string is read as well so an error that arrives there is
 * still reported instead of being silently ignored.
 */
export const parseGoogleRedirect = (
	hash: string,
	search: string,
): GoogleRedirectPayload => {
	const fragment = new URLSearchParams(hash.replace(/^#/, ""));
	const query = new URLSearchParams(search.replace(/^\?/, ""));

	const credential =
		fragment.get("id_token") ||
		fragment.get("credential") ||
		query.get("id_token") ||
		query.get("credential");
	if (credential) {
		return {
			kind: "credential",
			credential,
			state: fragment.get("state") ?? query.get("state"),
		};
	}

	const error = fragment.get("error") || query.get("error");
	if (error) {
		return {
			kind: "error",
			error,
			description:
				fragment.get("error_description") ||
				query.get("error_description") ||
				"",
			state: fragment.get("state") ?? query.get("state"),
		};
	}

	return { kind: "empty" };
};

/**
 * Remove the OAuth round-trip parameters from a query string, keeping every
 * other parameter (a referral code, for example).
 */
export const stripOAuthParams = (search: string): string => {
	const params = new URLSearchParams(search);
	let changed = false;
	for (const key of OAUTH_QUERY_KEYS) {
		if (params.has(key)) {
			params.delete(key);
			changed = true;
		}
	}
	if (!changed) return search;
	const stripped = params.toString();
	return stripped ? `?${stripped}` : "";
};

/**
 * Remove the credential from the address bar, keeping the path and any
 * unrelated query parameters. The fragment is removed with `replaceState`, so
 * the credential never lands in the browser history.
 */
export const clearGoogleRedirectParams = (
	history: History,
	location: Location,
): void => {
	const search = stripOAuthParams(location.search);
	if (!location.hash && search === location.search) return;
	history.replaceState(history.state, "", `${location.pathname}${search}`);
};

/**
 * Where a pending sign-in request was found.
 *
 * "tab" means THIS tab started the sign-in (the per-tab copy).
 * "shared" means another tab in this browser started it (the shared copy).
 */
export type PendingGoogleRequestScope = "tab" | "shared";

export interface PendingGoogleRequest {
	state: string;
	/**
	 * The one-time value for THIS attempt.
	 *
	 * It is generated fresh on every click, so the server can consume it
	 * exactly once without a second attempt of the same sign-in ever
	 * being refused. The OAuth `state` cannot play that role: it is built
	 * once per page render, so a second click on the same page would reuse
	 * a value the server has already consumed.
	 */
	nonce: string;
	at: number;
	/**
	 * True when the request is older than PENDING_MAX_AGE_MS.
	 *
	 * This is a SOFT bound: it says the request is probably abandoned. It does
	 * NOT make the request unusable. The age is never the reason a credential
	 * is rejected -- the STATE MATCH is (see GoogleRedirectHandler).
	 */
	expired: boolean;
	/** Which store the request was found in. */
	scope: PendingGoogleRequestScope;
}

/**
 * The result of looking the in-flight sign-in request up.
 *
 * "missing" and "unavailable" are DIFFERENT outcomes and must not be
 * confused. A missing marker means "no sign-in was started here" -- the
 * credential is rejected. "unavailable" means the browser is blocking
 * storage, so the app cannot even tell -- the credential is rejected, and
 * the user is told WHY, instead of being met with silence.
 */
export type PendingGoogleLookup =
	| { kind: "found"; request: PendingGoogleRequest }
	| { kind: "missing" }
	| { kind: "unavailable"; reason: string };

/**
 * A sign-in that has not come back within this window is treated as probably
 * abandoned. Without it, a marker left behind by a closed tab could make a
 * later, unrelated visit to the site root report a sign-in failure.
 *
 * It is a SOFT bound and it only decides that one thing. It must never be
 * the reason a credential whose `state` matches is rejected: a user who
 * takes longer than this at Google's consent screen -- a slow device, a
 * hesitation, a phone in the background, a long MFA prompt -- comes back
 * with a perfectly valid credential.
 */
export const PENDING_MAX_AGE_MS = 15 * 60 * 1000;

/**
 * Remember that a sign-in is in flight, so the credential that comes back can
 * be tied to this request.
 *
 * Returns FALSE when NEITHER store could be written: the browser is blocking
 * storage (private mode with storage disabled, blocked third-party storage, a
 * hardened profile). The caller MUST then fail loudly. A sign-in whose marker
 * cannot be recorded is a sign-in that is guaranteed to be rejected on the way
 * back, and it must never start silently.
 */
export const rememberPendingGoogleRequest = (state: string): boolean => {
	const payload = JSON.stringify({
		state,
		nonce: randomToken(),
		at: Date.now(),
	});
	let stored = false;
	// A new attempt supersedes the previous one for this `state`: the
	// server consumes the one-time value exactly once, so a second attempt
	// must carry a NEW one.
	clearGoogleSignInSubmission(state);
	try {
		// The SHARED copy: this is what makes the sign-in verifiable in
		// another tab.
		localStorage.setItem(GOOGLE_OAUTH_PENDING_KEY, payload);
		stored = true;
	} catch {
		// The shared store is unavailable. The per-tab store is the fallback.
	}
	try {
		// The PER-TAB copy: this is what lets THIS tab report a sign-in that
		// came back without a credential.
		sessionStorage.setItem(GOOGLE_OAUTH_PENDING_KEY, payload);
		stored = true;
	} catch {
		// The per-tab store is unavailable too.
	}
	return stored;
};

const parsePendingRecord = (
	raw: string,
	scope: PendingGoogleRequestScope,
): PendingGoogleRequest | null => {
	try {
		const parsed = JSON.parse(raw) as {
			state?: unknown;
			nonce?: unknown;
			at?: unknown;
		};
		if (!parsed || typeof parsed.state !== "string") return null;
		const at = typeof parsed.at === "number" ? parsed.at : 0;
		return {
			state: parsed.state,
			nonce: typeof parsed.nonce === "string" ? parsed.nonce : parsed.state,
			at,
			expired: Date.now() - at > PENDING_MAX_AGE_MS,
			scope,
		};
	} catch {
		return null;
	}
};

/**
 * Look the sign-in request that is in flight up, if there is one.
 *
 * The PER-TAB copy wins: it is the one that says "this tab started the
 * sign-in", and it is the only copy that still carries that meaning when two
 * tabs are signing in at the same time. The SHARED copy is the fallback that
 * makes a sign-in started in one tab verifiable in another.
 *
 * This READS the marker; it never discards it, however old it is. The caller
 * is the one that consumes it, and it is the `state` -- not the age -- that
 * decides whether a credential is accepted.
 */
export const lookupPendingGoogleRequest = (): PendingGoogleLookup => {
	let sharedUsable = true;
	let sharedRaw: string | null = null;
	try {
		sharedRaw = localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY);
	} catch {
		sharedUsable = false;
	}

	let tabRaw: string | null = null;
	let tabUsable = true;
	try {
		tabRaw = sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY);
	} catch {
		tabUsable = false;
	}

	const tab = tabRaw ? parsePendingRecord(tabRaw, "tab") : null;
	if (tab) return { kind: "found", request: tab };

	const shared = sharedRaw ? parsePendingRecord(sharedRaw, "shared") : null;
	if (shared) return { kind: "found", request: shared };

	if (!sharedUsable && !tabUsable) {
		return {
			kind: "unavailable",
			reason:
				"This browser is blocking site storage, so the sign-in cannot be verified.",
		};
	}
	return { kind: "missing" };
};

/**
 * Read the sign-in request that is in flight, if there is one.
 *
 * This is the simple form of `lookupPendingGoogleRequest`, for callers that
 * only care whether there is a marker at all.
 */
export const readPendingGoogleRequest = (): PendingGoogleRequest | null => {
	const found = lookupPendingGoogleRequest();
	return found.kind === "found" ? found.request : null;
};

/**
 * Remove the marker from BOTH stores.
 */
export const clearPendingGoogleRequest = (): void => {
	try {
		localStorage.removeItem(GOOGLE_OAUTH_PENDING_KEY);
	} catch {
		// Nothing to clear.
	}
	try {
		sessionStorage.removeItem(GOOGLE_OAUTH_PENDING_KEY);
	} catch {
		// Nothing to clear.
	}
};

/**
 * Claim the one-time value for THIS sign-in attempt.
 *
 * Returns TRUE when this call is the one that claimed it, and FALSE when it
 * had already been claimed -- which means the credential carrying it has
 * already been sent and must not be sent again.
 *
 * The claim lives in `localStorage`, so it survives a remount of the
 * component AND a full reload of the page. A React ref survives neither, and
 * the credential lives in the URL fragment, which a reload keeps.
 *
 * This is DEFENCE IN DEPTH. The guarantee that one redirect produces exactly
 * one session is enforced on the server, which consumes the same one-time
 * value; the claim only stops the browser from making a request that the
 * server is going to refuse.
 */
const readSubmittedStates = (): string[] => {
	try {
		const raw = localStorage.getItem(GOOGLE_OAUTH_SUBMITTED_KEY);
		const parsed = raw ? (JSON.parse(raw) as { states?: unknown }) : null;
		return Array.isArray(parsed?.states)
			? (parsed!.states as unknown[]).filter(
					(value): value is string => typeof value === "string",
				)
			: [];
	} catch {
		return [];
	}
};

const writeSubmittedStates = (states: string[]): void => {
	try {
		localStorage.setItem(
			GOOGLE_OAUTH_SUBMITTED_KEY,
			// Only the most recent values are kept. A dropped claim can only
			// ever let an ANCIENT one-time value be sent again, and the
			// server has long since forgotten it.
			JSON.stringify({ states: states.slice(-20) }),
		);
	} catch {
		// Storage is blocked. The server refuses a second attempt with the
		// same one-time value, so the guarantee still holds.
	}
};

/**
 * Claim the sign-in attempt for this `state`.
 *
 * Returns TRUE when this call is the one that claimed it, and FALSE when it
 * had already been claimed -- which means the credential that carries it has
 * already been sent and must not be sent again.
 *
 * The claim lives in `localStorage`, so it survives a remount of the
 * component AND a full reload of the page. A React ref survives neither, and
 * the credential lives in the URL fragment, which a reload keeps.
 *
 * This is DEFENCE IN DEPTH. The guarantee that one redirect produces exactly
 * one session is enforced on the server, which consumes the one-time value
 * once; the claim only stops the browser from making a request that the
 * server is going to refuse.
 */
export const claimGoogleSignInSubmission = (state: string): boolean => {
	if (!state) return true;
	const seen = readSubmittedStates();
	if (seen.includes(state)) return false;
	writeSubmittedStates([...seen, state]);
	return true;
};

/**
 * Forget the claim for this `state`.
 *
 * A new attempt supersedes the previous one: the server consumes the
 * one-time value exactly once, so an attempt that has already been sent must
 * not stop the user from signing in again.
 */
export const clearGoogleSignInSubmission = (state: string): void => {
	if (!state) return;
	const seen = readSubmittedStates();
	if (!seen.includes(state)) return;
	writeSubmittedStates(seen.filter((value) => value !== state));
};
