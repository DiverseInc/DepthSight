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
 * sessionStorage key holding the sign-in request that is currently in flight.
 * It carries the `state` value that must come back with the credential, which
 * is what proves the response belongs to a sign-in THIS browser started.
 */
export const GOOGLE_OAUTH_PENDING_KEY = "depthsight:google-oauth-pending";

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

export interface PendingGoogleRequest {
	state: string;
	at: number;
}

/**
 * A sign-in that has not come back within this window is treated as
 * abandoned. Without it, a marker left behind by a closed tab could make a
 * later, unrelated visit to the site root report a sign-in failure.
 */
export const PENDING_MAX_AGE_MS = 15 * 60 * 1000;

/**
 * Remember that a sign-in is in flight, so the credential that comes back can
 * be tied to this request.
 */
export const rememberPendingGoogleRequest = (state: string): void => {
	try {
		sessionStorage.setItem(
			GOOGLE_OAUTH_PENDING_KEY,
			JSON.stringify({ state, at: Date.now() }),
		);
	} catch {
		// Storage is unavailable (private mode, blocked storage). The sign-in
		// then fails the state check below instead of silently succeeding.
	}
};

export const readPendingGoogleRequest = (): PendingGoogleRequest | null => {
	try {
		const raw = sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY);
		if (!raw) return null;
		const parsed = JSON.parse(raw) as { state?: unknown; at?: unknown };
		if (!parsed || typeof parsed.state !== "string") return null;
		const at = typeof parsed.at === "number" ? parsed.at : 0;
		if (Date.now() - at > PENDING_MAX_AGE_MS) {
			clearPendingGoogleRequest();
			return null;
		}
		return { state: parsed.state, at };
	} catch {
		return null;
	}
};

export const clearPendingGoogleRequest = (): void => {
	try {
		sessionStorage.removeItem(GOOGLE_OAUTH_PENDING_KEY);
	} catch {
		// Nothing to clear.
	}
};
