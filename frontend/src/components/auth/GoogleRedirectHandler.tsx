// src/components/auth/GoogleRedirectHandler.tsx
//
// The route-INDEPENDENT half of the Google redirect flow.
//
// Google sends the user back to the registered redirect URI, which is the
// BARE ORIGIN (https://depthsight.diverseinc.net) -- see
// src/lib/googleOAuth.ts for why. That means the sign-in completes on the
// SITE ROOT, not on /login, so the credential cannot be handled by the login
// page: the login page is not even mounted when the user comes back.
//
// This component is mounted inside <AuthProvider> and above <Routes> (see
// src/App.tsx), so it runs on EVERY route, including the site root.
//
// It does four things, in this order:
//   1. reads the result of the redirect out of the URL,
//   2. REMOVES the credential from the address bar with history.replaceState
//      (the ID token is a bearer credential -- it must not stay in the URL or
//      in the browser history),
//   3. checks that the credential belongs to a sign-in this browser started
//      (the `state` value), and
//   4. hands the credential to the Google sign-in mutation, which posts it to
//      /api/v1/auth/google.
//
// ONE redirect must produce exactly ONE session. That is enforced on the
// SERVER, which consumes the one-time value once (see api/google_signin.py).
// The client guard here is defence in depth: the one-time value is CLAIMED in
// storage before the credential is sent, and the claim lives in localStorage,
// so it survives both a remount of this component and a full reload of the
// page. A React ref survives neither, and the credential lives in the URL
// fragment, which a reload keeps.
//
// When the redirect comes back WITHOUT a usable credential -- no fragment at
// all, or an error from Google -- the user gets a visible failure instead of
// a silent no-op.

import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useToast } from "@/components/ui/use-toast";
import { useGoogleLoginMutation } from "@/hooks/useGoogleLoginMutation";
import {
	claimGoogleSignInSubmission,
	clearExpiredPendingGoogleRequest,
	clearGoogleRedirectParams,
	clearPendingGoogleRequestInThisTab,
	clearPendingGoogleRequestRecord,
	lookupPendingGoogleRequest,
	parseGoogleRedirect,
} from "@/lib/googleOAuth";

export function GoogleRedirectHandler() {
	const { toast } = useToast();
	const { t } = useTranslation("login");
	const navigate = useNavigate();
	// `mutate` is stable across renders, unlike the whole mutation result.
	const { mutate } = useGoogleLoginMutation();

	// The toast, held in a ref so that the effect below does not depend on
	// its IDENTITY.
	//
	// A toast re-renders this component, and the effect re-runs whenever one
	// of its dependencies changes. Were `toast` a dependency and the hook
	// handed back a NEW function on every render, the toast and the effect
	// would feed each other: the re-render would run the effect again, and
	// the tab would freeze with the CPU at 100%. The ref makes the effect
	// independent of that identity, so the loop is impossible whatever the
	// hook returns.
	const toastRef = useRef(toast);
	useEffect(() => {
		toastRef.current = toast;
	}, [toast]);

	// The response this handler has already dealt with.
	//
	// The effect below can run more than once for the same response: a
	// navigation changes `navigate`, and a re-render changes the render.
	// The response must be reported ONCE and acted on ONCE -- reporting it
	// twice is a lie, and acting on it twice sends the credential twice.
	// Remembering it makes the effect IDEMPOTENT.
	const handledRef = useRef<string | null>(null);

	useEffect(() => {
		const payload = parseGoogleRedirect(
			window.location.hash,
			window.location.search,
		);
		// Every sign-in that is in flight in this browser. It is what tells
		// "no sign-in was started here" apart from "a DIFFERENT sign-in is
		// in flight" -- the two are refused with different messages.
		const inFlight = lookupPendingGoogleRequest();
		// The attempt THIS response belongs to. A credential or an error
		// carries the `state` of the attempt it belongs to, and that is what
		// ties it to a sign-in this browser started. When several sign-ins
		// are in flight at the same time, each credential finds its OWN
		// attempt -- the marker is not a single slot.
		const pending =
			payload.kind === "empty"
				? inFlight
				: lookupPendingGoogleRequest(payload.state);

		// What this run is ABOUT.
		//
		// A credential or an error carries the `state` of the attempt it
		// belongs to. An empty URL is about the attempt that is still in
		// flight. Two runs that are about the same thing must not report
		// the same thing twice -- see `handledRef` above.
		const responseKey =
			payload.kind === "empty"
				? pending.kind === "found"
					? `pending:${pending.request.state}`
					: "pending:none"
				: `${payload.kind}:${payload.state ?? ""}`;

		// Already dealt with: the response was reported, and the attempt it
		// belongs to was consumed or refused. Doing it again would report a
		// failure that is not news, and -- because a toast re-renders this
		// component -- would feed the effect its own tail.
		if (handledRef.current === responseKey) return;

		const fail = (description: string) => {
			// This response is reported ONCE.
			handledRef.current = responseKey;
			toastRef.current({
				variant: "destructive",
				title: t("toastFailureTitle"),
				description,
			});
			navigate("/login");
		};

		// The marker is cleared ONLY when it is provably the one that belongs
		// to THIS response. A failure that is not ours -- a foreign
		// credential, an unrelated page load -- must not destroy a sign-in
		// that is still in flight: the credential that comes back for it
		// would then find no marker and be refused, and the customer could
		// not sign in at all.
		const clearIfOurs = (state: string | null) => {
			if (!state) return;
			if (pending.kind === "found" && pending.request.state === state) {
				clearPendingGoogleRequestRecord(pending.request);
			}
		};

		// Nothing came back. If a sign-in was in flight, that is a failure the
		// user must SEE -- not a silent landing on the dashboard.
		if (payload.kind === "empty") {
			if (pending.kind !== "found") return; // a normal page load
			if (pending.request.expired) {
				// An abandoned attempt. This tab's claim on it is dropped, so
				// that a later, unrelated visit to the site root does not
				// report a failure that is not its own. The sign-in itself is
				// NOT destroyed: the credential may still be on its way (a
				// Google ID token is valid for about an hour, which is longer
				// than this window), and the age of a request is never the
				// reason a credential that carries a matching state is
				// refused.
				clearExpiredPendingGoogleRequest(pending.request);
				return;
			}
			if (pending.request.scope !== "tab") {
				// Another tab in this browser started the sign-in. This tab
				// must not report a failure that is not its own, and it must
				// NOT clear the shared marker: the tab that started the
				// sign-in still needs it to verify the credential.
				return;
			}
			// The sign-in came back to THIS tab without a credential. It is
			// reported once -- but the marker is NOT destroyed: the
			// credential may still be on its way (a slow Google), and it must
			// still be verifiable when it arrives.
			clearPendingGoogleRequestInThisTab(pending.request.state);
			fail(t("googleSignInFailed"));
			return;
		}

		// A credential or an error is in the URL. Strip it FIRST, before the
		// claim below: the ID token is a BEARER credential, so it must not
		// stay in the address bar or in this tab's history -- not even when
		// the claim says it has already been sent and the handler returns
		// early.
		clearGoogleRedirectParams(window.history, window.location);

		// ONE redirect, ONE session. The one-time value is claimed BEFORE the
		// credential is sent, because a reload -- or a browser session
		// restore -- can bring the credential back, and the claim is the
		// only guard that survives both. A second claim for the same value
		// means this credential has already been sent.
		if (payload.kind === "credential" && payload.state) {
			if (!claimGoogleSignInSubmission(payload.state)) {
				// Already sent. There is nothing left to do, and nothing to
				// report: the sign-in this credential belongs to is under
				// way, and reporting a failure would be a lie.
				return;
			}
		}

		if (payload.kind === "error") {
			// Google's error belongs to THIS sign-in only when it carries our
			// state. Another sign-in's marker must not be destroyed.
			clearIfOurs(payload.state);
			fail(payload.description || t("googleSignInFailed"));
			return;
		}

		// The credential must come back with the `state` this browser sent.
		// Without that check, a crafted link could sign somebody else in.
		//
		// The age of the request is NOT a reason to reject it. A user who
		// takes longer than PENDING_MAX_AGE_MS at Google's consent screen has
		// a perfectly valid credential, and a matching `state` proves the
		// response belongs to a sign-in this browser started.
		if (pending.kind === "unavailable") {
			// The browser is blocking storage, so the marker could not be
			// checked at all. Say THAT, instead of calling the credential
			// unverified.
			fail(t("googleSignInStorageBlocked"));
			return;
		}

		if (pending.kind !== "found") {
			// There is no request to compare against. Either NO sign-in is in
			// flight at all -- the marker was lost or already cleaned up,
			// most often because the sign-in took longer than the window --
			// or a DIFFERENT sign-in is in flight, and this response belongs
			// to that one and not to us. Say which one it is.
			fail(
				inFlight.kind === "found"
					? t("googleSignInUnverified")
					: t("googleSignInExpired"),
			);
			return;
		}

		if (pending.request.state !== payload.state) {
			// A response that does not carry our state is not ours. The
			// marker belongs to a sign-in that may still be in flight, so it
			// is NOT cleared here: the credential that comes back for THAT
			// sign-in must still find it.
			fail(t("googleSignInUnverified"));
			return;
		}

		// The attempt was CONSUMED. Only THAT attempt is removed, and only
		// from the store it was found in: another sign-in that is still in
		// flight keeps its own attempt, and the credential that comes back
		// for it must still find it.
		clearPendingGoogleRequestRecord(pending.request);
		// This response is acted on ONCE.
		handledRef.current = responseKey;
		mutate({
			token: payload.credential,
			// The one-time value the server consumes exactly once. It is the
			// one from the marker, not the OAuth `state`: the `state` is
			// built once per page render, so a second attempt on the same
			// page would carry a value the server has already consumed.
			signin_nonce: pending.request.nonce,
		});
		// The mutation owns the success and failure toasts.
	}, [mutate, navigate, t]);

	return null;
}

export default GoogleRedirectHandler;
