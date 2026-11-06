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
 * ONE sign-in attempt, as it is kept in a store.
 *
 * A browser can have SEVERAL sign-ins in flight at the same time: a
 * ctrl/cmd+click opens a sign-in in another tab while this tab starts one
 * of its own. The stores therefore keep a LIST of attempts, and every
 * attempt carries the `state` of the sign-in it belongs to.
 */
interface PendingAttempt {
	state: string;
	nonce: string;
	at: number;
}

/**
 * How the attempts are kept.
 *
 * The value under GOOGLE_OAUTH_PENDING_KEY is a JSON object:
 *
 *   {
 *     "state": "...", "nonce": "...", "at": 1730000000000,
 *     "attempts": [ { "state": "...", "nonce": "...", "at": ... } ]
 *   }
 *
 * The top-level fields are the MOST RECENT attempt; "attempts" holds the
 * EARLIER ones, oldest first. A value without "attempts" is a single
 * attempt -- which is what the app wrote before it could hold more than
 * one, and what a reader that knows about one attempt still understands.
 */

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
 * How many sign-in attempts a store keeps.
 *
 * A browser can have more than one sign-in in flight, so the marker is a
 * LIST of attempts rather than a single slot. The list is bounded so that
 * attempts that are never completed cannot grow it without limit -- the
 * same bound the list of already-consumed one-time values uses.
 */
const MAX_PENDING_ATTEMPTS = 20;

/**
 * Where the browser is going to follow the Google link.
 *
 * "tab"    -- THIS tab follows the link: a plain click, or the keyboard.
 *             Both stores are written. The per-tab copy says "this tab
 *             started the sign-in", which is what lets that tab report a
 *             sign-in that came back without a credential -- and what keeps
 *             two tabs signing in at the same time apart.
 *
 * "shared" -- the link may be followed SOMEWHERE ELSE: a middle click, the
 *             context menu's "Open link in new tab", a ctrl/cmd+click.
 *             Only the shared copy is written, because a tab that is NOT
 *             following the link must not claim the sign-in: the per-tab
 *             copy is what says "this tab started the sign-in", and a tab
 *             that did not follow it has no failure of its own to report.
 *             The shared copy is enough for the verification, because it is
 *             what the tab the sign-in comes back in reads.
 */
export type GoogleSignInFollow = "tab" | "shared";

/**
 * Remember that a sign-in is in flight, so the credential that comes back can
 * be tied to this request.
 *
 * `follow` says where the browser is going to follow the link -- see
 * `GoogleSignInFollow`.
 *
 * Returns FALSE when the marker could not be written where it is NEEDED:
 * the browser is blocking that store (private mode with storage disabled,
 * blocked third-party storage, a hardened profile). The caller MUST then
 * fail loudly. A sign-in whose marker cannot be recorded is a sign-in that
 * is guaranteed to be rejected on the way back, and it must never start
 * silently.
 */
export const rememberPendingGoogleRequest = (
	state: string,
	follow: GoogleSignInFollow = "tab",
): boolean => {
	const attempt: PendingAttempt = {
		state,
		nonce: randomToken(),
		at: Date.now(),
	};
	let stored = false;
	// A new attempt supersedes the previous one for this `state`: the
	// server consumes the one-time value exactly once, so a second attempt
	// must carry a NEW one.
	clearGoogleSignInSubmission(state);
	// The SHARED copy: this is what makes the sign-in verifiable in
	// another tab.
	stored = addPendingAttempt("shared", attempt) || stored;
	if (follow === "tab") {
		// The PER-TAB copy: this is what lets THIS tab report a sign-in
		// that came back without a credential.
		stored = addPendingAttempt("tab", attempt) || stored;
	}
	return stored;
};

/**
 * Add an attempt to a store.
 *
 * Returns FALSE when the store could not be written, which is what makes
 * the caller fail loudly instead of starting a sign-in that can never be
 * verified.
 *
 * An attempt for the SAME `state` is REPLACED: a new attempt supersedes
 * the previous one for that state, because the server consumes the one-time
 * value exactly once. An attempt for a DIFFERENT state is KEPT: it belongs
 * to another sign-in that is still in flight, and destroying it would
 * refuse that sign-in when its credential comes back.
 */
const addPendingAttempt = (
	scope: PendingGoogleRequestScope,
	attempt: PendingAttempt,
): boolean => {
	const attempts = readPendingAttempts(scope);
	if (attempts === null) return false; // the store cannot be read
	const kept = attempts.filter((entry) => entry.state !== attempt.state);
	kept.push(attempt);
	return writePendingAttempts(scope, kept.slice(-MAX_PENDING_ATTEMPTS));
};

/**
 * The attempts kept in ONE store, oldest first.
 *
 * Returns NULL when the store cannot be READ. That is different from
 * "there is no attempt in it", and the two must not be confused.
 */
const readPendingAttempts = (
	scope: PendingGoogleRequestScope,
): PendingAttempt[] | null => {
	try {
		const raw =
			scope === "tab"
				? sessionStorage.getItem(GOOGLE_OAUTH_PENDING_KEY)
				: localStorage.getItem(GOOGLE_OAUTH_PENDING_KEY);
		return raw ? parsePendingAttempts(raw) : [];
	} catch {
		// The store cannot be read.
		return null;
	}
};

/**
 * Write the attempts of ONE store.
 *
 * Returns FALSE when the store could not be written.
 */
const writePendingAttempts = (
	scope: PendingGoogleRequestScope,
	attempts: PendingAttempt[],
): boolean => {
	try {
		if (attempts.length === 0) {
			if (scope === "tab") {
				sessionStorage.removeItem(GOOGLE_OAUTH_PENDING_KEY);
			} else {
				localStorage.removeItem(GOOGLE_OAUTH_PENDING_KEY);
			}
			return true;
		}
		const newest = attempts[attempts.length - 1];
		const earlier = attempts.slice(0, attempts.length - 1);
		const payload: Record<string, unknown> = {
			state: newest.state,
			nonce: newest.nonce,
			at: newest.at,
		};
		if (earlier.length > 0) payload.attempts = earlier;
		const serialized = JSON.stringify(payload);
		if (scope === "tab") {
			sessionStorage.setItem(GOOGLE_OAUTH_PENDING_KEY, serialized);
		} else {
			localStorage.setItem(GOOGLE_OAUTH_PENDING_KEY, serialized);
		}
		return true;
	} catch {
		// The store cannot be written.
		return false;
	}
};

/**
 * Parse ONE attempt out of a stored value.
 *
 * Returns NULL when the value does not carry a usable attempt.
 */
const toPendingAttempt = (raw: unknown): PendingAttempt | null => {
	if (!raw || typeof raw !== "object") return null;
	const parsed = raw as {
		state?: unknown;
		nonce?: unknown;
		at?: unknown;
	};
	if (typeof parsed.state !== "string") return null;
	const at = typeof parsed.at === "number" ? parsed.at : 0;
	return {
		state: parsed.state,
		nonce: typeof parsed.nonce === "string" ? parsed.nonce : parsed.state,
		at,
	};
};

/**
 * Parse the attempts out of a stored value, oldest first.
 *
 * A value that cannot be read at all holds NO attempt -- not an attempt
 * that is broken, and not one that may be discarded because of its age.
 */
const parsePendingAttempts = (raw: string): PendingAttempt[] => {
	try {
		const parsed = JSON.parse(raw) as {
			state?: unknown;
			nonce?: unknown;
			at?: unknown;
			attempts?: unknown;
		};
		if (!parsed || typeof parsed.state !== "string") return [];
		const attempts: PendingAttempt[] = [];
		if (Array.isArray(parsed.attempts)) {
			for (const attempt of parsed.attempts) {
				const record = toPendingAttempt(attempt);
				if (record) attempts.push(record);
			}
		}
		const newest = toPendingAttempt(parsed);
		if (newest) attempts.push(newest);
		return attempts;
	} catch {
		return [];
	}
};

/**
 * The attempt with this `state`, if the store holds one.
 *
 * The MOST RECENT attempt with that state wins: a new attempt supersedes
 * the previous one for the same state.
 */
const findPendingAttempt = (
	attempts: PendingAttempt[],
	state: string,
): PendingAttempt | null => {
	for (let index = attempts.length - 1; index >= 0; index -= 1) {
		if (attempts[index].state === state) return attempts[index];
	}
	return null;
};

/**
 * The request as the caller sees it: the attempt, and how old it is.
 *
 * The age is a SOFT bound. It says the attempt is probably abandoned; it
 * never makes it unusable, and it is never the reason a credential is
 * rejected -- the STATE MATCH is (see GoogleRedirectHandler).
 */
const toPendingRequest = (
	attempt: PendingAttempt,
	scope: PendingGoogleRequestScope,
): PendingGoogleRequest => {
	return {
		state: attempt.state,
		nonce: attempt.nonce,
		at: attempt.at,
		expired: Date.now() - attempt.at > PENDING_MAX_AGE_MS,
		scope,
	};
};

/**
 * Look the sign-in request that is in flight up, if there is one.
 *
 * `state` says WHICH attempt is wanted. The credential that comes back
 * carries the `state` of the attempt it belongs to, and that is what ties
 * it to a sign-in this browser started. When several sign-ins are in flight
 * at the same time, each credential finds its OWN attempt: without it, a
 * credential could be matched to somebody else's sign-in, or refused while
 * its own sign-in is perfectly in flight.
 *
 * Without a `state`, the newest attempt is returned.
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
export const lookupPendingGoogleRequest = (
	state?: string | null,
): PendingGoogleLookup => {
	const shared = readPendingAttempts("shared");
	const tab = readPendingAttempts("tab");
	const sharedUsable = shared !== null;
	const tabUsable = tab !== null;
	const sharedAttempts = shared ?? [];
	const tabAttempts = tab ?? [];

	if (!sharedUsable && !tabUsable) {
		return {
			kind: "unavailable",
			reason:
				"This browser is blocking site storage, so the sign-in cannot be verified.",
		};
	}

	if (state) {
		// The attempt THIS response belongs to.
		const tabMatch = findPendingAttempt(tabAttempts, state);
		if (tabMatch) {
			return { kind: "found", request: toPendingRequest(tabMatch, "tab") };
		}
		const sharedMatch = findPendingAttempt(sharedAttempts, state);
		if (sharedMatch) {
			return {
				kind: "found",
				request: toPendingRequest(sharedMatch, "shared"),
			};
		}
		return { kind: "missing" };
	}

	// No state: the newest attempt there is.
	const newestTab = tabAttempts[tabAttempts.length - 1];
	if (newestTab) {
		return { kind: "found", request: toPendingRequest(newestTab, "tab") };
	}
	const newestShared = sharedAttempts[sharedAttempts.length - 1];
	if (newestShared) {
		return {
			kind: "found",
			request: toPendingRequest(newestShared, "shared"),
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
 * Remove ONE attempt from ONE store.
 *
 * Only the attempt with this `state` is removed, and only from the store
 * it is named in. Another sign-in that is still in flight keeps its own
 * attempt -- and so does the copy of THIS attempt in the other store: the
 * `state` is what proves a credential belongs to a sign-in, and the
 * one-time value is claimed separately (see claimGoogleSignInSubmission).
 */
const removePendingAttempt = (
	scope: PendingGoogleRequestScope,
	state: string,
): void => {
	const attempts = readPendingAttempts(scope);
	if (attempts === null) return; // the store cannot be read
	const kept = attempts.filter((entry) => entry.state !== state);
	if (kept.length === attempts.length) return; // nothing to remove
	writePendingAttempts(scope, kept);
};

/**
 * Remove the attempt that was CONSUMED (or that failed).
 *
 * Only that attempt is removed, and only from the store it was found in.
 * Completing ONE sign-in must never destroy a DIFFERENT sign-in that is
 * still in flight: the credential that comes back for it would then find
 * no record at all and be refused.
 */
export const clearPendingGoogleRequestRecord = (
	request: PendingGoogleRequest,
): void => {
	removePendingAttempt(request.scope, request.state);
};

/**
 * Drop this tab's claim on an EXPIRED attempt.
 *
 * An attempt older than PENDING_MAX_AGE_MS is probably abandoned, and a
 * later, unrelated visit to the site root must not report a sign-in failure
 * for it. So the PER-TAB copy is dropped.
 *
 * The SHARED copy is left alone. It is what makes the credential verifiable
 * in the tab it comes back in, and the AGE of an attempt is never a reason
 * to refuse a credential that carries a matching state: the Google ID token
 * is valid for about an hour, which is longer than this window.
 */
export const clearExpiredPendingGoogleRequest = (
	request: PendingGoogleRequest,
): void => {
	if (request.scope !== "tab") return;
	removePendingAttempt("tab", request.state);
};

/**
 * Drop the PER-TAB copy of the marker, keeping the SHARED one.
 *
 * The shared copy is what makes the sign-in verifiable in another tab -- and
 * in this tab too, once the per-tab copy is gone. So the per-tab copy can be
 * dropped without losing the verification, AS LONG AS the shared copy really
 * carries the same request. When it does not (another tab's sign-in, or the
 * shared store could not be written at all), the per-tab copy is the only
 * proof there is and it stays.
 *
 * It is used when a failure is reported: the sign-in is not going to be
 * reported twice, but the credential may still be on its way and it must
 * still be verifiable when it arrives.
 */
export const clearPendingGoogleRequestInThisTab = (state: string): void => {
	const shared = readPendingAttempts("shared");
	if (shared === null) return; // the shared store cannot be read
	if (!findPendingAttempt(shared, state)) return;
	removePendingAttempt("tab", state);
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
