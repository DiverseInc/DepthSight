// src/hooks/useGoogleLoginMutation.ts
//
// The Google sign-in request, shared by the login page and the
// route-independent redirect handler.
//
// It was extracted from src/pages/Login.tsx unchanged, so that the handler
// which runs after Google redirects back to the site root hands the
// credential to the SAME mutation the login page used.

import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useToast } from "@/components/ui/use-toast";
import { useAuth } from "@/context/AuthContext";

/**
 * One sign-in attempt, ready to be exchanged for a session.
 */
export interface GoogleSignInSubmission {
	/** The Google ID token that came back with the redirect. */
	token: string;
	/**
	 * The one-time value for THIS attempt. The server consumes it exactly
	 * once, so one redirect cannot mint two sessions -- not by a remount,
	 * not by a reload, and not by a second tab that received the same
	 * redirect.
	 */
	signin_nonce: string;
}

export const useGoogleLoginMutation = () => {
	const { login } = useAuth();
	const { toast } = useToast();
	const { t } = useTranslation("login");

	return useMutation({
		mutationFn: async (submission: GoogleSignInSubmission) => {
			const response = await fetch("/api/v1/auth/google", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					token: submission.token,
					signin_nonce: submission.signin_nonce,
				}),
			});

			if (!response.ok) {
				const errorData = await response.json();
				throw new Error(errorData.detail || t("toastFailureTitle"));
			}
			return response.json();
		},
		onSuccess: async (data) => {
			await login(data);
			toast({ title: t("cardTitle"), description: t("toastSuccess") });
		},
		onError: (_error: Error) => {
			toast({
				variant: "destructive",
				title: t("toastFailureTitle"),
				description: "Google Login Failed",
			});
		},
	});
};
