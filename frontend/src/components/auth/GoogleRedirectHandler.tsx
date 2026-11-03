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
// When the redirect comes back WITHOUT a usable credential -- no fragment at
// all, or an error from Google -- the user gets a visible failure instead of
// a silent no-op.

import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useToast } from "@/components/ui/use-toast";
import { useGoogleLoginMutation } from "@/hooks/useGoogleLoginMutation";
import {
	clearGoogleRedirectParams,
	clearPendingGoogleRequest,
	parseGoogleRedirect,
	readPendingGoogleRequest,
} from "@/lib/googleOAuth";

export function GoogleRedirectHandler() {
	const { toast } = useToast();
	const { t } = useTranslation("login");
	const navigate = useNavigate();
	// `mutate` is stable across renders, unlike the whole mutation result.
	const { mutate } = useGoogleLoginMutation();
	// React StrictMode runs effects twice in development. The redirect must be
	// handled exactly once, or the credential would be posted twice.
	const handled = useRef(false);

	useEffect(() => {
		if (handled.current) return;
		handled.current = true;

		const payload = parseGoogleRedirect(
			window.location.hash,
			window.location.search,
		);
		const pending = readPendingGoogleRequest();

		const fail = (description: string) => {
			clearPendingGoogleRequest();
			toast({
				variant: "destructive",
				title: t("toastFailureTitle"),
				description,
			});
			navigate("/login");
		};

		// Nothing came back. If a sign-in was in flight, that is a failure the
		// user must SEE -- not a silent landing on the dashboard.
		if (payload.kind === "empty") {
			if (pending) {
				clearGoogleRedirectParams(window.history, window.location);
				fail(t("googleSignInFailed"));
			}
			return;
		}

		// A credential or an error is in the URL. Strip it BEFORE doing
		// anything else with it.
		clearGoogleRedirectParams(window.history, window.location);

		if (payload.kind === "error") {
			fail(payload.description || t("googleSignInFailed"));
			return;
		}

		// The credential must come back with the `state` this browser sent.
		// Without that check, a crafted link could sign somebody else in.
		if (!pending || pending.state !== payload.state) {
			fail(t("googleSignInUnverified"));
			return;
		}

		clearPendingGoogleRequest();
		mutate(payload.credential);
		// The mutation owns the success and failure toasts.
	}, [mutate, navigate, t, toast]);

	return null;
}

export default GoogleRedirectHandler;
