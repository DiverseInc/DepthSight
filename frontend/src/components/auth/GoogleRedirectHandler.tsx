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

import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useToast } from "@/components/ui/use-toast";
import { useGoogleLoginMutation } from "@/hooks/useGoogleLoginMutation";
import {
	claimGoogleSignInSubmission,
	clearGoogleRedirectParams,
	clearPendingGoogleRequest,
	clearPendingGoogleRequestInThisTab,
	lookupPendingGoogleRequest,
	parseGoogleRedirect,
} from "@/lib/googleOAuth";

export function GoogleRedirectHandler() {
	const { toast } = useToast();
	const { t } = useTranslation("login");
	const navigate = useNavigate();
	// `mutate` is stable across renders, unlike the whole mutation result.
	const { mutate } = useGoogleLoginMutation();

	useEffect(() => {
		const payload = parseGoogleRedirect(
			window.location.hash,
			window.location.search,
		);
		const pending = lookupPendingGoogleRequest();

		const fail = (description: string) => {
			toast({
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
				clearPendingGoogleRequest();
			}
		};

		// Nothing came back. If a sign-in was in flight, that is a failure the
		// user must SEE -- not a silent landing on the dashboard.
		if (payload.kind === "empty") {
			if (pending.kind !== "found") return; // a normal page load
			if (pending.request.expired) {
				// An abandoned attempt. Clean the marker up, but do not report
				// a failure for a later, unrelated visit to the site root.
				clearPendingGoogleRequest();
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
			// There is no request to compare against: the marker was lost or
			// already cleaned up, most often because the sign-in took longer
			// than the window. Say that, instead of calling the credential
			// unverified.
			fail(t("googleSignInExpired"));
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

		clearPendingGoogleRequest();
		mutate({
			token: payload.credential,
			// The one-time value the server consumes exactly once. It is the
			// one from the marker, not the OAuth `state`: the `state` is
			// built once per page render, so a second attempt on the same
			// page would carry a value the server has already consumed.
			signin_nonce: pending.request.nonce,
		});
		// The mutation owns the success and failure toasts.
	}, [mutate, navigate, t, toast]);

	return null;
}

export default GoogleRedirectHandler;
